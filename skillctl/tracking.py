from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import AppConfig, TrackedSource
from .indexing import IndexedSkill, fetch_from_source, index_source


@dataclass
class TrackCheckResult:
    source: TrackedSource
    checked_at: str
    total: int
    added: list[str]
    removed: list[str]
    all_items: list[IndexedSkill]


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _slugify(raw: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw.strip().lower())
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "tracked-source"


def _state_dir(pool_dir: Path) -> Path:
    d = pool_dir / "state" / "tracking"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _snapshot_path(pool_dir: Path, source_id: str) -> Path:
    return _state_dir(pool_dir) / f"{_slugify(source_id)}.json"


def _load_prev_items(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    items = data.get("items", [])
    if not isinstance(items, list):
        return []
    return [str(x) for x in items if str(x).strip()]


def _save_snapshot(pool_dir: Path, src: TrackedSource, items: list[str]) -> None:
    snap = {
        "checked_at": _now_iso(),
        "id": src.id,
        "agent_id": src.agent_id,
        "name": src.name,
        "source": src.source,
        "total": len(items),
        "items": items,
    }
    _snapshot_path(pool_dir, src.id).write_text(
        json.dumps(snap, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def list_tracked_sources(cfg: AppConfig, only_enabled: bool = False) -> list[TrackedSource]:
    rows = sorted(cfg.tracked_sources, key=lambda x: (x.agent_id, x.name.lower(), x.id))
    if only_enabled:
        rows = [x for x in rows if x.enabled]
    return rows


def get_tracked_source(cfg: AppConfig, source_id: str) -> TrackedSource | None:
    key = source_id.strip().lower()
    for row in cfg.tracked_sources:
        if row.id == key:
            return row
    return None


def add_tracked_source(
    cfg: AppConfig,
    agent_id: str,
    name: str,
    source: str,
    source_id: str | None = None,
    enabled: bool = True,
    note: str = "",
) -> TrackedSource:
    aid = _slugify(agent_id)
    if not aid:
        raise ValueError("agent_id 不能为空。")

    base_id = _slugify(source_id or f"{aid}-{name}")
    sid = base_id
    existing = {x.id for x in cfg.tracked_sources}
    idx = 2
    while sid in existing:
        sid = f"{base_id}-{idx}"
        idx += 1

    row = TrackedSource(
        id=sid,
        agent_id=aid,
        name=name.strip(),
        source=source.strip(),
        enabled=bool(enabled),
        note=note.strip(),
    )
    cfg.tracked_sources.append(row)
    return row


def remove_tracked_source(cfg: AppConfig, source_id: str) -> bool:
    key = source_id.strip().lower()
    for idx, row in enumerate(cfg.tracked_sources):
        if row.id == key:
            del cfg.tracked_sources[idx]
            return True
    return False


def check_tracked_source(
    pool_dir: Path,
    source: TrackedSource,
    update_snapshot: bool = True,
    proxy: str | None = None,
    no_proxy: str | None = None,
    log: Callable[[str], None] | None = None,
) -> TrackCheckResult:
    _root, items = index_source(source.source, proxy=proxy, no_proxy=no_proxy, log=log)
    current = sorted({x.rel_dir for x in items})
    snap_path = _snapshot_path(pool_dir, source.id)
    prev = _load_prev_items(snap_path)
    prev_set = set(prev)
    curr_set = set(current)

    added = sorted(curr_set - prev_set)
    removed = sorted(prev_set - curr_set)

    if update_snapshot:
        _save_snapshot(pool_dir, source, current)

    return TrackCheckResult(
        source=source,
        checked_at=_now_iso(),
        total=len(current),
        added=added,
        removed=removed,
        all_items=items,
    )


def check_tracked_sources(
    cfg: AppConfig,
    pool_dir: Path,
    source_id: str | None = None,
    only_enabled: bool = True,
    update_snapshot: bool = True,
    proxy: str | None = None,
    no_proxy: str | None = None,
    log: Callable[[str], None] | None = None,
) -> list[TrackCheckResult]:
    if source_id:
        row = get_tracked_source(cfg, source_id)
        if not row:
            raise FileNotFoundError(f"未找到 tracked source: {source_id}")
        if only_enabled and not row.enabled:
            return []
        return [
            check_tracked_source(
                pool_dir,
                row,
                update_snapshot=update_snapshot,
                proxy=proxy,
                no_proxy=no_proxy,
                log=log,
            )
        ]

    rows = list_tracked_sources(cfg, only_enabled=only_enabled)
    return [
        check_tracked_source(pool_dir, row, update_snapshot=update_snapshot, proxy=proxy, no_proxy=no_proxy, log=log)
        for row in rows
    ]


def import_from_tracked_source(
    cfg: AppConfig,
    pool_dir: Path,
    source_id: str,
    selectors: list[str] | None = None,
    fetch_all: bool = False,
    proxy: str | None = None,
    no_proxy: str | None = None,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    row = get_tracked_source(cfg, source_id)
    if not row:
        raise FileNotFoundError(f"未找到 tracked source: {source_id}")

    if fetch_all:
        return fetch_from_source(
            source=row.source,
            pool_dir=pool_dir,
            fetch_all=True,
            proxy=proxy,
            no_proxy=no_proxy,
            log=log,
        )

    if not selectors:
        raise ValueError("未提供要导入的 skill（请使用 --skill 或 --all）。")

    imported: list[str] = []
    for selector in selectors:
        paths = fetch_from_source(
            source=row.source,
            pool_dir=pool_dir,
            selector=selector,
            fetch_all=False,
            proxy=proxy,
            no_proxy=no_proxy,
            log=log,
        )
        imported.extend(paths)
    return imported
