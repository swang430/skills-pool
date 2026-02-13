from __future__ import annotations

import json
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .scanner import parse_frontmatter


@dataclass
class AuditResult:
    scanned_at: str
    issues: list[dict[str, Any]]
    summary: dict[str, int]


def _pool_skill_dirs(pool_dir: Path) -> list[Path]:
    root = pool_dir / "skills"
    if not root.exists():
        return []
    return [p for p in sorted(root.iterdir()) if p.is_dir()]


def audit_pool(pool_dir: Path) -> AuditResult:
    issues: list[dict[str, Any]] = []
    by_name: dict[str, list[str]] = defaultdict(list)

    for skill_dir in _pool_skill_dirs(pool_dir):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            issues.append(
                {
                    "type": "missing_skill_md",
                    "path": str(skill_dir),
                    "message": "缺少 SKILL.md",
                }
            )
            continue
        name, _desc = parse_frontmatter(skill_md)
        by_name[(name or skill_dir.name).strip().lower()].append(str(skill_dir))

    for name, paths in by_name.items():
        if len(paths) > 1:
            issues.append(
                {
                    "type": "duplicate_name",
                    "name": name,
                    "paths": paths,
                    "message": f"检测到重名 skill: {name}",
                }
            )

    # dist 目录失效链接检查
    dist_root = pool_dir / "dist"
    for platform in ("codex", "gemini", "claude"):
        p_dir = dist_root / platform
        if not p_dir.exists():
            continue
        for item in p_dir.iterdir():
            if item.is_symlink() and not item.exists():
                issues.append(
                    {
                        "type": "broken_symlink",
                        "platform": platform,
                        "path": str(item),
                        "message": "dist 中存在失效软链接",
                    }
                )

    summary: dict[str, int] = defaultdict(int)
    for issue in issues:
        summary[str(issue["type"])] += 1

    return AuditResult(
        scanned_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        issues=issues,
        summary=dict(sorted(summary.items())),
    )


def write_audit_report(pool_dir: Path, result: AuditResult) -> dict[str, Path]:
    state_dir = pool_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "scanned_at": result.scanned_at,
        "summary": result.summary,
        "total_issues": len(result.issues),
        "issues": result.issues,
    }

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    snapshot_json = state_dir / f"maintenance-{ts}.json"
    latest_json = state_dir / "maintenance-latest.json"
    latest_md = state_dir / "maintenance-latest.md"

    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    snapshot_json.write_text(raw, encoding="utf-8")
    latest_json.write_text(raw, encoding="utf-8")

    lines = [
        "# Maintenance Report",
        "",
        f"- Scanned at: `{payload['scanned_at']}`",
        f"- Total issues: `{payload['total_issues']}`",
        "",
        "## Summary",
        "",
    ]
    if payload["summary"]:
        for k, v in payload["summary"].items():
            lines.append(f"- `{k}`: {v}")
    else:
        lines.append("- 无")
    lines.extend(["", "## Issues", ""])
    if payload["issues"]:
        for idx, issue in enumerate(payload["issues"], start=1):
            lines.append(f"{idx}. `{issue.get('type')}` - {issue.get('message', '')}")
            lines.append(f"   path: `{issue.get('path', '-')}`")
            if issue.get("paths"):
                lines.append(f"   paths: `{', '.join(issue['paths'])}`")
    else:
        lines.append("无问题。")
    lines.append("")
    latest_md.write_text("\n".join(lines), encoding="utf-8")

    return {
        "snapshot_json": snapshot_json,
        "latest_json": latest_json,
        "latest_md": latest_md,
    }


def prune_broken_dist_symlinks(pool_dir: Path) -> int:
    removed = 0
    for platform in ("codex", "gemini", "claude"):
        p_dir = pool_dir / "dist" / platform
        if not p_dir.exists():
            continue
        for item in p_dir.iterdir():
            if item.is_symlink() and not item.exists():
                item.unlink(missing_ok=True)
                removed += 1
    return removed


def delete_pool_skill(pool_dir: Path, skill_id: str) -> Path:
    src = pool_dir / "skills" / skill_id
    if not src.exists():
        raise FileNotFoundError(f"未找到 skill: {src}")

    trash_dir = pool_dir / "trash"
    trash_dir.mkdir(parents=True, exist_ok=True)
    suffix = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = trash_dir / f"{skill_id}-{suffix}"
    shutil.move(str(src), str(dst))
    return dst
