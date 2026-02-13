from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .models import PromoteResult
from .scanner import parse_frontmatter, sha256_file


PROJECT_SKILL_PATTERNS = (
    ".gemini/skills/*/SKILL.md",
    ".agent/skills/*/SKILL.md",
    ".claude/skills/*/SKILL.md",
    ".codex/skills/*/SKILL.md",
)


def discover_project_skills(workspace: Path) -> list[Path]:
    ws = workspace.resolve()
    hits: list[Path] = []
    for pattern in PROJECT_SKILL_PATTERNS:
        hits.extend(sorted(ws.glob(pattern)))
    # 去重
    seen: set[str] = set()
    unique: list[Path] = []
    for item in hits:
        key = str(item.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique.append(item.resolve())
    return unique


def _slugify(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9_-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "unnamed-skill"


def _resolve_skill_dir(input_path: Path, workspace: Path) -> Path | None:
    p = input_path
    if not p.is_absolute():
        p = (workspace / p).resolve()
    if p.is_file() and p.name == "SKILL.md":
        return p.parent
    if p.is_dir() and (p / "SKILL.md").exists():
        return p
    return None


def _append_promotion_log(pool_dir: Path, row: dict) -> None:
    state_dir = pool_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / "promotions.jsonl"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def promote_skill(input_path: str, pool_dir: Path, workspace: Path) -> PromoteResult:
    skill_dir = _resolve_skill_dir(Path(input_path), workspace)
    if skill_dir is None:
        return PromoteResult(
            success=False,
            action="invalid_input",
            message="未找到有效 skill（需要目录下含 SKILL.md）。",
        )

    skill_md = skill_dir / "SKILL.md"
    name, _desc = parse_frontmatter(skill_md)
    skill_id = _slugify(name or skill_dir.name)

    target_root = pool_dir / "skills"
    target_root.mkdir(parents=True, exist_ok=True)
    dst = target_root / skill_id

    src_hash = sha256_file(skill_md)
    if dst.exists():
        existing_md = dst / "SKILL.md"
        if existing_md.exists():
            try:
                old_hash = sha256_file(existing_md)
            except OSError:
                old_hash = ""
            if old_hash == src_hash:
                return PromoteResult(
                    success=True,
                    action="skip_same_hash",
                    message="目标已存在相同版本，跳过。",
                    source=str(skill_dir),
                    destination=str(dst),
                )
        suffix = datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = target_root / f"{skill_id}-{suffix}"

    shutil.copytree(skill_dir, dst, symlinks=True)

    origin = {
        "source_path": str(skill_dir),
        "workspace": str(workspace.resolve()),
        "promoted_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "sha256": src_hash,
    }
    (dst / ".skillctl-origin.json").write_text(
        json.dumps(origin, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _append_promotion_log(
        pool_dir,
        {
            "action": "promote",
            "source": str(skill_dir),
            "destination": str(dst),
            "skill_id": dst.name,
            "sha256": src_hash,
            "time": origin["promoted_at"],
        },
    )

    return PromoteResult(
        success=True,
        action="copied",
        message="升格成功。",
        source=str(skill_dir),
        destination=str(dst),
    )
