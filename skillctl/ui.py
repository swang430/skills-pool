from __future__ import annotations

import curses
import locale
import re
from pathlib import Path

from .config import AppConfig, ensure_pool_layout, save_config
from .ecosystem import (
    ecosystem_status_rows,
    granted_platforms,
    normalize_follow_sources,
    normalize_grant_targets,
    non_auto_targets,
)
from .indexing import fetch_from_source, index_source
from .markets import MarketView, list_market_views
from .maintenance import audit_pool, prune_broken_dist_symlinks, write_audit_report
from .promote import discover_project_skills, promote_skill
from .report import build_inventory_payload, load_latest_inventory, write_inventory_reports
from .scanner import scan_environment
from .syncer import parse_target_platforms, run_sync
from .tracking import (
    add_tracked_source,
    check_tracked_sources,
    import_from_tracked_source,
    list_tracked_sources,
    remove_tracked_source,
)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _safe_addnstr(
    stdscr: curses.window,
    y: int,
    x: int,
    text: str,
    max_chars: int,
    attr: int = curses.A_NORMAL,
) -> None:
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    limit = min(max_chars, w - x)
    if limit <= 0:
        return
    try:
        stdscr.addnstr(y, x, text, limit, attr)
    except curses.error:
        # 终端无法渲染某些 Unicode 时，回退为 ASCII，避免直接崩溃。
        fallback = text.encode("ascii", errors="replace").decode("ascii")
        try:
            stdscr.addnstr(y, x, fallback, limit, attr)
        except curses.error:
            return


def _safe_hline(stdscr: curses.window, y: int, x: int, ch: str, n: int) -> None:
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    width = min(n, w - x)
    if width <= 0:
        return
    try:
        stdscr.hline(y, x, ch, width)
    except curses.error:
        return


MENU_ITEMS = [
    "扫描并记录当前环境 skills",
    "查看库存总览",
    "升格项目 skill 到全局池",
    "索引/下载外部 skill（git 或本地路径）",
    "同步池到各 Agent（可选 dry-run）",
    "生态授权管理（关注来源 / 赋予目标）",
    "外部追踪管理（含常用 market 选择）",
    "维护检查（体检 + 可选清理失效链接）",
    "退出",
]


def _draw(stdscr: curses.window, selected: int, status: str, cfg: AppConfig) -> None:
    stdscr.clear()
    height, width = stdscr.getmaxyx()
    title = "skillctl - 本地轻量 Skill 管理器 (↑/↓ 选择, Enter 执行)"
    if height < 8 or width < 24:
        _safe_addnstr(stdscr, 1, 1, "终端窗口太小，请放大后再使用 UI。", width - 2, curses.A_BOLD)
        _safe_addnstr(stdscr, 3, 1, f"当前尺寸: {width}x{height}", width - 2)
        stdscr.refresh()
        return
    _safe_addnstr(stdscr, 1, 2, title, width - 4, curses.A_BOLD)
    _safe_addnstr(stdscr, 2, 2, f"Pool: {cfg.pool_path}", width - 4)

    top = 4
    for idx, text in enumerate(MENU_ITEMS):
        y = top + idx
        if y >= height - 4:
            break
        prefix = "> " if idx == selected else "  "
        attr = curses.A_REVERSE if idx == selected else curses.A_NORMAL
        _safe_addnstr(stdscr, y, 2, f"{prefix}{idx + 1}. {text}", width - 4, attr)

    _safe_hline(stdscr, height - 4, 1, "-", max(1, width - 2))
    _safe_addnstr(stdscr, height - 3, 2, "状态:", width - 4, curses.A_BOLD)
    _safe_addnstr(stdscr, height - 2, 2, status, width - 4)
    stdscr.refresh()


