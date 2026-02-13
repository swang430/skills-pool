from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .models import SkillRecord


def iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_frontmatter(skill_md: Path) -> tuple[str | None, str | None]:
    try:
        text = skill_md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None, None

    if not text.startswith("---"):
        return None, None

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, None

    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return None, None

    name = None
    description = None
    for line in lines[1:end_idx]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip().strip("\"'")
        if key == "name":
            name = value
        elif key == "description":
            description = value
    return name, description


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _claude_enabled_plugins() -> set[str]:
    settings = _load_json(Path.home() / ".claude/settings.json")
    enabled_plugins = settings.get("enabledPlugins", {})
    if not isinstance(enabled_plugins, dict):
        return set()
    return {k for k, v in enabled_plugins.items() if bool(v)}


def _scan_skill_md(
    skill_md: Path,
    agent: str,
    scope: str,
    source_type: str,
    enabled: bool | None = True,
    extra: dict | None = None,
) -> SkillRecord | None:
    if not skill_md.exists():
        return None

    skill_dir = skill_md.parent
    front_name, front_desc = parse_frontmatter(skill_md)
    name = front_name or skill_dir.name
    description = front_desc or ""

    try:
        digest = sha256_file(skill_md)
    except OSError:
        return None

    return SkillRecord(
        agent=agent,
        scope=scope,
        name=name,
        description=description,
        source_type=source_type,
        enabled=enabled,
        skill_dir=str(skill_dir),
        skill_md=str(skill_md),
        sha256=digest,
        discovered_at=iso_now(),
        extra=extra or {},
    )


def _scan_one_level(
    base_dir: Path,
    agent: str,
    scope: str,
    source_type: str,
    enabled: bool | None = True,
) -> list[SkillRecord]:
    if not base_dir.exists():
        return []

    records: list[SkillRecord] = []
    for skill_md in sorted(base_dir.glob("*/SKILL.md")):
        rec = _scan_skill_md(
            skill_md=skill_md,
            agent=agent,
            scope=scope,
            source_type=source_type,
            enabled=enabled,
        )
        if rec:
            records.append(rec)
    return records


def _parse_claude_market_skill(skill_md: Path, enabled_plugins: set[str]) -> tuple[bool | None, dict]:
    parts = skill_md.parts
    extra: dict[str, str] = {}
    enabled: bool | None = None

    if "marketplaces" in parts:
        idx = parts.index("marketplaces")
        if idx + 1 < len(parts):
            extra["marketplace"] = parts[idx + 1]

    plugin_name = None
    if "plugins" in parts:
        idx = parts.index("plugins")
        if idx + 1 < len(parts):
            plugin_name = parts[idx + 1]
    elif "external_plugins" in parts:
        idx = parts.index("external_plugins")
        if idx + 1 < len(parts):
            plugin_name = parts[idx + 1]

    if plugin_name:
        extra["plugin"] = plugin_name
        enabled = any(x.startswith(f"{plugin_name}@") for x in enabled_plugins)

    return enabled, extra


def _scan_claude_marketplaces(base_dir: Path) -> list[SkillRecord]:
    if not base_dir.exists():
        return []

    enabled_plugins = _claude_enabled_plugins()
    records: list[SkillRecord] = []
    for skill_md in sorted(base_dir.rglob("SKILL.md")):
        parts = skill_md.parts
        if "skills" not in parts:
            continue

        enabled, extra = _parse_claude_market_skill(skill_md, enabled_plugins)
        rec = _scan_skill_md(
            skill_md=skill_md,
            agent="claude",
            scope="plugin_marketplace",
            source_type="marketplace",
            enabled=enabled,
            extra=extra,
        )
        if rec:
            records.append(rec)
    return records


def scan_environment(workspace: Path | None = None) -> list[SkillRecord]:
    ws = (workspace or Path.cwd()).resolve()
    home = Path.home()

    records: list[SkillRecord] = []
    records.extend(_scan_one_level(home / ".codex/skills", "codex", "user", "native"))
    records.extend(_scan_one_level(home / ".codex/skills/.system", "codex", "user", "system"))
    records.extend(_scan_one_level(ws / ".codex/skills", "codex", "workspace", "native"))
    records.extend(_scan_one_level(ws / ".codex/skills/.system", "codex", "workspace", "system"))

    records.extend(_scan_one_level(home / ".gemini/skills", "gemini", "user", "native"))
    records.extend(_scan_one_level(ws / ".gemini/skills", "gemini", "workspace", "native"))

    records.extend(_scan_one_level(home / ".gemini/antigravity/skills", "antigravity", "user", "native"))
    records.extend(_scan_one_level(ws / ".agent/skills", "antigravity", "workspace", "native"))

    records.extend(_scan_one_level(home / ".claude/skills", "claude", "user", "native"))
    records.extend(_scan_one_level(ws / ".claude/skills", "claude", "workspace", "native"))
    records.extend(_scan_claude_marketplaces(home / ".claude/plugins/marketplaces"))

    # 去重：同一 SKILL.md 只保留一次
    unique: dict[str, SkillRecord] = {}
    for rec in records:
        unique[rec.skill_md] = rec
    return sorted(unique.values(), key=lambda x: (x.agent, x.scope, x.name, x.skill_md))
