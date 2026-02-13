from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

from .markets import list_market_views
from .promote import promote_skill
from .scanner import parse_frontmatter


@dataclass
class IndexedSkill:
    name: str
    description: str
    skill_dir: str
    skill_md: str
    rel_dir: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "description": self.description,
            "skill_dir": self.skill_dir,
            "skill_md": self.skill_md,
            "rel_dir": self.rel_dir,
        }


def _is_git_source(source: str) -> bool:
    s = source.strip()
    return (
        s.startswith("http://")
        or s.startswith("https://")
        or s.startswith("git@")
        or s.endswith(".git")
    )


def _build_git_env(proxy: str | None = None, no_proxy: str | None = None) -> dict[str, str] | None:
    proxy_url = (proxy or "").strip()
    no_proxy_value = (no_proxy or "").strip()
    if not proxy_url and not no_proxy_value:
        return None

    env = dict(os.environ)
    if proxy_url:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env[key] = proxy_url
    if no_proxy_value:
        env["NO_PROXY"] = no_proxy_value
        env["no_proxy"] = no_proxy_value
    return env


@contextmanager
def materialize_source(
    source: str,
    proxy: str | None = None,
    no_proxy: str | None = None,
    log: Callable[[str], None] | None = None,
) -> Iterator[Path]:
    src = Path(source).expanduser()
    if src.exists() and src.is_dir():
        if log:
            log(f"使用本地来源: {src.resolve()}")
        yield src.resolve()
        return

    if not _is_git_source(source):
        raise FileNotFoundError(f"source 不存在且不是可识别的 git 地址: {source}")

    tmp_root = Path(tempfile.mkdtemp(prefix="skillctl-src-"))
    repo_path = tmp_root / "repo"
    env = _build_git_env(proxy=proxy, no_proxy=no_proxy)
    try:
        cmd = ["git", "clone", "--depth", "1", "--progress", source, str(repo_path)]
        if log:
            log(f"开始克隆远程来源: {source}")
            proc = subprocess.Popen(
                cmd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
            )
            lines: list[str] = []
            assert proc.stdout is not None
            for raw in proc.stdout:
                lines.append(raw)
                line = raw.rstrip()
                if line:
                    log(line)
            proc.wait()
            if proc.returncode != 0:
                tail = "".join(lines[-20:]).strip()
                raise RuntimeError(f"git clone 失败: {tail}")
            log("git clone 完成。")
        else:
            proc = subprocess.run(cmd, text=True, capture_output=True, check=False, env=env)
            if proc.returncode != 0:
                raise RuntimeError(f"git clone 失败: {proc.stderr.strip()}")
        yield repo_path
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def index_repo(source_root: Path) -> list[IndexedSkill]:
    rows: list[IndexedSkill] = []
    for skill_md in sorted(source_root.rglob("SKILL.md")):
        # 只索引目录型 skill
        skill_dir = skill_md.parent
        rel_dir = str(skill_dir.relative_to(source_root))
        name, desc = parse_frontmatter(skill_md)
        rows.append(
            IndexedSkill(
                name=name or skill_dir.name,
                description=desc or "",
                skill_dir=str(skill_dir),
                skill_md=str(skill_md),
                rel_dir=rel_dir,
            )
        )
    return rows


def index_source(
    source: str,
    proxy: str | None = None,
    no_proxy: str | None = None,
    log: Callable[[str], None] | None = None,
) -> tuple[Path, list[IndexedSkill]]:
    with materialize_source(source, proxy=proxy, no_proxy=no_proxy, log=log) as root:
        rows = index_repo(root)
        # materialize_source 若是远程会在退出 context 后清理，因此这里需要重新返回临时根路径信息不适用
        # index_source 只用于展示，不依赖后续文件读取，返回 root 的字符串副本即可。
        if log:
            log(f"索引完成，发现 {len(rows)} 个 skill。")
        return Path(str(root)), rows


def discover_market_sources() -> list[Path]:
    rows: list[Path] = []
    seen: set[str] = set()
    for item in list_market_views(include_missing=False):
        if _is_git_source(item.source):
            continue
        path = Path(item.source)
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        rows.append(path)
    return rows


def write_index_report(pool_dir: Path, source: str, items: list[IndexedSkill]) -> dict[str, Path]:
    state_dir = pool_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    latest_json = state_dir / "index-latest.json"
    snapshot_json = state_dir / f"index-{ts}.json"

    payload = {
        "indexed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": source,
        "total": len(items),
        "items": [x.to_dict() for x in items],
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    latest_json.write_text(raw, encoding="utf-8")
    snapshot_json.write_text(raw, encoding="utf-8")
    return {"latest_json": latest_json, "snapshot_json": snapshot_json}


def _select_items(items: list[IndexedSkill], selector: str | None, fetch_all: bool) -> list[IndexedSkill]:
    if fetch_all:
        return items
    if not selector:
        raise ValueError("未提供 --skill，且未设置 --all。")

    exact_rel = [x for x in items if x.rel_dir == selector]
    if exact_rel:
        return exact_rel

    by_name = [x for x in items if x.name == selector]
    if len(by_name) == 1:
        return by_name
    if len(by_name) > 1:
        matches = ", ".join(x.rel_dir for x in by_name)
        raise ValueError(f"名称重复，请改用 --skill 指定 rel_dir。候选: {matches}")

    end_match = [x for x in items if x.rel_dir.endswith(selector)]
    if len(end_match) == 1:
        return end_match
    raise ValueError("未找到匹配 skill。")


def fetch_from_source(
    source: str,
    pool_dir: Path,
    selector: str | None = None,
    fetch_all: bool = False,
    proxy: str | None = None,
    no_proxy: str | None = None,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    promoted: list[str] = []

    def _update_origin(dst_path: str, rel_dir: str) -> None:
        try:
            dst = Path(dst_path)
            origin_file = dst / ".skillctl-origin.json"
            origin: dict[str, object] = {}
            if origin_file.exists():
                try:
                    existing = json.loads(origin_file.read_text(encoding="utf-8"))
                    if isinstance(existing, dict):
                        origin = dict(existing)
                except (OSError, json.JSONDecodeError):
                    origin = {}
            origin["external_source"] = source
            origin["external_rel_dir"] = rel_dir
            origin["imported_via"] = "fetch_from_source"
            origin["imported_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            origin_file.write_text(json.dumps(origin, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            return

    with materialize_source(source, proxy=proxy, no_proxy=no_proxy, log=log) as root:
        items = index_repo(root)
        selected = _select_items(items, selector=selector, fetch_all=fetch_all)
        if log:
            log(f"准备导入 {len(selected)} 个 skill。")
        for item in selected:
            if log:
                log(f"导入: {item.rel_dir}")
            result = promote_skill(item.skill_dir, pool_dir, root)
            if result.success:
                promoted.append(result.destination)
                if result.destination:
                    _update_origin(result.destination, item.rel_dir)
                if log:
                    log(f"完成: {result.destination}")
            else:
                raise RuntimeError(result.message)
    return promoted