def _prompt(stdscr: curses.window, text: str) -> str:
    height, width = stdscr.getmaxyx()
    if height < 4 or width < 12:
        return ""
    # 双行输入：上行显示提示，下行固定输入起点，避免中文宽度导致错位。
    stdscr.move(height - 2, 0)
    stdscr.clrtoeol()
    stdscr.move(height - 1, 0)
    stdscr.clrtoeol()
    _safe_addnstr(stdscr, height - 2, 2, text, width - 4)
    _safe_addnstr(stdscr, height - 1, 2, "> ", width - 4)
    stdscr.refresh()
    curses.echo()
    try:
        raw = stdscr.getstr(height - 1, 4, max(1, width - 6))
    except curses.error:
        return ""
    finally:
        curses.noecho()
    try:
        return raw.decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""


def _show_text(stdscr: curses.window, title: str, content: str) -> None:
    lines = content.splitlines() or [""]
    offset = 0
    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        _safe_addnstr(stdscr, 0, 2, title, w - 4, curses.A_BOLD)
        _safe_addnstr(stdscr, 1, 2, "空格/b 翻页, j/k 或 ↑/↓ 行滚动, g/G 到首尾, q 返回", w - 4)
        body_h = h - 3
        for i in range(body_h):
            idx = offset + i
            if idx >= len(lines):
                break
            _safe_addnstr(stdscr, 2 + i, 2, lines[idx], w - 4)
        stdscr.refresh()

        key = stdscr.getch()
        if key in (ord("q"), 27):
            return
        if key in (curses.KEY_NPAGE, ord(" "), ord("f")):
            offset = min(max(0, len(lines) - body_h), offset + body_h)
        elif key in (curses.KEY_PPAGE, ord("b")):
            offset = max(0, offset - body_h)
        elif key in (curses.KEY_DOWN, ord("j")):
            offset = min(max(0, len(lines) - body_h), offset + 1)
        elif key in (curses.KEY_UP, ord("k")):
            offset = max(0, offset - 1)
        elif key in (ord("g"),):
            offset = 0
        elif key in (ord("G"),):
            offset = max(0, len(lines) - body_h)


def _resolve_market_selector(selector: str, rows: list[MarketView]) -> MarketView | None:
    raw = selector.strip().lower()
    if not raw:
        return None
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(rows):
            return rows[idx - 1]
    for item in rows:
        if item.id == raw:
            return item
    return None


def _choose_source(stdscr: curses.window, title: str) -> tuple[str | None, str, MarketView | None]:
    mode = _prompt(stdscr, "来源选择: [m]arket(含 claude-marketplace) / [i]输入地址 / [q]取消: ").strip().lower()
    if mode in {"q", "quit", "cancel"}:
        return None, "已取消。", None
    if mode in {"m", "market"}:
        rows = list_market_views(include_missing=True)
        lines = ["常用 Skills Market:", ""]
        for idx, item in enumerate(rows, start=1):
            status = "可用" if item.exists else "不可用"
            lines.append(f"{idx}. {item.id} | {item.name} | {status}")
            lines.append(f"   source: {item.source}")
            lines.append(f"   agent:  {item.agent_hint}")
            lines.append(f"   note:   {item.description}")
        _show_text(stdscr, title, "\n".join(lines))
        selector = _prompt(stdscr, "输入 market id 或序号: ")
        item = _resolve_market_selector(selector, rows)
        if not item:
            return None, "未找到 market，已取消。", None
        if not item.exists:
            return None, f"market 不可用（路径不存在）: {item.id}", item
        return item.source, f"来自 market: {item.name} ({item.id})", item

    source = _prompt(stdscr, "输入 git 地址或本地目录: ")
    if not source:
        return None, "已取消。", None
    return source, "", None


def _prompt_default(stdscr: curses.window, text: str, default: str) -> str:
    raw = _prompt(stdscr, f"{text} [默认: {default}]: ")
    return raw or default


def _action_scan(stdscr: curses.window, cfg: AppConfig) -> str:
    records = scan_environment(Path.cwd())
    payload = build_inventory_payload(records, Path.cwd())
    paths = write_inventory_reports(cfg.pool_path, payload)
    return f"扫描完成: {len(records)} 条。报告: {paths['latest_json']} / {paths['latest_md']}"


