from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class EcosystemEntry:
    id: str
    name: str
    kind: str  # "agent" or "app"
    can_generate: bool
    can_consume: bool
    sync_platform: str | None
    inventory_agents: tuple[str, ...] = ()
    note: str = ""


ENTRIES: tuple[EcosystemEntry, ...] = (
    EcosystemEntry(
        id="codex",
        name="Codex",
        kind="agent",
        can_generate=True,
        can_consume=True,
        sync_platform="codex",
        inventory_agents=("codex",),
    ),
    EcosystemEntry(
        id="gemini",
        name="Gemini CLI",
        kind="agent",
        can_generate=True,
        can_consume=True,
        sync_platform="gemini",
        inventory_agents=("gemini",),
    ),
    EcosystemEntry(
        id="claude",
        name="Claude",
        kind="agent",
        can_generate=True,
        can_consume=True,
        sync_platform="claude",
        inventory_agents=("claude",),
    ),
    EcosystemEntry(
        id="antigravity",
        name="Antigravity",
        kind="agent",
        can_generate=True,
        can_consume=True,
        sync_platform="antigravity",
        inventory_agents=("antigravity",),
    ),
    EcosystemEntry(
        id="obsidian",
        name="Obsidian",
        kind="app",
        can_generate=False,
        can_consume=True,
        sync_platform=None,
        note="当前无统一自动安装路径，仅记录授权状态。",
    ),
)


ENTRY_BY_ID = {item.id: item for item in ENTRIES}
DEFAULT_FOLLOW_SOURCE_IDS = tuple(item.id for item in ENTRIES if item.can_generate)
DEFAULT_GRANT_TARGET_IDS = tuple(item.id for item in ENTRIES if item.can_consume and item.sync_platform)


def all_entries() -> tuple[EcosystemEntry, ...]:
    return ENTRIES


def known_sync_platforms() -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for entry in ENTRIES:
        if not entry.sync_platform:
            continue
        if entry.sync_platform in seen:
            continue
        seen.add(entry.sync_platform)
        values.append(entry.sync_platform)
    return values


def _normalize_common(values: Iterable[str] | None) -> list[str]:
    if not values:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        key = str(raw).strip().lower()
        if not key or key in {"none", "null"}:
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def normalize_follow_sources(values: Iterable[str] | None) -> list[str]:
    items = _normalize_common(values)
    if not items:
        return list(DEFAULT_FOLLOW_SOURCE_IDS)
    result: list[str] = []
    for item in items:
        entry = ENTRY_BY_ID.get(item)
        if entry and not entry.can_generate:
            continue
        result.append(item)
    return result


def normalize_grant_targets(values: Iterable[str] | None) -> list[str]:
    items = _normalize_common(values)
    if not items:
        return list(DEFAULT_GRANT_TARGET_IDS)
    result: list[str] = []
    for item in items:
        entry = ENTRY_BY_ID.get(item)
        if entry and not entry.can_consume:
            continue
        result.append(item)
    return result


def split_csv(values: str | None) -> list[str]:
    if values is None:
        return []
    return _normalize_common(values.split(","))


def granted_platforms(grant_targets: Iterable[str] | None) -> list[str]:
    targets = normalize_grant_targets(grant_targets)
    result: list[str] = []
    seen: set[str] = set()
    for target in targets:
        entry = ENTRY_BY_ID.get(target)
        if entry:
            platform = entry.sync_platform
        else:
            # 自定义目标默认按同名平台处理，若 targets.conf 存在同名 platform 即可自动同步。
            platform = target
        if not platform:
            continue
        if platform in seen:
            continue
        seen.add(platform)
        result.append(platform)
    return result


def non_auto_targets(grant_targets: Iterable[str] | None) -> list[EcosystemEntry]:
    rows: list[EcosystemEntry] = []
    for target in normalize_grant_targets(grant_targets):
        entry = ENTRY_BY_ID.get(target)
        if entry and not entry.sync_platform:
            rows.append(entry)
    return rows


def _inventory_counts(payload: dict | None) -> dict[str, int]:
    if not payload:
        return {}
    counts = payload.get("counts_by_agent")
    if isinstance(counts, dict):
        result: dict[str, int] = {}
        for key, value in counts.items():
            try:
                result[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        return result

    result: dict[str, int] = {}
    for row in payload.get("records", []) if isinstance(payload, dict) else []:
        if not isinstance(row, dict):
            continue
        agent = str(row.get("agent", "")).strip()
        if not agent:
            continue
        result[agent] = result.get(agent, 0) + 1
    return result


def ecosystem_status_rows(
    follow_sources: Iterable[str] | None,
    grant_targets: Iterable[str] | None,
    inventory_payload: dict | None,
    available_platforms: Iterable[str] | None = None,
) -> list[dict[str, object]]:
    follow_set = set(normalize_follow_sources(follow_sources))
    grant_set = set(normalize_grant_targets(grant_targets))
    available_set = {str(x).strip().lower() for x in (available_platforms or []) if str(x).strip()}
    counts = _inventory_counts(inventory_payload)

    rows: list[dict[str, object]] = []
    for entry in ENTRIES:
        discovered = 0
        for agent in entry.inventory_agents:
            discovered += counts.get(agent, 0)

        rows.append(
            {
                "id": entry.id,
                "name": entry.name,
                "kind": entry.kind,
                "can_generate": entry.can_generate,
                "can_consume": entry.can_consume,
                "followed": entry.id in follow_set,
                "granted": entry.id in grant_set,
                "sync_platform": entry.sync_platform or "-",
                "auto_sync_ready": bool(entry.sync_platform and entry.sync_platform in available_set),
                "discovered_count": discovered,
                "note": entry.note,
            }
        )

    known_ids = {entry.id for entry in ENTRIES}
    custom_ids = sorted((follow_set | grant_set) - known_ids)
    for item_id in custom_ids:
        rows.append(
            {
                "id": item_id,
                "name": item_id,
                "kind": "agent",
                "can_generate": True,
                "can_consume": True,
                "followed": item_id in follow_set,
                "granted": item_id in grant_set,
                "sync_platform": item_id,
                "auto_sync_ready": item_id in available_set,
                "discovered_count": counts.get(item_id, 0),
                "note": "自定义对象：可记录关注/赋予；若 targets.conf 存在同名 platform，可自动同步。",
            }
        )
    return rows
