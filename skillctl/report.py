from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import SkillRecord


def _counts_by_agent(records: list[SkillRecord]) -> dict[str, int]:
    counter = Counter(rec.agent for rec in records)
    return dict(sorted(counter.items(), key=lambda x: x[0]))


def _counts_by_scope(records: list[SkillRecord]) -> dict[str, int]:
    counter = Counter(f"{rec.agent}:{rec.scope}" for rec in records)
    return dict(sorted(counter.items(), key=lambda x: x[0]))


def build_inventory_payload(records: list[SkillRecord], workspace: Path) -> dict[str, Any]:
    return {
        "scanned_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "workspace": str(workspace),
        "total": len(records),
        "counts_by_agent": _counts_by_agent(records),
        "counts_by_scope": _counts_by_scope(records),
        "records": [rec.to_dict() for rec in records],
    }


def _render_inventory_md(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Skills Inventory")
    lines.append("")
    lines.append(f"- Scanned at: `{payload['scanned_at']}`")
    lines.append(f"- Workspace: `{payload['workspace']}`")
    lines.append(f"- Total: `{payload['total']}`")
    lines.append("")
    lines.append("## Counts By Agent")
    lines.append("")
    for key, value in payload["counts_by_agent"].items():
        lines.append(f"- `{key}`: {value}")
    lines.append("")
    lines.append("## Records")
    lines.append("")
    lines.append("| Agent | Scope | Name | Enabled | Path |")
    lines.append("|---|---|---|---:|---|")
    for rec in payload["records"]:
        enabled = rec["enabled"]
        enabled_str = "unknown" if enabled is None else ("yes" if enabled else "no")
        lines.append(
            f"| {rec['agent']} | {rec['scope']} | {rec['name']} | {enabled_str} | `{rec['skill_md']}` |"
        )
    lines.append("")
    return "\n".join(lines)


def write_inventory_reports(pool_dir: Path, payload: dict[str, Any]) -> dict[str, Path]:
    state_dir = pool_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    snapshot_json = state_dir / f"inventory-{ts}.json"
    latest_json = state_dir / "inventory-latest.json"
    latest_md = state_dir / "inventory-latest.md"

    content = json.dumps(payload, ensure_ascii=False, indent=2)
    snapshot_json.write_text(content, encoding="utf-8")
    latest_json.write_text(content, encoding="utf-8")
    latest_md.write_text(_render_inventory_md(payload), encoding="utf-8")

    return {
        "snapshot_json": snapshot_json,
        "latest_json": latest_json,
        "latest_md": latest_md,
    }


def load_latest_inventory(pool_dir: Path) -> dict[str, Any] | None:
    path = pool_dir / "state/inventory-latest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