def _action_overview(stdscr: curses.window, cfg: AppConfig) -> str:
    payload = load_latest_inventory(cfg.pool_path)
    if not payload:
        return "尚无库存报告，请先执行扫描。"

    lines: list[str] = []
    lines.append(f"Scanned At: {payload.get('scanned_at')}")
    lines.append(f"Workspace:  {payload.get('workspace')}")
    lines.append(f"Total:      {payload.get('total')}")
    lines.append("")
    lines.append("Counts By Agent:")
    for k, v in payload.get("counts_by_agent", {}).items():
        lines.append(f"  - {k}: {v}")
    lines.append("")
    lines.append("Counts By Scope:")
    for k, v in payload.get("counts_by_scope", {}).items():
        lines.append(f"  - {k}: {v}")
    lines.append("")
    lines.append("Top Records (first 30):")
    records = payload.get("records", [])[:30]
    for rec in records:
        lines.append(f"  - {rec.get('agent')} | {rec.get('scope')} | {rec.get('name')} | {rec.get('skill_md')}")

    _show_text(stdscr, "库存总览", "\n".join(lines))
    return "已显示库存总览。"


def _action_promote(stdscr: curses.window, cfg: AppConfig) -> str:
    project_skills = discover_project_skills(Path.cwd())
    hint = ""
    if project_skills:
        hint = "\n".join(f"{idx + 1}. {p}" for idx, p in enumerate(project_skills[:8]))
    else:
        hint = "未发现常见项目 skills，可手工输入路径。"
    _show_text(stdscr, "项目 Skills 发现结果", hint)

    raw = _prompt(stdscr, "输入要升格的 skill 目录或 SKILL.md 路径: ")
    if not raw:
        return "已取消。"
    result = promote_skill(raw, cfg.pool_path, Path.cwd())
    if result.success:
        return f"{result.message} {result.source} -> {result.destination}"
    return f"失败: {result.message}"


def _action_sync(stdscr: curses.window, cfg: AppConfig) -> str:
    mode = _prompt(stdscr, "dry-run? [Y/n]: ").lower()
    dry_run = mode not in {"n", "no"}
    only = _prompt(stdscr, "仅同步平台(可空，例如 codex,gemini): ")
    prune = _prompt(stdscr, "是否 prune 过期链接? [Y/n]: ").lower() not in {"n", "no"}
    result = run_sync(
        cfg.pool_path,
        dry_run=dry_run,
        prune=prune,
        only=only or None,
        allowed_platforms=granted_platforms(cfg.grant_targets),
    )

    output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    manual_targets = non_auto_targets(cfg.grant_targets)
    if manual_targets:
        output += f"\n提示: {', '.join(item.name for item in manual_targets)} 暂无统一自动安装路径。"
    if not output.strip():
        output = "(无输出)"
    _show_text(stdscr, "同步输出", output)

    if result.success:
        return "同步完成。"
    return f"同步失败，退出码: {result.returncode}"


def _action_index_fetch(stdscr: curses.window, cfg: AppConfig) -> str:
    source, source_note, _market = _choose_source(stdscr, "选择外部来源")
    if not source:
        return source_note or "已取消。"
    try:
        _root, items = index_source(source)
    except Exception as exc:  # noqa: BLE001
        return f"索引失败: {exc}"

    preview = [f"共 {len(items)} 个 skill。", ""]
    for idx, item in enumerate(items[:40], start=1):
        preview.append(f"{idx}. {item.name} | {item.rel_dir}")
    if len(items) > 40:
        preview.append(f"... 其余 {len(items) - 40} 条略")
    if source_note:
        preview.append("")
        preview.append(source_note)
    _show_text(stdscr, "索引结果", "\n".join(preview))

    mode = _prompt(stdscr, "下载模式: [o]ne / [a]ll / [n]one: ").lower()
    if mode in {"n", "none", ""}:
        return "仅索引，未下载。"

    try:
        if mode in {"a", "all"}:
            promoted = fetch_from_source(source=source, pool_dir=cfg.pool_path, fetch_all=True)
        else:
            selector = _prompt(stdscr, "输入要下载的 skill 名或 rel_dir: ")
            if not selector:
                return "未输入 skill，已取消。"
            promoted = fetch_from_source(source=source, pool_dir=cfg.pool_path, selector=selector, fetch_all=False)
    except Exception as exc:  # noqa: BLE001
        return f"下载失败: {exc}"

    _show_text(stdscr, "下载结果", "\n".join([f"- {p}" for p in promoted]) or "无")
    return f"下载完成，共 {len(promoted)} 个。"


