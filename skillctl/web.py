from __future__ import annotations

import json
import uuid
import re
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import uvicorn

from .config import AppConfig, ensure_pool_layout, load_config, save_config
from .ecosystem import all_entries, ecosystem_status_rows, granted_platforms, normalize_follow_sources, normalize_grant_targets
from .indexing import fetch_from_source, index_source
from .markets import get_market_view, list_market_views
from .promote import promote_skill
from .report import build_inventory_payload, load_latest_inventory, write_inventory_reports
from .scanner import parse_frontmatter, scan_environment, sha256_file
from .syncer import parse_target_platforms, run_sync
from .tracking import (
    add_tracked_source,
    check_tracked_sources,
    get_tracked_source,
    import_from_tracked_source,
    list_tracked_sources,
    remove_tracked_source,
)


class SourceSelector(BaseModel):
    source: str | None = None
    market: str | None = None


class ScanRequest(BaseModel):
    run_async: bool = Field(default=False, alias="async")


class SyncRequest(BaseModel):
    dry_run: bool = False
    prune: bool = False
    only: str | None = None
    targets: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    run_async: bool = Field(default=False, alias="async")


class IndexRequest(SourceSelector):
    run_async: bool = Field(default=False, alias="async")


class FetchRequest(SourceSelector):
    skill: str | None = None
    fetch_all: bool = Field(default=False, alias="all")
    run_async: bool = Field(default=False, alias="async")


class TrackCreateRequest(SourceSelector):
    agent: str
    name: str
    source_id: str | None = Field(default=None, alias="id")
    note: str | None = None
    enabled: bool = True
    auto_follow: bool = True


class TrackCheckRequest(BaseModel):
    source_id: str | None = Field(default=None, alias="id")
    include_disabled: bool = False
    update_snapshot: bool = True
    show_all: bool = False
    run_async: bool = Field(default=False, alias="async")


class TrackImportRequest(BaseModel):
    source_id: str = Field(alias="id")
    fetch_all: bool = Field(default=False, alias="all")
    skills: list[str] = Field(default_factory=list)
    run_async: bool = Field(default=False, alias="async")


class EcosystemUpdateRequest(BaseModel):
    follow_sources: list[str] | None = None
    grant_targets: list[str] | None = None


class ProxyUpdateRequest(BaseModel):
    proxy_url: str | None = None
    no_proxy: str | None = None


class DriftCheckRequest(BaseModel):
    rescan: bool = True
    run_async: bool = Field(default=False, alias="async")


class PromoteAgentSkillRequest(BaseModel):
    path: str
    run_async: bool = Field(default=False, alias="async")


class PromoteAgentSkillsRequest(BaseModel):
    paths: list[str] = Field(default_factory=list)
    run_async: bool = Field(default=False, alias="async")


class SourceCompareRequest(BaseModel):
    source: str | None = None
    source_id: str | None = Field(default=None, alias="id")
    run_async: bool = Field(default=False, alias="async")


class SourceImportRequest(BaseModel):
    source: str
    skills: list[str] = Field(default_factory=list)
    run_async: bool = Field(default=False, alias="async")


class JobState:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.status = "queued"  # queued/running/success/error
        self.created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.started_at: str | None = None
        self.ended_at: str | None = None
        self.logs: list[str] = []
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.lock = Lock()


_JOBS: dict[str, JobState] = {}
_JOB_ORDER: list[str] = []
_JOBS_LOCK = Lock()
_MAX_JOBS = 120


def _clock_text() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _append_job_log(job: JobState, message: str) -> None:
    text = str(message).strip()
    if not text:
        return
    with job.lock:
        job.logs.append(f"[{_clock_text()}] {text}")


def _trim_jobs() -> None:
    with _JOBS_LOCK:
        if len(_JOB_ORDER) <= _MAX_JOBS:
            return
        # 优先淘汰最早完成的任务，保留运行中任务。
        for job_id in list(_JOB_ORDER):
            if len(_JOB_ORDER) <= _MAX_JOBS:
                break
            job = _JOBS.get(job_id)
            if not job:
                _JOB_ORDER.remove(job_id)
                continue
            with job.lock:
                status = job.status
            if status in {"success", "error"}:
                _JOBS.pop(job_id, None)
                _JOB_ORDER.remove(job_id)


def _start_job(kind: str, runner: Callable[[Callable[[str], None]], dict[str, Any]]) -> str:
    job = JobState(kind=kind)
    with _JOBS_LOCK:
        _JOBS[job.id] = job
        _JOB_ORDER.append(job.id)
    _trim_jobs()

    def _worker() -> None:
        with job.lock:
            job.status = "running"
            job.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        _append_job_log(job, f"任务开始: {kind}")
        try:
            result = runner(lambda msg: _append_job_log(job, msg))
        except Exception as exc:  # noqa: BLE001
            with job.lock:
                job.status = "error"
                job.error = str(exc)
                job.ended_at = datetime.now().astimezone().isoformat(timespec="seconds")
            _append_job_log(job, f"任务失败: {exc}")
            return

        with job.lock:
            job.status = "success"
            job.result = result
            job.ended_at = datetime.now().astimezone().isoformat(timespec="seconds")
        _append_job_log(job, f"任务完成: {kind}")

    Thread(target=_worker, daemon=True).start()
    return job.id


def _job_snapshot(job: JobState, cursor: int = 0) -> dict[str, Any]:
    with job.lock:
        safe_cursor = max(0, min(cursor, len(job.logs)))
        lines = job.logs[safe_cursor:]
        return {
            "ok": True,
            "id": job.id,
            "kind": job.kind,
            "status": job.status,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "ended_at": job.ended_at,
            "error": job.error,
            "result": job.result,
            "logs": lines,
            "next_cursor": len(job.logs),
        }


def _get_job(job_id: str) -> JobState | None:
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


def _cfg() -> AppConfig:
    cfg = load_config()
    ensure_pool_layout(cfg.pool_path)
    return cfg


def _resolve_source(source: str | None, market: str | None) -> tuple[str, dict[str, Any] | None]:
    src = (source or "").strip()
    mid = (market or "").strip().lower()
    if src and mid:
        raise HTTPException(status_code=400, detail="不能同时指定 source 和 market。")
    if src:
        return src, None
    if not mid:
        raise HTTPException(status_code=400, detail="请提供 source 或 market。")

    item = get_market_view(mid)
    if not item:
        raise HTTPException(status_code=400, detail=f"未知 market: {mid}")
    if not item.exists:
        raise HTTPException(status_code=400, detail=f"market 不可用（路径不存在）: {item.id}")
    return item.source, asdict(item)


def _inventory_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {"exists": False}
    return {
        "exists": True,
        "scanned_at": payload.get("scanned_at"),
        "workspace": payload.get("workspace"),
        "total": payload.get("total", 0),
        "counts_by_agent": payload.get("counts_by_agent", {}),
        "counts_by_scope": payload.get("counts_by_scope", {}),
    }


def _is_remote_source(raw: str | None) -> bool:
    text = str(raw or "").strip().lower()
    return bool(text) and (
        text.startswith("http://")
        or text.startswith("https://")
        or text.startswith("git@")
        or text.endswith(".git")
    )


def _skill_key(raw: str) -> str:
    text = re.sub(r"[^a-z0-9_-]+", "-", str(raw).strip().lower())
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text


def _tracking_snapshot_path(pool_dir: Path, source_id: str) -> Path:
    return pool_dir / "state" / "tracking" / f"{_skill_key(source_id) or 'tracked-source'}.json"


def _load_tracking_snapshot(pool_dir: Path, source_id: str) -> dict[str, Any] | None:
    path = _tracking_snapshot_path(pool_dir, source_id)
    if not path.exists():
        return None
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(row, dict):
        return None
    return row


def _count_skill_dirs(base: Path) -> int:
    if not base.exists():
        return 0
    return sum(1 for item in base.glob("*/SKILL.md"))


def _pool_skill_keys(pool_dir: Path) -> tuple[set[str], int]:
    root = pool_dir / "skills"
    if not root.exists():
        return set(), 0

    keys: set[str] = set()
    total = 0
    for skill_md in root.glob("*/SKILL.md"):
        total += 1
        dir_name = skill_md.parent.name
        key = _skill_key(dir_name)
        if key:
            keys.add(key)
        front_name, _desc = parse_frontmatter(skill_md)
        if front_name:
            fkey = _skill_key(front_name)
            if fkey:
                keys.add(fkey)
    return keys, total


def _pool_skill_hash_index(pool_dir: Path) -> tuple[set[str], dict[str, set[str]], set[str], int]:
    root = pool_dir / "skills"
    if not root.exists():
        return set(), {}, set(), 0

    keys: set[str] = set()
    hashes_by_key: dict[str, set[str]] = {}
    all_hashes: set[str] = set()
    total = 0

    for skill_md in root.glob("*/SKILL.md"):
        total += 1
        dir_name = skill_md.parent.name
        front_name, _desc = parse_frontmatter(skill_md)
        names = [dir_name]
        if front_name:
            names.append(front_name)

        digest = ""
        try:
            digest = sha256_file(skill_md)
        except OSError:
            digest = ""
        if digest:
            all_hashes.add(digest)

        for raw in names:
            key = _skill_key(raw)
            if not key:
                continue
            keys.add(key)
            if not digest:
                continue
            hashes_by_key.setdefault(key, set()).add(digest)
    return keys, hashes_by_key, all_hashes, total


def _dist_skill_counts(pool_dir: Path) -> dict[str, int]:
    root = pool_dir / "dist"
    if not root.exists():
        return {}
    rows: dict[str, int] = {}
    for item in root.iterdir():
        if not item.is_dir():
            continue
        rows[item.name.lower()] = _count_skill_dirs(item)
    return rows


def _target_dir_map(pool_dir: Path) -> dict[str, Path]:
    config_path = pool_dir / "config" / "targets.conf"
    if not config_path.exists():
        return {}
    rows: dict[str, Path] = {}
    for raw in config_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [chunk.strip() for chunk in line.split("|")]
        if len(parts) < 3:
            continue
        platform = parts[0].strip().lower()
        target_raw = parts[2].strip()
        if not platform or not target_raw:
            continue
        target_path = Path(target_raw).expanduser()
        if not target_path.is_absolute():
            target_path = (pool_dir / target_path).resolve()
        rows[platform] = target_path
    return rows


def _installed_skill_counts(pool_dir: Path) -> dict[str, int]:
    targets = _target_dir_map(pool_dir)
    rows: dict[str, int] = {}
    for platform, path in targets.items():
        rows[platform] = _count_skill_dirs(path)
    return rows


def _safe_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _origin_source(origin: dict[str, Any]) -> str:
    source = str(origin.get("external_source", "")).strip()
    if source:
        return source

    source = str(origin.get("source_path", "")).strip()
    if not source:
        return ""
    if "skillctl-src-" in source:
        return "(历史远程导入，未记录源地址)"
    return source


