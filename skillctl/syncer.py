from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class SyncResult:
    returncode: int
    stdout: str
    stderr: str
    cmd: list[str]

    @property
    def success(self) -> bool:
        return self.returncode == 0


def parse_target_platforms(pool_dir: Path) -> list[str]:
    config_path = pool_dir / "config" / "targets.conf"
    if not config_path.exists():
        return []

    platforms: list[str] = []
    seen: set[str] = set()
    for raw in config_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [chunk.strip() for chunk in line.split("|")]
        if len(parts) < 3:
            continue
        platform = parts[0].lower()
        if not platform or platform in seen:
            continue
        seen.add(platform)
        platforms.append(platform)
    return platforms


def _normalize_platform_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [x.strip().lower() for x in value.split(",") if x.strip()]


def _normalize_skill_ids(values: list[str] | None) -> set[str]:
    if not values:
        return set()
    rows: set[str] = set()
    for raw in values:
        key = str(raw).strip()
        if not key:
            continue
        rows.add(key)
    return rows


def prepare_distribution(
    pool_dir: Path,
    platforms: list[str] | None = None,
    selected_skills: list[str] | None = None,
) -> None:
    source_root = pool_dir / "skills"
    if not source_root.exists():
        return

    target_platforms = platforms or parse_target_platforms(pool_dir)
    source_skills = [p for p in sorted(source_root.iterdir()) if (p / "SKILL.md").exists()]
    selected = _normalize_skill_ids(selected_skills)
    if selected:
        source_skills = [p for p in source_skills if p.name in selected]

    for platform in target_platforms:
        dist_dir = pool_dir / "dist" / platform
        dist_dir.mkdir(parents=True, exist_ok=True)

        # 先记录当前由本步骤创建的链接，后续可以清理
        expected = {skill.name for skill in source_skills}
        for item in dist_dir.iterdir():
            if item.name not in expected and item.is_symlink():
                item.unlink(missing_ok=True)

        for skill in source_skills:
            link_path = dist_dir / skill.name
            if link_path.is_symlink():
                target = link_path.resolve()
                if target == skill.resolve():
                    continue
                link_path.unlink(missing_ok=True)
            elif link_path.exists():
                # 非软链接冲突交给后续 link-skills.sh 的冲突策略处理，这里不覆盖
                continue
            link_path.symlink_to(skill.resolve())


def run_sync(
    pool_dir: Path,
    dry_run: bool = False,
    prune: bool = False,
    only: str | None = None,
    backup_conflicts: bool = False,
    allowed_platforms: list[str] | None = None,
    selected_skills: list[str] | None = None,
    log: Callable[[str], None] | None = None,
) -> SyncResult:
    from_config = parse_target_platforms(pool_dir)
    active = list(from_config)
    if allowed_platforms is not None:
        allowed_set = {x.strip().lower() for x in allowed_platforms if x.strip()}
        active = [x for x in active if x in allowed_set]

    req_only = _normalize_platform_csv(only)
    if req_only:
        req_set = set(req_only)
        active = [x for x in active if x in req_set]

    if not active:
        if log:
            log("无可同步平台（请检查授权或 targets.conf）。")
        return SyncResult(
            returncode=0,
            stdout="无可同步平台（请检查授权或 targets.conf）。",
            stderr="",
            cmd=[],
        )

    prepare_distribution(pool_dir, platforms=active, selected_skills=selected_skills)

    script = pool_dir / "tools/link-skills.sh"
    if not script.exists():
        raise FileNotFoundError(f"未找到同步脚本: {script}")

    cmd = [str(script)]
    if dry_run:
        cmd.append("--dry-run")
    if prune:
        cmd.append("--prune")
    cmd.extend(["--only", ",".join(active)])
    if backup_conflicts:
        cmd.append("--backup-conflicts")

    if not log:
        proc = subprocess.run(
            cmd,
            cwd=str(pool_dir),
            text=True,
            capture_output=True,
            check=False,
        )
        return SyncResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            cmd=cmd,
        )

    proc = subprocess.Popen(
        cmd,
        cwd=str(pool_dir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    lines: list[str] = []
    assert proc.stdout is not None
    for raw in proc.stdout:
        lines.append(raw)
        line = raw.rstrip()
        if line:
            log(line)
    proc.wait()
    stdout = "".join(lines)
    return SyncResult(
        returncode=proc.returncode,
        stdout=stdout,
        stderr="",
        cmd=cmd,
    )