def _split_csv_with_unknown(raw: str) -> tuple[list[str], list[str]]:
    valid: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        key = part.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        if _ID_RE.match(key):
            valid.append(key)
        else:
            unknown.append(key)
    return valid, unknown


def _action_ecosystem(stdscr: curses.window, cfg: AppConfig) -> str:
    payload = load_latest_inventory(cfg.pool_path)
    rows = ecosystem_status_rows(
        follow_sources=cfg.follow_sources,
        grant_targets=cfg.grant_targets,
        inventory_payload=payload,
        available_platforms=parse_target_platforms(cfg.pool_path),
    )

    lines = [
        "支持对象清单（id 可用于设置关注/赋予）:",
        "",
    ]
    for row in rows:
        lines.append(
            f"- {row['name']} ({row['id']}) | 类型:{row['kind']} | 生成:{'Y' if row['can_generate'] else 'N'} "
            f"| 消费:{'Y' if row['can_consume'] else 'N'} | 已发现:{row['discovered_count']} | 自动同步:{'Y' if row['auto_sync_ready'] else 'N'}"
        )
        note = str(row.get("note") or "").strip()
        if note:
            lines.append(f"  note: {note}")

    lines.extend(
        [
            "",
            f"当前关注来源: {', '.join(cfg.follow_sources)}",
            f"当前赋予目标: {', '.join(cfg.grant_targets)}",
            "",
            "说明: 留空=保持不变；输入逗号分隔 id 会覆盖原配置。",
        ]
    )
    _show_text(stdscr, "生态授权管理", "\n".join(lines))

    changed = False
    messages: list[str] = []

    follow_raw = _prompt(stdscr, "设置关注来源（逗号分隔，留空保持）: ")
    if follow_raw:
        ids, unknown = _split_csv_with_unknown(follow_raw)
        cfg.ecosystem.follow_sources = normalize_follow_sources(ids)
        changed = True
        if unknown:
            messages.append(f"忽略未知来源: {', '.join(unknown)}")

    grant_raw = _prompt(stdscr, "设置赋予目标（逗号分隔，留空保持）: ")
    if grant_raw:
        ids, unknown = _split_csv_with_unknown(grant_raw)
        cfg.ecosystem.grant_targets = normalize_grant_targets(ids)
        changed = True
        if unknown:
            messages.append(f"忽略未知目标: {', '.join(unknown)}")

    if changed:
        save_config(cfg)
        messages.append("生态配置已保存。")
    else:
        messages.append("配置未变更。")
    return " | ".join(messages)


def _yes_no(raw: str, default_yes: bool) -> bool:
    text = raw.strip().lower()
    if not text:
        return default_yes
    return text in {"y", "yes", "1", "true", "on"}


def _track_overview_text(cfg: AppConfig) -> str:
    rows = list_tracked_sources(cfg, only_enabled=False)
    lines = [
        "外部追踪来源（tracked sources）:",
        "",
    ]
    if not rows:
        lines.append("- 暂无来源。")
    else:
        for row in rows:
            lines.append(
                f"- {row.id} | agent:{row.agent_id} | enabled:{'Y' if row.enabled else 'N'} | "
                f"name:{row.name} | source:{row.source}"
            )
            if row.note:
                lines.append(f"  note: {row.note}")
    lines.extend(
        [
            "",
            "操作说明:",
            "- a: 新增来源（可选常用 market）",
            "- l: 刷新列表",
            "- c: 检查更新（新增/下线）",
            "- i: 选择性导入",
            "- r: 删除来源",
            "- q: 返回",
        ]
    )
    return "\n".join(lines)