def _build_pool_skill_rows(cfg: AppConfig) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    root = cfg.pool_path / "skills"
    platforms = parse_target_platforms(cfg.pool_path)
    rows: list[dict[str, Any]] = []
    source_counter: Counter[str] = Counter()

    if not root.exists():
        return rows, []

    for skill_dir in sorted(root.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue

        display_name, desc = parse_frontmatter(skill_md)
        origin = _safe_json(skill_dir / ".skillctl-origin.json")
        source = _origin_source(origin)
        source_counter[source or "(未知)"] += 1

        dist_targets: list[str] = []
        for platform in platforms:
            link = cfg.pool_path / "dist" / platform / skill_dir.name
            if link.exists():
                dist_targets.append(platform)

        rows.append(
            {
                "id": skill_dir.name,
                "name": display_name or skill_dir.name,
                "description": desc or "",
                "source": source or "(未知)",
                "source_rel_dir": str(origin.get("external_rel_dir", "")).strip(),
                "imported_via": str(origin.get("imported_via", "manual")).strip() or "manual",
                "updated_at": str(origin.get("imported_at") or origin.get("promoted_at") or "-"),
                "distributed_to": dist_targets,
                "distributed_text": ",".join(dist_targets) if dist_targets else "-",
                "path": str(skill_dir),
            }
        )

    source_rows = [
        {"source": key, "count": value}
        for key, value in sorted(source_counter.items(), key=lambda x: (-x[1], x[0]))
    ]
    return rows, source_rows


def _resolve_compare_source(cfg: AppConfig, source: str | None, source_id: str | None) -> str:
    src = str(source or "").strip()
    sid = str(source_id or "").strip().lower()
    if src and sid:
        raise ValueError("不能同时提供 source 和 id。")
    if src:
        if not _is_remote_source(src):
            raise ValueError("当前策略仅支持 GitHub/Git 远程 source。")
        return src
    if sid:
        row = get_tracked_source(cfg, sid)
        if not row:
            raise FileNotFoundError(f"未找到 tracked source: {sid}")
        if not _is_remote_source(row.source):
            raise ValueError("当前策略仅支持 GitHub/Git 远程 source。")
        return row.source
    raise ValueError("请提供 source 或 id。")


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_pool_managed_local_skill(skill_dir: Path, pool_dir: Path) -> bool:
    pool_skills_root = (pool_dir / "skills").resolve()
    pool_dist_root = (pool_dir / "dist").resolve()
    try:
        resolved = skill_dir.resolve()
    except OSError:
        return False
    return _is_under(resolved, pool_skills_root) or _is_under(resolved, pool_dist_root)


def _detect_unmanaged_reason(
    skill_dir: Path,
    skill_md: Path,
    display_name: str,
    pool_dir: Path,
    pool_keys: set[str],
    pool_hashes_by_key: dict[str, set[str]],
    pool_all_hashes: set[str],
) -> str | None:
    # 目标目录指向 dist/skills 的链接，视为由 Pool 管理。
    if _is_pool_managed_local_skill(skill_dir, pool_dir):
        return None

    digest = ""
    try:
        digest = sha256_file(skill_md)
    except OSError:
        digest = ""

    key_dir = _skill_key(skill_dir.name)
    key_name = _skill_key(display_name)
    keys = [k for k in (key_dir, key_name) if k]

    if digest and digest in pool_all_hashes:
        return None

    overlaps = [k for k in keys if k in pool_keys]
    if overlaps:
        if digest and any(digest in pool_hashes_by_key.get(k, set()) for k in overlaps):
            return None
        return "与 Pool 同名但内容不同，建议纳入 Pool 后人工合并。"
    return "未在 Pool 中找到该本地技能。"


def _collect_target_skill_rows(pool_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for platform, target_dir in _target_dir_map(pool_dir).items():
        if not target_dir.exists():
            continue
        for skill_md in sorted(target_dir.glob("*/SKILL.md")):
            skill_dir = skill_md.parent
            front_name, _desc = parse_frontmatter(skill_md)
            rows.append(
                {
                    "agent": platform,
                    "scope": "target_local",
                    "name": front_name or skill_dir.name,
                    "skill_dir": str(skill_dir),
                    "skill_md": str(skill_md),
                    "source_type": "target_dir",
                    "enabled": True,
                }
            )
    return rows


def _detect_unmanaged_agent_skills(
    cfg: AppConfig,
    payload: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    pool_keys, pool_hashes_by_key, pool_all_hashes, _pool_total = _pool_skill_hash_index(cfg.pool_path)
    target_map = _target_dir_map(cfg.pool_path)
    target_roots: list[Path] = []
    for path in target_map.values():
        try:
            target_roots.append(path.expanduser().resolve())
        except OSError:
            continue

    rows: list[dict[str, Any]] = []
    seen_md: set[str] = set()

    def _append_row(item: dict[str, Any]) -> None:
        skill_md_raw = str(item.get("skill_md", "")).strip()
        skill_dir_raw = str(item.get("skill_dir", "")).strip()
        if not skill_md_raw or not skill_dir_raw:
            return
        skill_md = Path(skill_md_raw).expanduser()
        skill_dir = Path(skill_dir_raw).expanduser()
        if not skill_md.exists():
            return
        try:
            skill_md_key = str(skill_md.resolve())
        except OSError:
            skill_md_key = str(skill_md)
        if skill_md_key in seen_md:
            return

        name = str(item.get("name", "")).strip() or skill_dir.name
        reason = _detect_unmanaged_reason(
            skill_dir=skill_dir,
            skill_md=skill_md,
            display_name=name,
            pool_dir=cfg.pool_path,
            pool_keys=pool_keys,
            pool_hashes_by_key=pool_hashes_by_key,
            pool_all_hashes=pool_all_hashes,
        )
        if not reason:
            return

        seen_md.add(skill_md_key)
        rows.append(
            {
                "agent": str(item.get("agent", "")).strip().lower(),
                "scope": str(item.get("scope", "")).strip().lower(),
                "name": name,
                "skill_dir": str(skill_dir),
                "skill_md": str(skill_md),
                "source_type": str(item.get("source_type", "")).strip(),
                "enabled": item.get("enabled"),
                "reason": reason,
            }
        )

    # 1) 先以 targets.conf 中的本地安装目录为准。
    for item in _collect_target_skill_rows(cfg.pool_path):
        _append_row(item)

    # 2) 再补充 inventory 里不在 target 目录下的技能（例如工作区本地技能）。
    for item in (payload or {}).get("records", []):
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent", "")).strip().lower()
        if not agent:
            continue
        skill_md_raw = str(item.get("skill_md", "")).strip()
        if not skill_md_raw:
            continue
        skill_md = Path(skill_md_raw).expanduser()
        try:
            resolved_md = skill_md.resolve()
        except OSError:
            resolved_md = skill_md
        if any(_is_under(resolved_md, root) for root in target_roots):
            continue
        _append_row(item)

    rows.sort(key=lambda x: (x["agent"], x["name"], x["skill_dir"]))
    return rows


def _github_source_rows(cfg: AppConfig) -> list[dict[str, Any]]:
    tracked = list_tracked_sources(cfg, only_enabled=False)
    rows: list[dict[str, Any]] = []
    seen_source: set[str] = set()

    for row in tracked:
        if not _is_remote_source(row.source):
            continue
        snap = _load_tracking_snapshot(cfg.pool_path, row.id)
        seen_source.add(row.source)
        rows.append(
            {
                "id": row.id,
                "name": row.name,
                "agent_id": row.agent_id,
                "source": row.source,
                "enabled": row.enabled,
                "note": row.note,
                "snapshot_total": int(snap.get("total", 0)) if snap else None,
                "snapshot_checked_at": str(snap.get("checked_at", "")) if snap else "",
                "from_market": "",
                "tracked": True,
            }
        )

    for item in list_market_views(include_missing=True):
        if not _is_remote_source(item.source):
            continue
        if item.source in seen_source:
            continue
        # 仅把远程 market 作为可选 GitHub 源，不自动写入 tracked_sources。
        rows.append(
            {
                "id": item.id,
                "name": item.name,
                "agent_id": item.agent_hint,
                "source": item.source,
                "enabled": True,
                "note": item.description,
                "snapshot_total": None,
                "snapshot_checked_at": "",
                "from_market": item.id,
                "tracked": False,
            }
        )
    rows.sort(key=lambda x: (x["tracked"] is False, x["name"], x["id"]))
    return rows


def _build_topology_rows(
    cfg: AppConfig,
    inventory_payload: dict[str, Any] | None,
    drift_rows: list[dict[str, Any]],
    github_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    available = parse_target_platforms(cfg.pool_path)
    known_target_ids = {entry.id for entry in all_entries() if entry.can_consume}
    eco_rows = ecosystem_status_rows(
        follow_sources=cfg.follow_sources,
        grant_targets=cfg.grant_targets,
        inventory_payload=inventory_payload,
        available_platforms=available,
    )
    _pool_keys, pool_total = _pool_skill_keys(cfg.pool_path)

    rows: list[dict[str, Any]] = []

    for src in github_rows:
        status_bits: list[str] = []
        status_bits.append("tracked" if src["tracked"] else "候选")
        status_bits.append("enabled" if src["enabled"] else "disabled")
        rows.append(
            {
                "layer": "source",
                "id": src["id"],
                "name": src["name"],
                "owner": src["agent_id"],
                "location": src["source"],
                "skills": src["snapshot_total"] if src["snapshot_total"] is not None else "-",
                "status": ", ".join(status_bits),
                "drift": "-",
                "updated_at": src["snapshot_checked_at"] or "-",
                "tracked": src["tracked"],
                "enabled": src["enabled"],
                "from_market": src["from_market"],
            }
        )

    rows.append(
        {
            "layer": "pool",
            "id": "pool",
            "name": "统一 Skills Pool",
            "owner": "skillctl",
            "location": str(cfg.pool_path / "skills"),
            "skills": pool_total,
            "status": f"follow={len(cfg.follow_sources)} grant={len(cfg.grant_targets)}",
            "drift": "-",
            "updated_at": (inventory_payload or {}).get("scanned_at", "-"),
            "tracked": True,
            "enabled": True,
            "from_market": "",
        }
    )

    target_total = 0
    target_granted = 0
    for row in eco_rows:
        if not row.get("can_consume", False):
            continue
        target_id = str(row.get("id", ""))
        if target_id not in known_target_ids and not bool(row.get("granted", False)):
            # follow-only 的自定义来源（如 openai/axton）不计入 target 统计。
            continue

        target_total += 1
        if bool(row.get("granted", False)):
            target_granted += 1

    summary = {
        "source_total": sum(1 for x in rows if x.get("layer") == "source"),
        "source_tracked": sum(1 for x in rows if x.get("layer") == "source" and x.get("tracked")),
        "pool_total": pool_total,
        "target_total": target_total,
        "target_granted": target_granted,
        "drift_total": len(drift_rows),
    }
    return rows, summary


def _overview_payload(cfg: AppConfig) -> dict[str, Any]:
    payload = load_latest_inventory(cfg.pool_path)
    available = parse_target_platforms(cfg.pool_path)
    eco_rows = ecosystem_status_rows(
        follow_sources=cfg.follow_sources,
        grant_targets=cfg.grant_targets,
        inventory_payload=payload,
        available_platforms=available,
    )
    drift_rows = _detect_unmanaged_agent_skills(cfg, payload)
    github_rows = _github_source_rows(cfg)
    topology_rows, topology_summary = _build_topology_rows(cfg, payload, drift_rows, github_rows)
    pool_skill_rows, pool_source_rows = _build_pool_skill_rows(cfg)
    drift_counts = dict(sorted(Counter(item["agent"] for item in drift_rows).items(), key=lambda x: x[0]))
    return {
        "pool_dir": str(cfg.pool_path),
        "pool": {
            "total": len(pool_skill_rows),
            "skills": pool_skill_rows,
            "sources": pool_source_rows,
        },
        "inventory": _inventory_summary(payload),
        "network": {
            "proxy_url": cfg.proxy_url,
            "no_proxy": cfg.no_proxy,
        },
        "ecosystem": {
            "follow_sources": cfg.follow_sources,
            "grant_targets": cfg.grant_targets,
            "granted_platforms": granted_platforms(cfg.grant_targets),
            "rows": eco_rows,
        },
        "markets": [asdict(x) for x in list_market_views(include_missing=True)],
        "tracked_sources": [asdict(x) for x in list_tracked_sources(cfg, only_enabled=False)],
        "github_sources": github_rows,
        "topology": {
            "rows": topology_rows,
            "summary": topology_summary,
        },
        "drift": {
            "total": len(drift_rows),
            "counts_by_agent": drift_counts,
            "rows": drift_rows[:300],
        },
    }


def _sanitize_track_skills(skills: list[str]) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for item in skills:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(key)
    return rows


def _sanitize_skill_ids(skills: list[str]) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for item in skills:
        key = str(item).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(key)
    return rows


def _sanitize_paths(paths: list[str]) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for item in paths:
        key = str(item).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(key)
    return rows


def _do_scan(cfg: AppConfig, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    workspace = Path.cwd()
    if log:
        log(f"开始扫描工作区: {workspace}")
    records = scan_environment(workspace)
    payload = build_inventory_payload(records, workspace)
    paths = write_inventory_reports(cfg.pool_path, payload)
    if log:
        log(f"扫描完成，共 {len(records)} 条。")
        log(f"最新 JSON: {paths['latest_json']}")
        log(f"最新 MD: {paths['latest_md']}")
    return {
        "ok": True,
        "total": len(records),
        "latest_json": str(paths["latest_json"]),
        "latest_md": str(paths["latest_md"]),
    }


def _do_sync(cfg: AppConfig, req: SyncRequest, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    if log:
        log("开始执行同步。")
    allowed_platforms = granted_platforms(cfg.grant_targets)
    target_platforms = _sanitize_skill_ids(granted_platforms(req.targets))
    selected_skills = _sanitize_skill_ids(req.skills)
    only_value = req.only
    if target_platforms:
        only_value = ",".join(target_platforms)
    prune_value = req.prune
    if selected_skills and not prune_value:
        prune_value = True
        if log:
            log("检测到选择性同步，自动启用 prune 以避免目标残留旧技能。")
    result = run_sync(
        pool_dir=cfg.pool_path,
        dry_run=req.dry_run,
        prune=prune_value,
        only=only_value,
        allowed_platforms=allowed_platforms,
        selected_skills=selected_skills or None,
        log=log,
    )
    if log:
        log(f"同步结束，返回码: {result.returncode}")
    return {
        "ok": result.success,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "cmd": result.cmd,
        "selected_skills": selected_skills,
        "targets": target_platforms,
    }


def _do_index(cfg: AppConfig, req: IndexRequest, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    source, market = _resolve_source(req.source, req.market)
    if log:
        log(f"开始索引来源: {source}")
    _root, items = index_source(source, proxy=cfg.proxy_url, no_proxy=cfg.no_proxy, log=log)
    rows = [
        {
            "name": x.name,
            "description": x.description,
            "rel_dir": x.rel_dir,
        }
        for x in items[:400]
    ]
    if log:
        log(f"索引完成，共 {len(items)} 条。")
    return {
        "ok": True,
        "source": source,
        "market": market,
        "total": len(items),
        "items": rows,
    }


def _do_fetch(cfg: AppConfig, req: FetchRequest, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    source, market = _resolve_source(req.source, req.market)
    if log:
        log(f"开始导入来源: {source}")
    paths = fetch_from_source(
        source=source,
        pool_dir=cfg.pool_path,
        selector=req.skill,
        fetch_all=req.fetch_all,
        proxy=cfg.proxy_url,
        no_proxy=cfg.no_proxy,
        log=log,
    )
    if log:
        log(f"导入结束，共 {len(paths)} 个。")
    return {
        "ok": True,
        "source": source,
        "market": market,
        "total": len(paths),
        "items": paths,
    }


def _do_track_check(cfg: AppConfig, req: TrackCheckRequest, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    if log:
        log("开始检查 tracked sources。")
    results = check_tracked_sources(
        cfg=cfg,
        pool_dir=cfg.pool_path,
        source_id=req.source_id,
        only_enabled=not req.include_disabled,
        update_snapshot=req.update_snapshot,
        proxy=cfg.proxy_url,
        no_proxy=cfg.no_proxy,
        log=log,
    )

    rows: list[dict[str, Any]] = []
    for r in results:
        if log:
            log(f"[{r.source.id}] total={r.total} +{len(r.added)} -{len(r.removed)}")
        rows.append(
            {
                "id": r.source.id,
                "agent_id": r.source.agent_id,
                "name": r.source.name,
                "checked_at": r.checked_at,
                "total": r.total,
                "added": r.added,
                "removed": r.removed,
                "items": [{"name": x.name, "rel_dir": x.rel_dir, "description": x.description} for x in r.all_items]
                if req.show_all
                else [],
            }
        )
    return {"ok": True, "results": rows}


def _do_track_import(cfg: AppConfig, req: TrackImportRequest, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    skills = _sanitize_track_skills(req.skills)
    if log:
        log(f"开始导入 tracked source: {req.source_id}")
    imported = import_from_tracked_source(
        cfg=cfg,
        pool_dir=cfg.pool_path,
        source_id=req.source_id,
        selectors=skills,
        fetch_all=req.fetch_all,
        proxy=cfg.proxy_url,
        no_proxy=cfg.no_proxy,
        log=log,
    )
    if log:
        log(f"Track 导入完成，共 {len(imported)} 个。")
    return {"ok": True, "total": len(imported), "items": imported}


def _do_source_compare(
    cfg: AppConfig,
    req: SourceCompareRequest,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    source = _resolve_compare_source(cfg, req.source, req.source_id)
    if log:
        log(f"开始比较来源与 Pool 差异: {source}")
    _root, items = index_source(source, proxy=cfg.proxy_url, no_proxy=cfg.no_proxy, log=log)
    pool_keys, _pool_total = _pool_skill_keys(cfg.pool_path)

    rows: list[dict[str, Any]] = []
    new_total = 0
    for item in items[:1000]:
        rel_leaf = Path(item.rel_dir).name
        in_pool = (_skill_key(item.name) in pool_keys) or (_skill_key(rel_leaf) in pool_keys)
        if not in_pool:
            new_total += 1
        rows.append(
            {
                "name": item.name,
                "description": item.description,
                "rel_dir": item.rel_dir,
                "in_pool": in_pool,
                "status": "已在 Pool" if in_pool else "新增候选",
            }
        )
    if log:
        log(f"比较完成：来源 {len(items)} 条，新增候选 {new_total} 条。")
    return {
        "ok": True,
        "compare": True,
        "source": source,
        "total": len(items),
        "new_total": new_total,
        "in_pool_total": len(items) - new_total,
        "items": rows,
    }


def _do_source_import(
    cfg: AppConfig,
    req: SourceImportRequest,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    source = _resolve_compare_source(cfg, req.source, None)
    selectors = _sanitize_skill_ids(req.skills)
    if not selectors:
        raise ValueError("请先选择要导入的 skills。")

    imported: list[str] = []
    if log:
        log(f"开始按选择导入，共 {len(selectors)} 项。")
    for rel_dir in selectors:
        rows = fetch_from_source(
            source=source,
            pool_dir=cfg.pool_path,
            selector=rel_dir,
            fetch_all=False,
            proxy=cfg.proxy_url,
            no_proxy=cfg.no_proxy,
            log=log,
        )
        imported.extend(rows)
    if log:
        log(f"导入完成，共 {len(imported)} 个。")
    return {
        "ok": True,
        "source": source,
        "total": len(imported),
        "items": imported,
    }


def _do_drift_check(cfg: AppConfig, req: DriftCheckRequest, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    if req.rescan:
        workspace = Path.cwd()
        if log:
            log(f"执行实时扫描: {workspace}")
        records = scan_environment(workspace)
        payload = build_inventory_payload(records, workspace)
        write_inventory_reports(cfg.pool_path, payload)
    else:
        payload = load_latest_inventory(cfg.pool_path)
        if payload is None:
            workspace = Path.cwd()
            if log:
                log("未找到 inventory-latest，自动执行一次扫描。")
            records = scan_environment(workspace)
            payload = build_inventory_payload(records, workspace)
            write_inventory_reports(cfg.pool_path, payload)

    rows = _detect_unmanaged_agent_skills(cfg, payload)
    counts_by_agent = dict(sorted(Counter(item["agent"] for item in rows).items(), key=lambda x: x[0]))
    if log:
        log(f"Check 完成：发现 {len(rows)} 条未纳入 Pool 的本地技能。")
    return {
        "ok": True,
        "total": len(rows),
        "counts_by_agent": counts_by_agent,
        "rows": rows[:300],
    }


def _do_promote_agent_skill(
    cfg: AppConfig,
    req: PromoteAgentSkillRequest,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    source_path = str(req.path or "").strip()
    if not source_path:
        raise ValueError("缺少 path。")
    if log:
        log(f"纳入 Pool: {source_path}")
    result = promote_skill(source_path, cfg.pool_path, Path.cwd())
    if not result.success:
        raise RuntimeError(result.message)
    if log:
        log(f"纳入完成: {result.destination}")
    return {
        "ok": True,
        "action": result.action,
        "source": result.source,
        "destination": result.destination,
        "message": result.message,
    }


def _do_promote_agent_skills(
    cfg: AppConfig,
    req: PromoteAgentSkillsRequest,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    paths = _sanitize_paths(req.paths)
    if not paths:
        raise ValueError("请至少提供一个 path。")

    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for source_path in paths:
        if log:
            log(f"纳入 Pool: {source_path}")
        result = promote_skill(source_path, cfg.pool_path, Path.cwd())
        if result.success:
            items.append(
                {
                    "path": source_path,
                    "action": result.action,
                    "destination": result.destination,
                    "message": result.message,
                }
            )
            if log:
                log(f"纳入完成: {result.destination}")
            continue
        errors.append({"path": source_path, "error": result.message})
        if log:
            log(f"纳入失败: {source_path} -> {result.message}")

    if log:
        log(f"批量纳入结束：成功 {len(items)}，失败 {len(errors)}。")
    return {
        "ok": len(errors) == 0,
        "total": len(paths),
        "success_total": len(items),
        "failed_total": len(errors),
        "items": items,
        "errors": errors,
    }


def _page_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>skillctl Web 控制台</title>
  <style>
    :root {
      --bg: #f3ecdf;
      --bg-deep: #e8decf;
      --card: rgba(255, 252, 246, 0.88);
      --card-soft: rgba(255, 249, 238, 0.9);
      --ink: #1f2937;
      --muted: #5f6772;
      --line: #d6c8b2;
      --line-soft: #e6d9c6;
      --accent: #0f766e;
      --accent-strong: #0b5f58;
      --accent2: #145cb8;
      --warn: #b42318;
      --ok: #2f8f5b;
      --radius: 16px;
      --shadow: 0 16px 36px rgba(61, 44, 24, 0.12);
      --shadow-soft: 0 10px 24px rgba(50, 35, 16, 0.08);
      --font-ui: "Avenir Next", "PingFang SC", "Noto Sans CJK SC", sans-serif;
      --font-display: "Iowan Old Style", "Songti SC", "STSong", serif;
      --font-mono: "SF Mono", Menlo, Monaco, Consolas, monospace;
    }
    * { box-sizing: border-box; }
    html, body { min-height: 100%; }
    body {
      margin: 0;
      color: var(--ink);
      font-family: var(--font-ui);
      background:
        radial-gradient(circle at 8% 12%, rgba(15, 118, 110, 0.14), transparent 36%),
        radial-gradient(circle at 92% 8%, rgba(20, 92, 184, 0.14), transparent 40%),
        radial-gradient(circle at 65% 92%, rgba(159, 95, 33, 0.12), transparent 36%),
        linear-gradient(150deg, var(--bg) 0%, #f8f1e6 45%, var(--bg-deep) 100%);
    }
    .wrap {
      width: min(1280px, 96vw);
      margin: 16px auto 20px;
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 12px;
      padding-bottom: 136px;
      animation: pageIn 420ms ease-out both;
    }
    @keyframes pageIn {
      from { opacity: 0; transform: translateY(8px); }
      to { opacity: 1; transform: translateY(0); }
    }
    .hero {
      grid-column: 1 / -1;
      border: 1px solid var(--line);
      border-radius: calc(var(--radius) + 6px);
      box-shadow: var(--shadow);
      padding: 16px 18px;
      background:
        linear-gradient(120deg, rgba(255, 255, 255, 0.84), rgba(255, 249, 239, 0.84)),
        linear-gradient(22deg, rgba(15, 118, 110, 0.1), rgba(20, 92, 184, 0.1));
      position: relative;
      overflow: hidden;
    }
    .hero::after {
      content: "";
      position: absolute;
      right: -110px;
      top: -80px;
      width: 300px;
      height: 300px;
      background: radial-gradient(circle, rgba(15, 118, 110, 0.2), rgba(15, 118, 110, 0) 68%);
      pointer-events: none;
    }
    .hero h1 {
      margin: 0 0 6px;
      display: flex;
      flex-wrap: wrap;
      align-items: flex-end;
      gap: 10px;
      line-height: 1.06;
    }
    .hero-title-main {
      font-family: var(--font-display);
      font-size: clamp(30px, 3.4vw, 44px);
      letter-spacing: 0.03em;
      font-weight: 700;
      background: linear-gradient(95deg, #0d5e58 0%, #145cb8 48%, #8a5522 100%);
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
      text-shadow: 0 7px 20px rgba(20, 92, 184, 0.16);
    }
    .hero-title-sub {
      font-size: clamp(14px, 1.2vw, 16px);
      letter-spacing: 0.16em;
      text-transform: uppercase;
      color: #244761;
      padding: 5px 12px 4px;
      border-radius: 999px;
      border: 1px solid rgba(36, 71, 97, 0.24);
      background: linear-gradient(180deg, rgba(255, 255, 255, 0.82), rgba(228, 240, 255, 0.54));
      transform: translateY(-2px);
      white-space: nowrap;
    }
    .hero p {
      margin: 0;
      color: #4c5b6c;
      font-size: 14px;
      max-width: 860px;
    }
    .hero-console {
      margin-top: 12px;
      border: 1px solid var(--line-soft);
      border-radius: 14px;
      background: rgba(255, 255, 255, 0.78);
      padding: 10px;
      backdrop-filter: blur(4px);
    }
    .hero-console-title {
      font-size: 12px;
      color: #334155;
      font-weight: 650;
      margin-bottom: 6px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .hero-console-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(220px, 1fr));
      gap: 8px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow-soft);
      padding: 12px;
      backdrop-filter: blur(2px);
    }
    .card h2 {
      margin: 0;
      font-family: var(--font-display);
      letter-spacing: 0.02em;
      font-size: 19px;
      font-weight: 650;
      color: #1f3650;
    }
    .card-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 8px;
    }
    .card-hint {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 8px;
    }
    .topology, .summary, .source, .pool { grid-column: 1 / -1; }
    .sync, .ecosystem { grid-column: span 6; }
    .topology { order: 1; }
    .summary { order: 2; }
    .source { order: 3; }
    .sync { order: 4; }
    .ecosystem { order: 5; }
    .pool { order: 99; }
    .ecosystem.hidden { display: none; }
    .source { min-height: 420px; }
    .sync, .ecosystem { min-height: 340px; }
    .row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: end;
      margin-bottom: 6px;
    }
    .summary-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 8px;
    }
    .summary-head h2 { margin: 0; }
    .summary-layout {
      display: grid;
      grid-template-columns: 1.7fr 1fr;
      gap: 10px;
    }
    .summary-main {
      border: 1px solid var(--line-soft);
      border-radius: 14px;
      background: linear-gradient(175deg, rgba(255, 255, 255, 0.82), rgba(255, 248, 236, 0.92));
      padding: 10px;
      min-height: 170px;
    }
    .summary-actions {
      border: 1px solid var(--line-soft);
      border-radius: 14px;
      background: linear-gradient(182deg, rgba(255, 255, 255, 0.86), rgba(248, 244, 238, 0.88));
      padding: 10px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      min-height: 170px;
    }
    .summary-actions button {
      width: 100%;
      justify-content: center;
    }
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(110px, 1fr));
      gap: 8px;
      margin-bottom: 10px;
    }
    .kpi {
      border: 1px solid #decfb8;
      border-radius: 11px;
      background: linear-gradient(175deg, #fff, #fef8ed);
      padding: 8px;
      transition: transform 150ms ease, box-shadow 150ms ease;
    }
    .kpi:hover {
      transform: translateY(-1px);
      box-shadow: 0 6px 14px rgba(37, 49, 64, 0.1);
    }
    .kpi-label {
      color: var(--muted);
      font-size: 11px;
      line-height: 1.35;
      margin-bottom: 3px;
    }
    .kpi-value {
      font-size: 22px;
      font-weight: 680;
      line-height: 1.05;
      color: #102a43;
    }
    .summary-meta {
      display: grid;
      grid-template-columns: 1fr;
      gap: 5px;
      margin-bottom: 8px;
    }
    .summary-meta-row {
      display: flex;
      align-items: baseline;
      gap: 8px;
      font-size: 12px;
      color: var(--muted);
      min-width: 0;
    }
    .summary-meta-row .mono {
      color: #334155;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      display: block;
      min-width: 0;
      flex: 1;
    }
    .summary-core {
      border: 1px solid #e2d4bf;
      border-radius: 12px;
      background: rgba(255, 252, 246, 0.86);
      padding: 8px;
      margin-bottom: 8px;
    }
    .summary-subgrid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1.08fr);
      gap: 8px;
      align-items: start;
    }
    .summary-sources-title {
      font-size: 12px;
      color: #334155;
      font-weight: 650;
      margin-bottom: 4px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }
    .source-chip-cloud {
      max-height: 120px;
      overflow-y: auto;
      padding-right: 2px;
    }
    .summary-drift-panel .table-scroll.short { max-height: 210px; }
    .task-panel {
      border: 1px solid #d9ccb7;
      background: linear-gradient(180deg, rgba(255, 255, 255, 0.9), rgba(246, 242, 236, 0.9));
      margin-bottom: 2px;
    }
    .task-stats {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 6px;
      margin-bottom: 6px;
    }
    .task-stat {
      border: 1px solid #e1d3be;
      border-radius: 9px;
      padding: 6px 7px;
      background: #fff;
      font-size: 12px;
      color: #475569;
    }
    .task-stat b {
      display: block;
      font-size: 18px;
      line-height: 1.05;
      color: #0f172a;
      margin-top: 2px;
    }
    .split-2 {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }
    .panel {
      border: 1px solid var(--line-soft);
      background: var(--card-soft);
      border-radius: 12px;
      padding: 9px;
      min-width: 0;
    }
    .panel + .panel { margin-top: 0; }
    .panel-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 6px;
      font-size: 12px;
      color: #334155;
      font-weight: 650;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }
    .source-shell {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1.7fr);
      gap: 10px;
      align-items: start;
    }
    .source-side-stack {
      display: grid;
      gap: 10px;
      align-content: start;
    }
    .source-mobile-tabs {
      display: none;
      gap: 6px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      margin-bottom: 2px;
    }
    .source-mobile-tabs button {
      border-radius: 999px;
      padding: 7px 8px;
      font-size: 12px;
      font-weight: 650;
    }
    .source-mobile-tabs button.active {
      background: linear-gradient(160deg, var(--accent2), #0e4b95);
      color: #fff;
      border-color: transparent;
    }
    .source-form {
      padding: 11px;
      background: linear-gradient(180deg, rgba(255, 255, 255, 0.9), rgba(252, 245, 234, 0.94));
    }
    .source-form-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      align-items: end;
    }
    .source-form-grid .wide { grid-column: 1 / -1; }
    .source-flags {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
    }
    .source-flags label {
      margin: 0;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 5px 9px;
      border: 1px solid #d6ccbc;
      border-radius: 999px;
      background: linear-gradient(180deg, #fff, #f6f1e8);
      color: #3f4f61;
      font-size: 12px;
      font-weight: 560;
    }
    .source-compare-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 8px;
      align-items: end;
    }
    .source-compare .btn-row button { flex: 1 1 150px; }
    .source-tracks .table-scroll { max-height: 680px; }
    .source-compare .table-scroll { max-height: 330px; }
    .stack-6 > * + * { margin-top: 6px; }
    .btn-row {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 6px;
    }
    .btn-row button { flex: 1 1 120px; }
    label {
      display: block;
      margin-bottom: 4px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 540;
    }
    input, select, button { font: inherit; }
    input, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 8px 10px;
      background: rgba(255, 255, 255, 0.96);
      transition: border-color 150ms ease, box-shadow 150ms ease;
    }
    input:focus, select:focus {
      outline: none;
      border-color: rgba(20, 92, 184, 0.64);
      box-shadow: 0 0 0 3px rgba(20, 92, 184, 0.14);
    }
    input[type="checkbox"] {
      width: auto;
      border: 1px solid #b9c3d3;
      padding: 0;
      border-radius: 4px;
      box-shadow: none;
      accent-color: var(--accent2);
    }
    button {
      border: 1px solid #d8c9b3;
      border-radius: 10px;
      padding: 8px 11px;
      cursor: pointer;
      background: linear-gradient(180deg, #ffffff, #f5f0e6);
      color: #2f3c4f;
      font-weight: 620;
      transition: transform 120ms ease, box-shadow 120ms ease, border-color 120ms ease;
    }
    button:hover {
      transform: translateY(-1px);
      box-shadow: 0 6px 14px rgba(33, 42, 54, 0.14);
      border-color: #cbb899;
    }
    button.primary {
      background: linear-gradient(160deg, var(--accent), var(--accent-strong));
      color: #fff;
      border-color: transparent;
    }
    button.alt {
      background: linear-gradient(160deg, var(--accent2), #0e4b95);
      color: #fff;
      border-color: transparent;
    }
    button.danger {
      background: linear-gradient(160deg, var(--warn), #8f1f17);
      color: #fff;
      border-color: transparent;
    }
    button:disabled {
      opacity: 0.55;
      cursor: not-allowed;
      transform: none;
      box-shadow: none;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      border-bottom: 1px solid #eee3d1;
      padding: 7px 5px;
      text-align: left;
      vertical-align: top;
    }
    th {
      color: #526377;
      font-weight: 650;
      letter-spacing: 0.01em;
      background: #f7f2e8;
    }
    tbody tr:nth-child(even) { background: rgba(249, 244, 233, 0.45); }
    tbody tr:hover { background: rgba(20, 92, 184, 0.08); }
    .table-scroll {
      border: 1px solid #e8dbc7;
      border-radius: 10px;
      overflow: auto;
      max-height: 260px;
      background: #fff;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.6);
    }
    .table-scroll table { margin: 0; }
    .table-scroll.tall { max-height: 390px; }
    .table-scroll.short { max-height: 220px; }
    .table-scroll th {
      position: sticky;
      top: 0;
      z-index: 1;
    }
    .track-actions {
      display: flex;
      align-items: center;
      gap: 6px;
      min-width: 190px;
      justify-content: flex-start;
      position: relative;
    }
    .track-actions > button {
      min-width: 92px;
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 12px;
      line-height: 1.2;
      font-weight: 670;
      letter-spacing: 0.01em;
      box-shadow: none;
      white-space: nowrap;
    }
    .track-actions > button:hover {
      transform: translateY(-1px);
      box-shadow: 0 4px 10px rgba(33, 42, 54, 0.12);
    }
    .track-actions .btn-compare {
      background: linear-gradient(180deg, #f2f8ff, #dfefff);
      border-color: #b8d1ef;
      color: #1e4976;
    }
    .track-actions .btn-check {
      background: linear-gradient(180deg, #ffffff, #edf2f8);
      border-color: #cdd8e7;
      color: #334155;
    }
    .track-actions .btn-import {
      background: linear-gradient(160deg, var(--accent), var(--accent-strong));
      border-color: transparent;
      color: #fff;
    }
    .track-actions .btn-delete {
      background: linear-gradient(180deg, #fff2f0, #ffe5e1);
      border-color: #e4b1a9;
      color: #9b241b;
    }
    .track-menu {
      position: relative;
    }
    .track-menu summary {
      list-style: none;
      border: 1px solid #d9c9b2;
      border-radius: 999px;
      padding: 6px 10px;
      background: linear-gradient(180deg, #fff, #f5f0e6);
      font-size: 12px;
      font-weight: 650;
      color: #334155;
      cursor: pointer;
      user-select: none;
      line-height: 1.2;
      min-width: 62px;
      text-align: center;
    }
    .track-menu summary::-webkit-details-marker { display: none; }
    .track-menu[open] summary {
      border-color: #b8c6da;
      box-shadow: 0 4px 10px rgba(33, 42, 54, 0.12);
      background: linear-gradient(180deg, #f4f8ff, #eaf1ff);
    }
    .track-menu-items {
      position: absolute;
      top: calc(100% + 6px);
      right: 0;
      min-width: 132px;
      z-index: 12;
      border: 1px solid #d7c8b2;
      border-radius: 10px;
      background: #fffefb;
      box-shadow: 0 10px 18px rgba(33, 42, 54, 0.18);
      padding: 6px;
      display: grid;
      gap: 5px;
    }
    .track-menu-items button {
      width: 100%;
      border-radius: 8px;
      padding: 6px 8px;
      font-size: 12px;
      font-weight: 640;
      box-shadow: none;
    }
    .track-menu-items button:hover {
      transform: none;
      box-shadow: none;
    }
    .mono {
      font-family: var(--font-mono);
      font-size: 12px;
      word-break: break-all;
    }
    .pill {
      display: inline-block;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 11px;
      background: rgba(20, 92, 184, 0.12);
      color: #184b88;
      border: 1px solid rgba(20, 92, 184, 0.2);
      white-space: nowrap;
    }
    .warn {
      background: rgba(180, 35, 24, 0.12);
      color: #9b241b;
      border-color: rgba(180, 35, 24, 0.2);
    }
    .hint { color: var(--muted); font-size: 12px; }
    .mini {
      display: inline-block;
      font-size: 11px;
      color: #475569;
      background: #e8eef7;
      border-radius: 999px;
      border: 1px solid #d4dfef;
      padding: 2px 8px;
      margin-right: 5px;
      margin-bottom: 4px;
    }
    .logbar {
      position: fixed;
      left: 50%;
      bottom: 10px;
      transform: translateX(-50%);
      width: min(1280px, 96vw);
      z-index: 50;
      background: rgba(254, 250, 243, 0.95);
      border: 1px solid #cdbca2;
      border-radius: 12px;
      box-shadow: 0 12px 24px rgba(48, 38, 21, 0.22);
      padding: 8px 10px;
    }
    .logbar-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 6px;
    }
    .logbar h2 {
      margin: 0;
      font-family: var(--font-display);
      font-size: 17px;
      color: #1f3650;
    }
    .logbar-tools {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }
    .logbar-tools button {
      border-radius: 999px;
      padding: 5px 10px;
      font-size: 12px;
      box-shadow: none;
    }
    .log-current {
      margin-bottom: 6px;
      font-size: 12px;
    }
    #logLines {
      font-family: var(--font-mono);
      font-size: 12px;
      background: #111827;
      color: #dbeafe;
      border-radius: 10px;
      border: 1px solid #2f3b4d;
      padding: 8px;
      min-height: 92px;
      max-height: 160px;
      overflow-y: auto;
      overflow-x: hidden;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .logbar.collapsed #logLines,
    .logbar.collapsed .log-current {
      display: none;
    }
    .undo-bar {
      position: fixed;
      left: 50%;
      bottom: 182px;
      transform: translateX(-50%);
      width: min(860px, 92vw);
      z-index: 60;
      display: flex;
      align-items: center;
      gap: 10px;
      border: 1px solid #e2bc85;
      border-radius: 12px;
      background: linear-gradient(180deg, #fffaf0, #fff4df);
      box-shadow: 0 10px 18px rgba(72, 44, 14, 0.2);
      padding: 8px 10px;
    }
    .undo-bar.hidden { display: none; }
    .undo-bar #undoText {
      flex: 1;
      font-size: 13px;
      color: #6b4423;
    }
    .undo-bar button {
      border-radius: 999px;
      padding: 6px 12px;
      font-size: 12px;
      font-weight: 680;
      background: linear-gradient(160deg, #145cb8, #0e4b95);
      color: #fff;
      border-color: transparent;
    }
    .target-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(136px, 1fr));
      gap: 7px;
    }
    .target-grid label {
      margin: 0;
      padding: 7px 9px;
      border: 1px solid #d9cfbe;
      border-radius: 9px;
      background: linear-gradient(180deg, #fff, #f5efe4);
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      color: #334155;
      transition: border-color 120ms ease, box-shadow 120ms ease, transform 120ms ease;
    }
    .target-grid label:hover {
      border-color: #bfa784;
      box-shadow: 0 6px 12px rgba(54, 43, 25, 0.12);
      transform: translateY(-1px);
    }
    @media (max-width: 980px) {
      .topology, .summary, .pool, .source, .sync, .ecosystem { grid-column: 1 / -1; }
      .summary-layout { grid-template-columns: 1fr; }
      .kpi-grid { grid-template-columns: repeat(2, minmax(110px, 1fr)); }
      .summary-subgrid { grid-template-columns: 1fr; }
      .split-2 { grid-template-columns: 1fr; }
      .source-shell { grid-template-columns: 1fr; }
      .source-side-stack { grid-template-columns: 1fr; }
      .source-mobile-tabs { display: grid; }
      .source-side-stack.tab-form .source-compare { display: none; }
      .source-side-stack.tab-compare .source-form { display: none; }
      .source-form-grid { grid-template-columns: 1fr; }
      .source-compare-grid { grid-template-columns: 1fr; }
      .hero-console-grid { grid-template-columns: 1fr; }
      .summary-actions button { width: 100%; }
      .hero h1 { gap: 8px; }
      .hero-title-sub {
        letter-spacing: 0.11em;
        transform: translateY(0);
        padding: 4px 10px 3px;
      }
      .track-actions {
        min-width: 0;
      }
      .track-actions > button {
        min-width: 82px;
      }
      .undo-bar {
        bottom: 172px;
        width: 94vw;
        align-items: flex-start;
        flex-direction: column;
        gap: 6px;
      }
      .undo-bar button {
        align-self: flex-end;
      }
      .logbar-tools {
        justify-content: flex-end;
      }
      .task-stats {
        grid-template-columns: 1fr 1fr 1fr;
      }
      .summary-main {
        min-height: 0;
      }
      .summary-actions {
        min-width: 0;
      }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1 aria-label="skillctl Web 控制台">
        <span class="hero-title-main">skillctl</span>
        <span class="hero-title-sub">Web 控制台</span>
      </h1>
      <p>入口 Source -> 中枢 Pool -> 出口 Agent。GUI 仅执行真实同步，不提供 dry-run。</p>
      <div class="hero-console">
        <div class="hero-console-title">控制台网络代理</div>
        <div class="hero-console-grid">
          <div>
            <label>Proxy URL</label>
            <input id="proxyUrl" placeholder="例如 http://127.0.0.1:7897" />
          </div>
          <div>
            <label>NO_PROXY（可空）</label>
            <input id="proxyNoProxy" placeholder="例如 localhost,127.0.0.1" />
          </div>
        </div>
        <div class="btn-row">
          <button class="primary" onclick="saveProxy()">保存代理</button>
          <button onclick="clearProxy()">清空代理</button>
        </div>
      </div>
    </section>

    <section class="card topology">
      <div class="card-head">
        <h2>主界面总览（Source / Pool）</h2>
        <span id="topologyCount" class="pill">0 行</span>
      </div>
      <div class="card-hint">此处仅展示 Source 与 Pool；Agent 目标请在“同步工具”查看与操作。</div>
      <div class="table-scroll tall">
        <table>
          <thead>
            <tr>
              <th>层级</th><th>Source ID</th><th>Source 名称</th><th>Agent ID</th><th>源地址</th>
              <th>Skills</th><th>状态</th><th>漂移</th><th>更新时间</th>
            </tr>
          </thead>
          <tbody id="topologyRows"></tbody>
        </table>
      </div>
    </section>

    <section class="card summary">
      <div class="summary-head">
        <h2>总览与操作</h2>
        <span id="summaryScanTime" class="pill">加载中</span>
      </div>
      <div class="summary-layout">
        <div class="summary-main">
          <div class="summary-core">
            <div class="kpi-grid">
              <div class="kpi">
                <div class="kpi-label">Pool Skills</div>
                <div class="kpi-value" id="kpiPoolTotal">-</div>
              </div>
              <div class="kpi">
                <div class="kpi-label">Source（已登记/总候选）</div>
                <div class="kpi-value" id="kpiSourceTotal">-</div>
              </div>
              <div class="kpi">
                <div class="kpi-label">Agent出口（已授权/总数）</div>
                <div class="kpi-value" id="kpiTargetTotal">-</div>
              </div>
              <div class="kpi">
                <div class="kpi-label">Check 漂移</div>
                <div class="kpi-value" id="kpiDriftTotal">-</div>
              </div>
            </div>
            <div class="summary-meta">
              <div class="summary-meta-row">
                <span>Pool目录</span>
                <span id="summaryPoolDir" class="mono">-</span>
              </div>
              <div class="summary-meta-row">
                <span>网络代理</span>
                <span id="summaryProxy" class="mono">-</span>
              </div>
            </div>
          </div>
          <div class="summary-subgrid">
            <div class="panel">
              <div class="summary-sources-title">来源分布（Top 12）</div>
              <div id="poolSourceChips" class="source-chip-cloud"></div>
            </div>
            <div class="panel summary-drift-panel">
              <div class="panel-title">
                <span>Check 结果（未纳入 Pool）</span>
                <span id="driftCount" class="pill">0 条</span>
              </div>
              <div class="table-scroll short">
                <table>
                  <thead><tr><th>Agent</th><th>Skill</th><th>目录</th><th>原因</th><th>操作</th></tr></thead>
                  <tbody id="driftRows"></tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
        <div class="summary-actions">
          <div class="panel task-panel stack-6">
            <div class="panel-title"><span>任务队列</span></div>
            <div class="task-stats">
              <div class="task-stat">运行中<b id="taskRunning">0</b></div>
              <div class="task-stat">成功<b id="taskSuccess">0</b></div>
              <div class="task-stat">失败<b id="taskError">0</b></div>
            </div>
            <div class="hint">当前任务</div>
            <div id="taskCurrent" class="mono">-</div>
          </div>
          <button class="primary" onclick="refreshAll()">刷新总览</button>
          <button class="alt" onclick="runDriftCheck()">扫描并检查</button>
          <button onclick="promoteAllDrift()">全部纳入 Pool</button>
          <button id="ecoToggleBtn" onclick="toggleEcosystem()">隐藏高级区</button>
          <div class="hint">建议流程：先刷新 -> 扫描并检查 -> 复核结果 -> 全部纳入 Pool。</div>
        </div>
      </div>
    </section>

    <section class="card source">
      <div class="card-head">
        <h2>Source 工具</h2>
        <span class="pill">入口</span>
      </div>
      <div class="card-hint">先登记 Source，再做差异比较，最后按需导入到 Pool。</div>
      <div class="source-shell">
        <div id="sourceSideStack" class="source-side-stack tab-form">
          <div class="source-mobile-tabs">
            <button id="sourceTabForm" class="active" onclick="setSourceTab('form')">① 新增</button>
            <button id="sourceTabCompare" onclick="setSourceTab('compare')">③ 比较</button>
          </div>
          <div class="panel source-form stack-6">
            <div class="panel-title">
              <span>① 新增 Source（登记入口）</span>
              <span class="pill">GitHub / Git</span>
            </div>
            <div class="source-form-grid">
              <div>
                <label>Agent ID</label>
                <input id="trackAgent" placeholder="openai/deepseek..." />
              </div>
              <div>
                <label>Source 名称</label>
                <input id="trackName" placeholder="source name" />
              </div>
              <div>
                <label>Source ID（可空）</label>
                <input id="trackId" placeholder="auto" />
              </div>
              <div class="wide">
                <label>源地址</label>
                <input id="trackSource" placeholder="https://github.com/org/repo.git" />
              </div>
            </div>
            <div class="source-flags">
              <label><input id="trackEnabled" type="checkbox" checked /> enabled</label>
              <label><input id="trackAutoFollow" type="checkbox" checked /> auto follow</label>
              <button class="primary" onclick="addTrack()">新增 Source</button>
            </div>
          </div>

          <div class="panel source-compare stack-6">
            <div class="panel-title"><span>③ 差异比较与选择导入</span></div>
            <div class="source-compare-grid">
              <div>
                <label>从已登记 Source 选择</label>
                <select id="compareSourcePreset"><option value="">(手动输入)</option></select>
              </div>
              <div>
                <label>或直接输入 Source</label>
                <input id="compareSourceInput" placeholder="https://github.com/org/repo.git" />
              </div>
            </div>
            <div class="btn-row">
              <button class="alt" onclick="runSourceCompare()">比较差异</button>
              <button onclick="importSelectedCandidates()">导入选中候选</button>
              <button onclick="importAllNewCandidates()">导入全部新增候选</button>
            </div>
            <div class="table-scroll">
              <table>
                <thead><tr><th>选中</th><th>Skill</th><th>rel_dir</th><th>状态</th><th>操作</th></tr></thead>
                <tbody id="compareRows"></tbody>
              </table>
            </div>
          </div>
        </div>

        <div class="panel source-tracks stack-6">
          <div class="panel-title"><span>② 已登记 Source（跟踪与导入）</span></div>
          <div class="hint">行内可直接执行：用于比较 / 检查 / 全量导入 / 删除。</div>
          <div class="table-scroll">
            <table>
              <thead><tr><th>Source ID</th><th>Source 名称</th><th>Agent ID</th><th>源地址</th><th>快照</th><th>操作</th></tr></thead>
              <tbody id="trackRows"></tbody>
            </table>
          </div>
        </div>
      </div>
    </section>

    <section class="card pool">
      <div class="card-head">
        <h2>Pool 中枢（Skill 来源与去向）</h2>
        <span id="poolSelectionHint" class="pill">已选 0 项</span>
      </div>
      <div class="card-hint">若来源显示“历史远程导入，未记录源地址”，表示旧版本导入时未保存 external_source 元数据。</div>
      <div class="panel">
        <div class="row">
          <div style="flex:1; min-width:220px;">
            <label>过滤 Skill（按 ID/名称/来源）</label>
            <input id="poolFilter" placeholder="输入关键字过滤" />
          </div>
        </div>
        <div class="btn-row">
          <button onclick="selectVisiblePoolSkills()">全选可见 Skill</button>
          <button onclick="clearPoolSelection()">清空选中</button>
        </div>
      </div>
      <div class="table-scroll tall">
        <table>
          <thead><tr><th>选中</th><th>Skill ID</th><th>名称</th><th>来源</th><th>分发到</th><th>更新时间</th></tr></thead>
          <tbody id="poolRows"></tbody>
        </table>
      </div>
    </section>

    <section class="card sync">
      <div class="card-head">
        <h2>同步工具（把 Pool Skill 分发到 Agent）</h2>
        <span class="pill">出口</span>
      </div>
      <div class="card-hint">不支持 dry-run。选择性同步会自动启用 prune，避免目标残留旧技能。</div>
      <div class="panel">
        <label>目标 Agent（可多选）</label>
        <div id="syncTargets" class="target-grid"></div>
        <div class="row" style="margin-top:6px;">
          <label style="width:190px;"><input id="syncPrune" type="checkbox" /> prune（清理旧链接）</label>
        </div>
        <div class="btn-row">
          <button class="primary" onclick="runSyncSelected()">同步到选中目标</button>
        </div>
      </div>
      <div class="card-hint" id="syncHint"></div>
    </section>

    <section id="ecosystemCard" class="card ecosystem">
      <div class="card-head">
        <h2>Ecosystem 管理区</h2>
        <span class="pill">策略</span>
      </div>
      <div class="card-hint">管理 Follow 与 Grant 的策略，不直接导入技能。</div>
      <div class="panel">
        <div class="row">
          <div style="flex:1; min-width:220px;">
            <label>补充 Follow（可空，逗号分隔）</label>
            <input id="ecoFollowExtra" placeholder="例如 deepseek,cursor" />
          </div>
          <div style="flex:1; min-width:220px;">
            <label>补充 Grant（可空，逗号分隔）</label>
            <input id="ecoGrantExtra" placeholder="例如 deepseek,obsidian" />
          </div>
        </div>
        <div class="btn-row">
          <button class="primary" onclick="saveEcosystem()">保存 Follow/Grant</button>
        </div>
      </div>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>ID</th><th>名称</th><th>类型</th><th>Follow</th><th>Grant</th><th>Auto Sync</th><th>平台</th><th>本地发现</th>
            </tr>
          </thead>
          <tbody id="ecosystemRows"></tbody>
        </table>
      </div>
    </section>
  </div>

  <section id="logBar" class="logbar">
    <div class="logbar-head">
      <h2>实时日志</h2>
      <div class="logbar-tools">
        <button id="logFilterBtn" onclick="toggleLogFilter()">仅看当前任务：关</button>
        <button id="logCollapseBtn" onclick="toggleLogCollapse()">折叠</button>
      </div>
    </div>
    <div class="log-current">当前任务：<span id="logCurrentTask" class="mono">-</span></div>
    <div id="logLines"></div>
  </section>
  <section id="undoBar" class="undo-bar hidden">
    <div id="undoText">待执行操作</div>
    <span id="undoCountdown" class="pill">0s</span>
    <button onclick="undoPendingAction()">撤销</button>
  </section>

  <script>
    let state = {
      overview: null,
      compareSource: '',
      compareItems: [],
      compareSelected: {},
      poolSelected: {},
      driftRows: [],
      jobs: {},
      logEntries: [],
      logFilterCurrent: false,
      logCollapsed: false,
      currentJobKey: '',
      currentJobLabel: '',
      jobStats: { success: 0, error: 0 },
      pendingAction: null,
      undoTicker: null,
      sourceMobileTab: 'form',
    };

    function nowText() { return new Date().toLocaleTimeString(); }
    function applySourceTab() {
      const root = document.getElementById('sourceSideStack');
      const btnForm = document.getElementById('sourceTabForm');
      const btnCompare = document.getElementById('sourceTabCompare');
      if (!root || !btnForm || !btnCompare) return;
      const tab = state.sourceMobileTab === 'compare' ? 'compare' : 'form';
      root.classList.toggle('tab-form', tab === 'form');
      root.classList.toggle('tab-compare', tab === 'compare');
      btnForm.classList.toggle('active', tab === 'form');
      btnCompare.classList.toggle('active', tab === 'compare');
    }
    function setSourceTab(tab) {
      state.sourceMobileTab = tab === 'compare' ? 'compare' : 'form';
      applySourceTab();
    }
    function normalizeCurrentJob() {
      const ids = Object.keys(state.jobs);
      if (!ids.length) {
        state.currentJobKey = '';
        state.currentJobLabel = '';
        return;
      }
      const hasCurrent = ids.some((jobId) => {
        const row = state.jobs[jobId];
        return row && row.key === state.currentJobKey;
      });
      if (hasCurrent) return;
      const first = state.jobs[ids[0]];
      state.currentJobKey = (first && first.key) ? first.key : '';
      state.currentJobLabel = (first && first.label) ? first.label : '';
    }
    function updateTaskPanel() {
      normalizeCurrentJob();
      const running = Object.keys(state.jobs).length;
      const runningEl = document.getElementById('taskRunning');
      const successEl = document.getElementById('taskSuccess');
      const errorEl = document.getElementById('taskError');
      const currentEl = document.getElementById('taskCurrent');
      const logCurrentEl = document.getElementById('logCurrentTask');
      if (runningEl) runningEl.textContent = String(running);
      if (successEl) successEl.textContent = String(state.jobStats.success);
      if (errorEl) errorEl.textContent = String(state.jobStats.error);
      const current = state.currentJobKey || '-';
      if (currentEl) currentEl.textContent = current;
      if (logCurrentEl) logCurrentEl.textContent = current;
      applyLogUiState();
    }
    function visibleLogEntries() {
      if (!state.logFilterCurrent || !state.currentJobKey) return state.logEntries;
      return state.logEntries.filter((x) => !x.jobKey || x.jobKey === state.currentJobKey);
    }
    function applyLogUiState() {
      const bar = document.getElementById('logBar');
      const filterBtn = document.getElementById('logFilterBtn');
      const collapseBtn = document.getElementById('logCollapseBtn');
      if (bar) bar.classList.toggle('collapsed', !!state.logCollapsed);
      if (filterBtn) {
        filterBtn.textContent = `仅看当前任务：${state.logFilterCurrent ? '开' : '关'}`;
        filterBtn.classList.toggle('alt', !!state.logFilterCurrent);
      }
      if (collapseBtn) {
        collapseBtn.textContent = state.logCollapsed ? '展开' : '折叠';
      }
    }
    function renderLog() {
      const box = document.getElementById('logLines');
      if (!box) return;
      const keepBottom = (box.scrollTop + box.clientHeight) >= (box.scrollHeight - 12);
      box.textContent = visibleLogEntries().map((x) => x.line).join('\\n');
      if (keepBottom) {
        box.scrollTop = box.scrollHeight;
      }
    }
    function log(msg, opt = {}) {
      const entry = {
        line: `[${nowText()}] ${msg}`,
        jobKey: String(opt.jobKey || '').trim(),
      };
      state.logEntries.push(entry);
      if (state.logEntries.length > 800) state.logEntries.shift();
      renderLog();
    }
    function toggleLogFilter() {
      state.logFilterCurrent = !state.logFilterCurrent;
      applyLogUiState();
      renderLog();
    }
    function toggleLogCollapse() {
      state.logCollapsed = !state.logCollapsed;
      applyLogUiState();
    }
    async function executePendingAction(pending) {
      if (!pending || pending.done) return;
      pending.done = true;
      if (pending.timer) clearTimeout(pending.timer);
      if (state.pendingAction === pending) {
        state.pendingAction = null;
        renderUndoBar();
      }
      log(`[执行] ${pending.label}`);
      await pending.execute();
    }
    async function flushPendingAction() {
      const pending = state.pendingAction;
      if (!pending) return;
      try {
        await executePendingAction(pending);
      } catch (e) {
        log(`延迟动作失败: ${e.message}`);
      }
    }
    function renderUndoBar() {
      const bar = document.getElementById('undoBar');
      const textEl = document.getElementById('undoText');
      const secEl = document.getElementById('undoCountdown');
      if (!bar || !textEl || !secEl) return;
      const pending = state.pendingAction;
      if (!pending) {
        bar.classList.add('hidden');
        if (state.undoTicker) {
          clearInterval(state.undoTicker);
          state.undoTicker = null;
        }
        return;
      }
      const left = Math.max(0, Math.ceil((pending.deadline - Date.now()) / 1000));
      textEl.textContent = pending.label;
      secEl.textContent = `${left}s`;
      bar.classList.remove('hidden');
      if (!state.undoTicker) {
        state.undoTicker = setInterval(() => {
          if (!state.pendingAction) {
            renderUndoBar();
            return;
          }
          const remain = Math.max(0, Math.ceil((state.pendingAction.deadline - Date.now()) / 1000));
          const timerEl = document.getElementById('undoCountdown');
          if (timerEl) timerEl.textContent = `${remain}s`;
        }, 250);
      }
    }
    async function scheduleUndoableAction(label, execute, delayMs = 5000) {
      await flushPendingAction();
      const pending = {
        label,
        execute,
        deadline: Date.now() + delayMs,
        timer: null,
        done: false,
      };
      pending.timer = setTimeout(() => {
        void executePendingAction(pending).catch((e) => {
          log(`延迟动作失败: ${e.message}`);
        });
      }, delayMs);
      state.pendingAction = pending;
      renderUndoBar();
      log(`[可撤销] ${label}（${Math.ceil(delayMs / 1000)} 秒内）`);
    }
    function undoPendingAction() {
      const pending = state.pendingAction;
      if (!pending) return;
      pending.done = true;
      if (pending.timer) clearTimeout(pending.timer);
      state.pendingAction = null;
      renderUndoBar();
      log(`[已撤销] ${pending.label}`);
    }

    function applyEcosystemVisibility(hidden) {
      const card = document.getElementById('ecosystemCard');
      const btn = document.getElementById('ecoToggleBtn');
      if (!card || !btn) return;
      card.classList.toggle('hidden', !!hidden);
      btn.textContent = hidden ? '显示高级区' : '隐藏高级区';
      localStorage.setItem('skillctl.ecos_hidden', hidden ? '1' : '0');
    }

    function toggleEcosystem() {
      const card = document.getElementById('ecosystemCard');
      if (!card) return;
      const hidden = !card.classList.contains('hidden');
      applyEcosystemVisibility(hidden);
    }

    function dedupe(items) {
      const seen = new Set();
      const out = [];
      for (const raw of items || []) {
        const key = String(raw || '').trim().toLowerCase();
        if (!key || seen.has(key)) continue;
        seen.add(key);
        out.push(key);
      }
      return out;
    }
    function parseCsv(text) { return text ? dedupe(text.split(',')) : []; }
    function isRemoteSource(text) {
      const s = String(text || '').trim().toLowerCase();
      return s.startsWith('http://') || s.startsWith('https://') || s.startsWith('git@') || s.endsWith('.git');
    }
    function escapeHtml(text) {
      return String(text ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    async function api(path, method='GET', body=null) {
      const opt = { method, headers: {} };
      if (body !== null) {
        opt.headers['Content-Type'] = 'application/json';
        opt.body = JSON.stringify(body);
      }
      const resp = await fetch(path, opt);
      const text = await resp.text();
      let data = null;
      try { data = JSON.parse(text); } catch (_e) { data = { raw: text }; }
      if (!resp.ok) {
        const detail = data && (data.detail || data.raw) ? (data.detail || data.raw) : resp.statusText;
        throw new Error(detail);
      }
      return data;
    }

    function watchJob(jobId, label) {
      if (!jobId || state.jobs[jobId]) return;
      const shortId = jobId.slice(0, 8);
      const jobKey = `${label}#${shortId}`;
      state.jobs[jobId] = { cursor: 0, busy: false, timer: null, key: jobKey, label };
      state.currentJobKey = jobKey;
      state.currentJobLabel = label;
      updateTaskPanel();
      log(`[任务提交] ${label} #${shortId}`, { jobKey });

      const tick = async () => {
        const w = state.jobs[jobId];
        if (!w || w.busy) return;
        w.busy = true;
        try {
          const data = await api(`/api/jobs/${encodeURIComponent(jobId)}?cursor=${w.cursor}`);
          for (const line of data.logs || []) log(`[${jobKey}] ${line}`, { jobKey });
          w.cursor = data.next_cursor || w.cursor;
          if (data.status === 'success' || data.status === 'error') {
            clearInterval(w.timer);
            delete state.jobs[jobId];
            if (data.status === 'success') state.jobStats.success += 1;
            else state.jobStats.error += 1;
            if (data.status === 'success' && data.result) {
              const result = data.result;
              if (result.compare && Array.isArray(result.items)) {
                state.compareItems = result.items;
                state.compareSource = result.source || state.compareSource;
                state.compareSelected = {};
                renderCompareRows();
                log(`[差异结果] 来源 ${result.total} 条，新增 ${result.new_total} 条`, { jobKey });
              }
              if (Array.isArray(result.rows) && result.counts_by_agent !== undefined) {
                state.driftRows = result.rows;
                renderDriftRows();
                log(`[Check结果] 共 ${result.total ?? result.rows.length} 条`, { jobKey });
              }
              if (result.success_total !== undefined && result.failed_total !== undefined) {
                log(`[纳入结果] 成功 ${result.success_total}，失败 ${result.failed_total}`, { jobKey });
              }
            }
            log(`[任务结束] ${label} #${shortId} -> ${data.status}`, { jobKey });
            if (data.error) log(`[任务错误] ${label} #${shortId}: ${data.error}`, { jobKey });
            updateTaskPanel();
            renderLog();
            await refreshAll();
          }
        } catch (e) {
          clearInterval(w.timer);
          delete state.jobs[jobId];
          state.jobStats.error += 1;
          log(`[任务异常] ${label} #${shortId}: ${e.message}`, { jobKey });
          updateTaskPanel();
          renderLog();
        } finally {
          if (state.jobs[jobId]) state.jobs[jobId].busy = false;
        }
      };
      state.jobs[jobId].timer = setInterval(() => { void tick(); }, 900);
      void tick();
    }

    function renderOverview(data) {
      const top = (data.topology && data.topology.summary) ? data.topology.summary : {};
      const inventory = data.inventory || {};
      const proxy = (data.network && data.network.proxy_url) ? data.network.proxy_url : '(未设置)';

      document.getElementById('summaryScanTime').textContent = inventory.scanned_at
        ? `扫描 ${inventory.scanned_at}`
        : '尚未扫描';
      document.getElementById('kpiPoolTotal').textContent = String(data.pool ? data.pool.total : 0);
      document.getElementById('kpiSourceTotal').textContent = `${top.source_tracked || 0}/${top.source_total || 0}`;
      document.getElementById('kpiTargetTotal').textContent = `${top.target_granted || 0}/${top.target_total || 0}`;
      document.getElementById('kpiDriftTotal').textContent = String(top.drift_total || 0);
      document.getElementById('summaryPoolDir').textContent = data.pool_dir || '-';
      document.getElementById('summaryProxy').textContent = proxy;

      const chips = (data.pool && Array.isArray(data.pool.sources) ? data.pool.sources : [])
        .slice(0, 12)
        .map((x) => `<span class="mini">${escapeHtml(x.count)} · ${escapeHtml(x.source)}</span>`)
        .join('');
      document.getElementById('poolSourceChips').innerHTML = chips || '<span class="hint">暂无来源统计</span>';
    }

    function renderTopology(data) {
      const rows = (data.topology && data.topology.rows) ? data.topology.rows : [];
      const tbody = document.getElementById('topologyRows');
      const countEl = document.getElementById('topologyCount');
      if (countEl) countEl.textContent = `${rows.length} 行`;
      tbody.innerHTML = rows.map((x) => {
        const layer = x.layer === 'source'
          ? '<span class="pill">SOURCE</span>'
          : '<span class="pill">POOL</span>';
        const drift = String(x.drift) === '0'
          ? '<span class="pill">0</span>'
          : (String(x.drift) === '-' ? '-' : `<span class="pill warn">${escapeHtml(x.drift)}</span>`);
        return `<tr>
          <td>${layer}</td>
          <td class="mono">${escapeHtml(x.id)}</td>
          <td>${escapeHtml(x.name)}</td>
          <td class="mono">${escapeHtml(x.owner || '-')}</td>
          <td class="mono">${escapeHtml(x.location || '-')}</td>
          <td>${escapeHtml(x.skills)}</td>
          <td>${escapeHtml(x.status || '-')}</td>
          <td>${drift}</td>
          <td class="mono">${escapeHtml(x.updated_at || '-')}</td>
        </tr>`;
      }).join('');
    }

    function currentPoolRows() {
      const data = state.overview || {};
      const allRows = (data.pool && Array.isArray(data.pool.skills)) ? data.pool.skills : [];
      const keyword = (document.getElementById('poolFilter').value || '').trim().toLowerCase();
      if (!keyword) return allRows;
      return allRows.filter((x) => {
        const raw = `${x.id} ${x.name} ${x.source}`.toLowerCase();
        return raw.includes(keyword);
      });
    }

    function renderPoolRows() {
      const rows = currentPoolRows();
      const tbody = document.getElementById('poolRows');
      tbody.innerHTML = rows.map((x) => `<tr>
        <td><input type="checkbox" ${state.poolSelected[x.id] ? 'checked' : ''} onchange="togglePoolSkill('${encodeURIComponent(x.id)}', this.checked)" /></td>
        <td class="mono">${escapeHtml(x.id)}</td>
        <td>${escapeHtml(x.name)}</td>
        <td class="mono">${escapeHtml(x.source)}</td>
        <td>${escapeHtml(x.distributed_text || '-')}</td>
        <td class="mono">${escapeHtml(x.updated_at || '-')}</td>
      </tr>`).join('');
      updatePoolSelectionHint();
    }

    function updatePoolSelectionHint() {
      const count = Object.keys(state.poolSelected).length;
      document.getElementById('poolSelectionHint').textContent = `已选 ${count} 项`;
      document.getElementById('syncHint').textContent = count > 0
        ? `将同步 ${count} 个选中 skill（未选则表示全量）。`
        : '当前未选 skill，将执行全量同步。';
    }

    function togglePoolSkill(encodedSkillId, checked) {
      const skillId = decodeURIComponent(encodedSkillId);
      if (checked) state.poolSelected[skillId] = true;
      else delete state.poolSelected[skillId];
      updatePoolSelectionHint();
    }

    function selectVisiblePoolSkills() {
      for (const row of currentPoolRows()) state.poolSelected[row.id] = true;
      renderPoolRows();
    }

    function clearPoolSelection() {
      state.poolSelected = {};
      renderPoolRows();
    }

    function renderProxy(data) {
      const network = data.network || {};
      document.getElementById('proxyUrl').value = network.proxy_url || '';
      document.getElementById('proxyNoProxy').value = network.no_proxy || '';
    }

    function renderSourcePreset(data) {
      const rows = data.github_sources || [];
      const options = ['<option value="">(手动输入)</option>'].concat(
        rows.map((x) => `<option value="${escapeHtml(x.source)}">${escapeHtml(x.name)} (${escapeHtml(x.id)})</option>`)
      );
      document.getElementById('compareSourcePreset').innerHTML = options.join('');
    }

    function renderTracks(data) {
      const rows = (data.github_sources || []).filter((x) => x.tracked);
      const tbody = document.getElementById('trackRows');
      tbody.innerHTML = rows.map((x) => {
        const snap = x.snapshot_total === null || x.snapshot_total === undefined
          ? '-'
          : `${x.snapshot_total} @ ${x.snapshot_checked_at || '-'}`;
        return `<tr>
          <td class="mono">${escapeHtml(x.id)}</td>
          <td>${escapeHtml(x.name || x.id)}</td>
          <td class="mono">${escapeHtml(x.agent_id || '-')}</td>
          <td class="mono">${escapeHtml(x.source)}</td>
          <td class="mono">${escapeHtml(snap)}</td>
          <td>
            <div class="track-actions">
              <button class="btn-compare" title="将该来源填入下方比较输入框" onclick="useSourceForCompare('${encodeURIComponent(x.source)}')">用于比较</button>
              <details class="track-menu">
                <summary>更多</summary>
                <div class="track-menu-items">
                  <button class="btn-check" title="检查该来源与上次快照的新增/移除差异" onclick="checkTrack('${encodeURIComponent(x.id)}')">检查</button>
                  <button class="btn-import" title="将该来源的全部技能导入 Pool" onclick="importTrackAll('${encodeURIComponent(x.id)}')">全量导入</button>
                  <button class="btn-delete" title="删除这条来源跟踪记录" onclick="removeTrack('${encodeURIComponent(x.id)}')">删除</button>
                </div>
              </details>
            </div>
          </td>
        </tr>`;
      }).join('');
    }

    function renderCompareRows() {
      const tbody = document.getElementById('compareRows');
      const rows = state.compareItems || [];
      tbody.innerHTML = rows.map((x) => {
        const disabled = x.in_pool ? 'disabled' : '';
        const checked = state.compareSelected[x.rel_dir] ? 'checked' : '';
        const status = x.in_pool ? '<span class="pill">已在Pool</span>' : '<span class="pill warn">新增候选</span>';
        return `<tr>
          <td><input type="checkbox" ${checked} ${disabled} onchange="toggleCompareSkill('${encodeURIComponent(x.rel_dir)}', this.checked)" /></td>
          <td>${escapeHtml(x.name)}</td>
          <td class="mono">${escapeHtml(x.rel_dir)}</td>
          <td>${status}</td>
          <td>${x.in_pool ? '-' : `<button onclick="importOneCandidate('${encodeURIComponent(x.rel_dir)}')">导入</button>`}</td>
        </tr>`;
      }).join('');
    }

    function toggleCompareSkill(encodedRelDir, checked) {
      const rel = decodeURIComponent(encodedRelDir);
      if (checked) state.compareSelected[rel] = true;
      else delete state.compareSelected[rel];
    }

    function useSourceForCompare(encodedSource) {
      const source = decodeURIComponent(encodedSource);
      document.getElementById('compareSourceInput').value = source;
      state.compareSource = source;
      setSourceTab('compare');
      log('已选择 source: ' + source);
    }

    function renderSyncTargets(data) {
      const rows = (data.ecosystem && data.ecosystem.rows ? data.ecosystem.rows : []).filter((x) => x.can_consume);
      const holder = document.getElementById('syncTargets');
      holder.innerHTML = rows.map((x) => `
        <label>
          <input type="checkbox" data-role="sync-target" data-id="${escapeHtml(x.id)}" ${x.granted ? 'checked' : ''} />
          ${escapeHtml(x.name || x.id)}
        </label>
      `).join('');
    }

    function renderEcosystem(data) {
      const rows = (data.ecosystem && data.ecosystem.rows) ? data.ecosystem.rows : [];
      const tbody = document.getElementById('ecosystemRows');
      tbody.innerHTML = rows.map((x) => {
        const ready = x.auto_sync_ready ? '<span class="pill">ready</span>' : '<span class="pill warn">no</span>';
        const followDisabled = x.can_generate ? '' : 'disabled';
        const grantDisabled = x.can_consume ? '' : 'disabled';
        return `<tr>
          <td class="mono">${escapeHtml(x.id)}</td>
          <td>${escapeHtml(x.name || x.id)}</td>
          <td>${escapeHtml(x.kind || '-')}</td>
          <td><input type="checkbox" data-role="follow" data-id="${escapeHtml(x.id)}" ${x.followed ? 'checked' : ''} ${followDisabled} /></td>
          <td><input type="checkbox" data-role="grant" data-id="${escapeHtml(x.id)}" ${x.granted ? 'checked' : ''} ${grantDisabled} /></td>
          <td>${ready}</td>
          <td class="mono">${escapeHtml(x.sync_platform || '-')}</td>
          <td>${escapeHtml(String(x.discovered_count ?? 0))}</td>
        </tr>`;
      }).join('');
    }

    function renderDriftRows() {
      const tbody = document.getElementById('driftRows');
      const rows = state.driftRows || [];
      const countEl = document.getElementById('driftCount');
      if (countEl) countEl.textContent = `${rows.length} 条`;
      tbody.innerHTML = rows.map((x) => `<tr>
        <td>${escapeHtml(x.agent)}</td>
        <td>${escapeHtml(x.name)}</td>
        <td class="mono">${escapeHtml(x.skill_dir)}</td>
        <td>${escapeHtml(x.reason || '')}</td>
        <td><button onclick="promoteDrift('${encodeURIComponent(x.skill_dir)}')">纳入 Pool</button></td>
      </tr>`).join('');
    }

    async function refreshAll() {
      try {
        const data = await api('/api/overview');
        state.overview = data;
        renderOverview(data);
        renderTopology(data);
        renderProxy(data);
        renderSourcePreset(data);
        renderTracks(data);
        renderSyncTargets(data);
        renderEcosystem(data);
        renderPoolRows();
        if (!state.compareSource && Array.isArray(data.github_sources) && data.github_sources.length) {
          state.compareSource = data.github_sources[0].source || '';
          document.getElementById('compareSourceInput').value = state.compareSource;
        }
        if (data.drift && Array.isArray(data.drift.rows)) {
          state.driftRows = data.drift.rows;
          renderDriftRows();
        }
        updateTaskPanel();
        applySourceTab();
      } catch (e) {
        log('刷新失败: ' + e.message);
      }
    }

    async function runScan() {
      try {
        const data = await api('/api/scan', 'POST', { async: true });
        if (data.accepted && data.job_id) return watchJob(data.job_id, '扫描');
        log(`扫描完成: ${data.total} 条`);
        await refreshAll();
      } catch (e) {
        log('扫描失败: ' + e.message);
      }
    }

    async function saveProxy() {
      const proxy_url = document.getElementById('proxyUrl').value.trim();
      const no_proxy = document.getElementById('proxyNoProxy').value.trim();
      try {
        const data = await api('/api/proxy', 'POST', { proxy_url, no_proxy });
        log(`代理已保存: ${data.proxy_url || '(未设置)'}`);
        await refreshAll();
      } catch (e) {
        log('保存代理失败: ' + e.message);
      }
    }

    async function clearProxy() {
      try {
        await api('/api/proxy', 'POST', { proxy_url: '', no_proxy: '' });
        log('代理已清空。');
        await refreshAll();
      } catch (e) {
        log('清空代理失败: ' + e.message);
      }
    }

    async function runSourceCompare() {
      const source = (document.getElementById('compareSourceInput').value.trim()
        || document.getElementById('compareSourcePreset').value.trim()
        || state.compareSource).trim();
      if (!source) return log('请先提供 source。');
      if (!isRemoteSource(source)) return log('当前策略只接受 GitHub/Git 远程源。');
      state.compareSource = source;
      document.getElementById('compareSourceInput').value = source;
      setSourceTab('compare');
      try {
        const data = await api('/api/source/compare', 'POST', { source, async: true });
        if (data.accepted && data.job_id) return watchJob(data.job_id, 'source比较');
        state.compareItems = data.items || [];
        state.compareSelected = {};
        renderCompareRows();
      } catch (e) {
        log('比较失败: ' + e.message);
      }
    }

    async function importOneCandidate(encodedRel) {
      const rel = decodeURIComponent(encodedRel);
      const source = (state.compareSource || '').trim();
      if (!source) return log('请先比较来源。');
      try {
        const data = await api('/api/source/import', 'POST', { source, skills: [rel], async: true });
        if (data.accepted && data.job_id) return watchJob(data.job_id, `导入:${rel}`);
      } catch (e) {
        log('导入失败: ' + e.message);
      }
    }

    async function importSelectedCandidates() {
      const source = (state.compareSource || '').trim();
      const selected = Object.keys(state.compareSelected);
      if (!source) return log('请先比较来源。');
      if (!selected.length) return log('请先选中候选 skill。');
      await scheduleUndoableAction(`导入 ${selected.length} 个候选 skill`, async () => {
        const data = await api('/api/source/import', 'POST', { source, skills: selected, async: true });
        if (data.accepted && data.job_id) watchJob(data.job_id, '导入候选');
      });
    }

    async function importAllNewCandidates() {
      const source = (state.compareSource || '').trim();
      if (!source) return log('请先比较来源。');
      const selected = (state.compareItems || []).filter((x) => !x.in_pool).map((x) => x.rel_dir);
      if (!selected.length) return log('当前没有新增候选。');
      await scheduleUndoableAction(`导入全部新增候选（${selected.length} 项）`, async () => {
        const data = await api('/api/source/import', 'POST', { source, skills: selected, async: true });
        if (data.accepted && data.job_id) watchJob(data.job_id, '导入新增候选');
      });
    }

    async function addTrack() {
      const body = {
        agent: document.getElementById('trackAgent').value.trim().toLowerCase(),
        name: document.getElementById('trackName').value.trim(),
        source: document.getElementById('trackSource').value.trim(),
        id: document.getElementById('trackId').value.trim() || null,
        enabled: document.getElementById('trackEnabled').checked,
        auto_follow: document.getElementById('trackAutoFollow').checked,
      };
      if (!body.agent || !body.name || !body.source) return log('agent/name/source 不能为空。');
      if (!isRemoteSource(body.source)) return log('source 必须是 GitHub/Git 远程地址。');
      try {
        const data = await api('/api/tracks', 'POST', {
          agent: body.agent, name: body.name, source: body.source, market: null,
          id: body.id, enabled: body.enabled, auto_follow: body.auto_follow
        });
        log('已新增 Source: ' + data.track.id);
        document.getElementById('trackSource').value = '';
        await refreshAll();
      } catch (e) {
        log('新增 Source 失败: ' + e.message);
      }
    }

    async function removeTrack(encodedId) {
      const id = decodeURIComponent(encodedId);
      await scheduleUndoableAction(`删除 Source: ${id}`, async () => {
        await api('/api/tracks/' + encodeURIComponent(id), 'DELETE');
        log('已删除 Source: ' + id);
        await refreshAll();
      });
    }

    async function checkTrack(encodedId) {
      const id = decodeURIComponent(encodedId);
      try {
        const data = await api('/api/tracks/check', 'POST', {
          id, include_disabled: true, update_snapshot: true, show_all: false, async: true,
        });
        if (data.accepted && data.job_id) return watchJob(data.job_id, `检查:${id}`);
      } catch (e) {
        log('检查失败: ' + e.message);
      }
    }

    async function importTrackAll(encodedId) {
      const id = decodeURIComponent(encodedId);
      await scheduleUndoableAction(`全量导入 Source: ${id}`, async () => {
        const data = await api('/api/tracks/import', 'POST', { id, all: true, async: true });
        if (data.accepted && data.job_id) watchJob(data.job_id, `源导入:${id}`);
      });
    }

    function selectedSyncTargets() {
      return Array.from(document.querySelectorAll('#syncTargets input[data-role="sync-target"]:checked'))
        .map((el) => String(el.getAttribute('data-id') || '').trim())
        .filter(Boolean);
    }

    async function runSyncSelected() {
      const targets = selectedSyncTargets();
      const prune = document.getElementById('syncPrune').checked;
      const skills = Object.keys(state.poolSelected);
      if (!targets.length) return log('请至少选择一个目标 Agent。');
      try {
        const data = await api('/api/sync', 'POST', {
          dry_run: false,
          prune,
          only: null,
          targets,
          skills,
          async: true,
        });
        if (data.accepted && data.job_id) return watchJob(data.job_id, '同步');
        log('同步返回码: ' + data.returncode);
      } catch (e) {
        log('同步失败: ' + e.message);
      }
    }

    async function saveEcosystem() {
      const follow = Array.from(document.querySelectorAll('#ecosystemRows input[data-role="follow"]:checked'))
        .map((el) => el.getAttribute('data-id') || '');
      const grant = Array.from(document.querySelectorAll('#ecosystemRows input[data-role="grant"]:checked'))
        .map((el) => el.getAttribute('data-id') || '');
      const followExtra = parseCsv(document.getElementById('ecoFollowExtra').value.trim());
      const grantExtra = parseCsv(document.getElementById('ecoGrantExtra').value.trim());
      try {
        const data = await api('/api/ecosystem', 'POST', {
          follow_sources: dedupe(follow.concat(followExtra)),
          grant_targets: dedupe(grant.concat(grantExtra)),
        });
        log(`Ecosystem 已保存: follow=${data.follow_sources.length}, grant=${data.grant_targets.length}`);
        document.getElementById('ecoFollowExtra').value = '';
        document.getElementById('ecoGrantExtra').value = '';
        await refreshAll();
      } catch (e) {
        log('保存 Ecosystem 失败: ' + e.message);
      }
    }

    async function runDriftCheck() {
      const rescan = true;
      try {
        const data = await api('/api/check/drift', 'POST', { rescan, async: true });
        if (data.accepted && data.job_id) return watchJob(data.job_id, '扫描并检查');
        state.driftRows = data.rows || [];
        renderDriftRows();
        log(`扫描并检查完成: ${data.total} 条`);
      } catch (e) {
        log('扫描并检查失败: ' + e.message);
      }
    }

    async function promoteDrift(encodedPath) {
      const path = decodeURIComponent(encodedPath);
      try {
        const data = await api('/api/promote/agent-skill', 'POST', { path, async: true });
        if (data.accepted && data.job_id) return watchJob(data.job_id, '纳入Pool');
        log(`纳入完成: ${data.destination || path}`);
        await refreshAll();
      } catch (e) {
        log('纳入失败: ' + e.message);
      }
    }

    async function promoteAllDrift() {
      const paths = dedupe((state.driftRows || []).map((x) => String(x.skill_dir || '').trim()));
      if (!paths.length) return log('当前没有可纳入 Pool 的本地技能。');
      try {
        const data = await api('/api/promote/agent-skills', 'POST', { paths, async: true });
        if (data.accepted && data.job_id) return watchJob(data.job_id, '批量纳入Pool');
        log(`批量纳入完成: 成功 ${data.success_total || 0}, 失败 ${data.failed_total || 0}`);
        await refreshAll();
      } catch (e) {
        log('批量纳入失败: ' + e.message);
      }
    }

    document.getElementById('compareSourcePreset').addEventListener('change', (e) => {
      const value = e.target.value || '';
      if (!value) return;
      document.getElementById('compareSourceInput').value = value;
      state.compareSource = value;
      setSourceTab('compare');
    });

    document.getElementById('poolFilter').addEventListener('input', () => renderPoolRows());

    applyEcosystemVisibility(localStorage.getItem('skillctl.ecos_hidden') === '1');
    applySourceTab();
    updateTaskPanel();
    applyLogUiState();
    renderUndoBar();
    renderLog();
    refreshAll();
  </script>
</body>
</html>"""


def create_app() -> FastAPI:
    app = FastAPI(title="skillctl-web", version="0.1.0")

    @app.get("/", response_class=HTMLResponse)
    def index_page() -> str:
        return _page_html()

    @app.get("/api/overview")
    def api_overview() -> dict[str, Any]:
        return _overview_payload(_cfg())

    @app.get("/api/markets")
    def api_markets() -> dict[str, Any]:
        return {"items": [asdict(x) for x in list_market_views(include_missing=True)]}

    @app.get("/api/jobs/{job_id}")
    def api_job_detail(job_id: str, cursor: int = 0) -> dict[str, Any]:
        job = _get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"未找到任务: {job_id}")
        return _job_snapshot(job, cursor=cursor)

    @app.post("/api/scan")
    def api_scan(req: ScanRequest | None = None) -> dict[str, Any]:
        req = req or ScanRequest()
        cfg = _cfg()
        if req.run_async:
            job_id = _start_job("scan", lambda log: _do_scan(cfg, log=log))
            return {"ok": True, "accepted": True, "job_id": job_id}
        return _do_scan(cfg)

    @app.post("/api/sync")
    def api_sync(req: SyncRequest) -> dict[str, Any]:
        cfg = _cfg()
        if req.run_async:
            job_id = _start_job("sync", lambda log: _do_sync(cfg, req, log=log))
            return {"ok": True, "accepted": True, "job_id": job_id}
        return _do_sync(cfg, req)

    @app.post("/api/index")
    def api_index(req: IndexRequest) -> dict[str, Any]:
        cfg = _cfg()
        if req.run_async:
            job_id = _start_job("index", lambda log: _do_index(cfg, req, log=log))
            return {"ok": True, "accepted": True, "job_id": job_id}
        return _do_index(cfg, req)

    @app.post("/api/source/compare")
    def api_source_compare(req: SourceCompareRequest) -> dict[str, Any]:
        cfg = _cfg()
        try:
            if req.run_async:
                job_id = _start_job("source-compare", lambda log: _do_source_compare(cfg, req, log=log))
                return {"ok": True, "accepted": True, "job_id": job_id}
            return _do_source_compare(cfg, req)
        except (RuntimeError, ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/source/import")
    def api_source_import(req: SourceImportRequest) -> dict[str, Any]:
        cfg = _cfg()
        try:
            if req.run_async:
                job_id = _start_job("source-import", lambda log: _do_source_import(cfg, req, log=log))
                return {"ok": True, "accepted": True, "job_id": job_id}
            return _do_source_import(cfg, req)
        except (RuntimeError, ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/fetch")
    def api_fetch(req: FetchRequest) -> dict[str, Any]:
        cfg = _cfg()
        try:
            if req.run_async:
                job_id = _start_job("fetch", lambda log: _do_fetch(cfg, req, log=log))
                return {"ok": True, "accepted": True, "job_id": job_id}
            return _do_fetch(cfg, req)
        except (RuntimeError, ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/tracks")
    def api_tracks() -> dict[str, Any]:
        cfg = _cfg()
        return {"items": [asdict(x) for x in list_tracked_sources(cfg, only_enabled=False)]}

    @app.post("/api/tracks")
    def api_track_add(req: TrackCreateRequest) -> dict[str, Any]:
        cfg = _cfg()
        source, market = _resolve_source(req.source, req.market)
        if not _is_remote_source(source):
            raise HTTPException(status_code=400, detail="当前策略仅支持 GitHub/Git 远程 source。")
        try:
            row = add_tracked_source(
                cfg=cfg,
                agent_id=req.agent,
                name=req.name,
                source=source,
                source_id=req.source_id,
                enabled=req.enabled,
                note=req.note or "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if req.auto_follow:
            cfg.ecosystem.follow_sources = normalize_follow_sources(cfg.follow_sources + [row.agent_id])
        save_config(cfg)
        return {
            "ok": True,
            "track": asdict(row),
            "market": market,
        }

    @app.delete("/api/tracks/{track_id}")
    def api_track_remove(track_id: str) -> dict[str, Any]:
        cfg = _cfg()
        ok = remove_tracked_source(cfg, track_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"未找到 tracked source: {track_id}")
        save_config(cfg)
        return {"ok": True}

    @app.post("/api/tracks/check")
    def api_track_check(req: TrackCheckRequest) -> dict[str, Any]:
        cfg = _cfg()
        try:
            if req.run_async:
                job_id = _start_job("track-check", lambda log: _do_track_check(cfg, req, log=log))
                return {"ok": True, "accepted": True, "job_id": job_id}
            return _do_track_check(cfg, req)
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/tracks/import")
    def api_track_import(req: TrackImportRequest) -> dict[str, Any]:
        cfg = _cfg()
        try:
            if req.run_async:
                job_id = _start_job("track-import", lambda log: _do_track_import(cfg, req, log=log))
                return {"ok": True, "accepted": True, "job_id": job_id}
            return _do_track_import(cfg, req)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/check/drift")
    def api_check_drift(req: DriftCheckRequest | None = None) -> dict[str, Any]:
        cfg = _cfg()
        req = req or DriftCheckRequest()
        try:
            if req.run_async:
                job_id = _start_job("drift-check", lambda log: _do_drift_check(cfg, req, log=log))
                return {"ok": True, "accepted": True, "job_id": job_id}
            return _do_drift_check(cfg, req)
        except (RuntimeError, ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/promote/agent-skill")
    def api_promote_agent_skill(req: PromoteAgentSkillRequest) -> dict[str, Any]:
        cfg = _cfg()
        try:
            if req.run_async:
                job_id = _start_job("promote-agent-skill", lambda log: _do_promote_agent_skill(cfg, req, log=log))
                return {"ok": True, "accepted": True, "job_id": job_id}
            return _do_promote_agent_skill(cfg, req)
        except (RuntimeError, ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/promote/agent-skills")
    def api_promote_agent_skills(req: PromoteAgentSkillsRequest) -> dict[str, Any]:
        cfg = _cfg()
        try:
            if req.run_async:
                job_id = _start_job("promote-agent-skills", lambda log: _do_promote_agent_skills(cfg, req, log=log))
                return {"ok": True, "accepted": True, "job_id": job_id}
            return _do_promote_agent_skills(cfg, req)
        except (RuntimeError, ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/ecosystem")
    def api_ecosystem_update(req: EcosystemUpdateRequest) -> dict[str, Any]:
        cfg = _cfg()
        changed = False
        if req.follow_sources is not None:
            cfg.ecosystem.follow_sources = normalize_follow_sources(req.follow_sources)
            changed = True
        if req.grant_targets is not None:
            cfg.ecosystem.grant_targets = normalize_grant_targets(req.grant_targets)
            changed = True
        if changed:
            save_config(cfg)
        return {"ok": True, **_overview_payload(cfg)["ecosystem"]}

    @app.post("/api/proxy")
    def api_proxy_update(req: ProxyUpdateRequest) -> dict[str, Any]:
        cfg = _cfg()
        changed = False
        if req.proxy_url is not None:
            cfg.network.proxy_url = str(req.proxy_url).strip()
            changed = True
        if req.no_proxy is not None:
            cfg.network.no_proxy = str(req.no_proxy).strip()
            changed = True
        if changed:
            save_config(cfg)
        return {"ok": True, **_overview_payload(cfg)["network"]}

    return app


def run_web(host: str = "127.0.0.1", port: int = 8765) -> None:
    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level="info")