def _action_track(stdscr: curses.window, cfg: AppConfig) -> str:
    _show_text(stdscr, "外部追踪管理", _track_overview_text(cfg))
    op = _prompt(stdscr, "选择操作 [a/l/c/i/r/q]: ").strip().lower()
    if not op or op in {"q", "quit", "back"}:
        return "已返回。"

    if op in {"l", "list"}:
        _show_text(stdscr, "外部追踪管理", _track_overview_text(cfg))
        return "已刷新追踪列表。"

    if op in {"a", "add"}:
        source, source_note, market = _choose_source(stdscr, "选择要追踪的来源")
        if not source:
            return source_note or "已取消。"

        default_agent = "custom"
        default_name = "custom-source"
        if market:
            default_agent = market.agent_hint
            default_name = market.id

        agent = _prompt_default(stdscr, "agent id（例如 deepseek）", default_agent)
        name = _prompt_default(stdscr, "来源名称", default_name)
        if not agent or not name:
            return "输入不完整，已取消。"
        source_id = _prompt(stdscr, "source id（可空自动生成）: ")
        note = _prompt(stdscr, "备注（可空）: ")
        enabled = _yes_no(_prompt(stdscr, "启用该来源? [Y/n]: "), default_yes=True)
        auto_follow = _yes_no(_prompt(stdscr, "自动加入关注来源? [Y/n]: "), default_yes=True)
        try:
            row = add_tracked_source(
                cfg=cfg,
                agent_id=agent,
                name=name,
                source=source,
                source_id=source_id or None,
                enabled=enabled,
                note=note,
            )
        except ValueError as exc:
            return f"新增失败: {exc}"

        if auto_follow:
            merged = cfg.follow_sources + [row.agent_id]
            cfg.ecosystem.follow_sources = normalize_follow_sources(merged)
        save_config(cfg)
        if source_note:
            return f"已新增 tracked source: {row.id} | {source_note}"
        return f"已新增 tracked source: {row.id}"

    if op in {"c", "check"}:
        source_id = _prompt(stdscr, "仅检查 source id（可空=全部启用）: ")
        include_disabled = _yes_no(_prompt(stdscr, "包含 disabled? [y/N]: "), default_yes=False)
        update_snapshot = _yes_no(_prompt(stdscr, "更新快照? [Y/n]: "), default_yes=True)
        show_all = _yes_no(_prompt(stdscr, "显示全量技能? [y/N]: "), default_yes=False)
        try:
            results = check_tracked_sources(
                cfg=cfg,
                pool_dir=cfg.pool_path,
                source_id=source_id or None,
                only_enabled=not include_disabled,
                update_snapshot=update_snapshot,
            )
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            return f"检查失败: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"检查失败: {exc}"

        if not results:
            return "无可检查来源。"

        lines: list[str] = []
        for r in results:
            lines.append(
                f"[{r.source.id}] agent:{r.source.agent_id} total:{r.total} "
                f"+{len(r.added)} -{len(r.removed)} checked:{r.checked_at}"
            )
            if r.added:
                lines.append("  新增:")
                for item in r.added[:80]:
                    lines.append(f"    - {item}")
                if len(r.added) > 80:
                    lines.append(f"    ... 其余 {len(r.added) - 80} 条")
            if r.removed:
                lines.append("  下线:")
                for item in r.removed[:80]:
                    lines.append(f"    - {item}")
                if len(r.removed) > 80:
                    lines.append(f"    ... 其余 {len(r.removed) - 80} 条")
            if show_all:
                lines.append("  全量:")
                for item in r.all_items:
                    lines.append(f"    - {item.name} | {item.rel_dir}")
            lines.append("")
        _show_text(stdscr, "追踪检查结果", "\n".join(lines).strip())
        return f"检查完成，共 {len(results)} 个来源。"

    if op in {"i", "import"}:
        source_id = _prompt(stdscr, "source id: ")
        if not source_id:
            return "未输入 source id，已取消。"
        mode = _prompt(stdscr, "导入模式 [o]ne/[m]ulti/[a]ll: ").strip().lower()
        fetch_all = mode in {"a", "all"}
        selectors: list[str] = []
        if not fetch_all:
            raw = _prompt(stdscr, "输入 skill 名或 rel_dir（多个用逗号分隔）: ")
            selectors = [x.strip() for x in raw.split(",") if x.strip()]
            if not selectors:
                return "未输入 skill，已取消。"
        try:
            imported = import_from_tracked_source(
                cfg=cfg,
                pool_dir=cfg.pool_path,
                source_id=source_id,
                selectors=selectors,
                fetch_all=fetch_all,
            )
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            return f"导入失败: {exc}"

        _show_text(stdscr, "导入结果", "\n".join([f"- {x}" for x in imported]) or "无")
        return f"导入完成，共 {len(imported)} 个。"

    if op in {"r", "remove", "delete"}:
        source_id = _prompt(stdscr, "要删除的 source id: ")
        if not source_id:
            return "未输入 source id，已取消。"
        confirm = _yes_no(_prompt(stdscr, "确认删除? [y/N]: "), default_yes=False)
        if not confirm:
            return "已取消删除。"
        ok = remove_tracked_source(cfg, source_id)
        if not ok:
            return f"未找到 tracked source: {source_id}"
        save_config(cfg)
        return f"已删除 tracked source: {source_id}"

    return "未知操作，已取消。"


def _action_maintain(stdscr: curses.window, cfg: AppConfig) -> str:
    do_prune = _prompt(stdscr, "先清理 dist 失效链接? [y/N]: ").lower() in {"y", "yes"}
    removed = 0
    if do_prune:
        removed = prune_broken_dist_symlinks(cfg.pool_path)
    result = audit_pool(cfg.pool_path)
    paths = write_audit_report(cfg.pool_path, result)

    lines = [
        f"问题总数: {len(result.issues)}",
        f"清理失效链接: {removed}",
        "",
        "Summary:",
    ]
    if result.summary:
        for k, v in result.summary.items():
            lines.append(f"- {k}: {v}")
    else:
        lines.append("- 无")
    _show_text(stdscr, "维护检查结果", "\n".join(lines))
    return f"维护报告已更新: {paths['latest_json']}"


def _loop(stdscr: curses.window, cfg: AppConfig) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)
    selected = 0
    status = "就绪。"

    while True:
        _draw(stdscr, selected, status, cfg)
        key = stdscr.getch()

        if key in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % len(MENU_ITEMS)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = (selected + 1) % len(MENU_ITEMS)
        elif key in (ord("q"), 27):
            return
        elif key in (ord("1"), ord("2"), ord("3"), ord("4"), ord("5"), ord("6"), ord("7"), ord("8"), ord("9")):
            selected = int(chr(key)) - 1
            key = 10

        if key in (10, 13, curses.KEY_ENTER):
            if selected == 0:
                status = _action_scan(stdscr, cfg)
            elif selected == 1:
                status = _action_overview(stdscr, cfg)
            elif selected == 2:
                status = _action_promote(stdscr, cfg)
            elif selected == 3:
                status = _action_index_fetch(stdscr, cfg)
            elif selected == 4:
                status = _action_sync(stdscr, cfg)
            elif selected == 5:
                status = _action_ecosystem(stdscr, cfg)
            elif selected == 6:
                status = _action_track(stdscr, cfg)
            elif selected == 7:
                status = _action_maintain(stdscr, cfg)
            elif selected == 8:
                return


def run_ui(cfg: AppConfig) -> None:
    ensure_pool_layout(cfg.pool_path)
    try:
        locale.setlocale(locale.LC_ALL, "")
    except Exception:
        pass
    curses.wrapper(_loop, cfg)
