from __future__ import annotations

import argparse
import re
from pathlib import Path

from .config import ensure_pool_layout, initialize_config, load_config, save_config
from .ecosystem import (
    DEFAULT_FOLLOW_SOURCE_IDS,
    DEFAULT_GRANT_TARGET_IDS,
    ecosystem_status_rows,
    granted_platforms,
    non_auto_targets,
    normalize_follow_sources,
    normalize_grant_targets,
)
from .indexing import discover_market_sources, fetch_from_source, index_source, write_index_report
from .markets import get_market_view, list_market_views
from .maintenance import audit_pool, delete_pool_skill, prune_broken_dist_symlinks, write_audit_report
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
from .ui import run_ui


def _cmd_init(args: argparse.Namespace) -> int:
    pool_dir = args.pool_dir
    if not pool_dir:
        user_input = input("请输入 skills pool 目录（回车使用默认）: ").strip()
        pool_dir = user_input or None

    cfg, cfg_path = initialize_config(pool_dir=pool_dir)
    print(f"配置已写入: {cfg_path}")
    print(f"Pool 目录: {cfg.pool_path}")
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    cfg = load_config()
    ensure_pool_layout(cfg.pool_path)

    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd()
    records = scan_environment(workspace)
    payload = build_inventory_payload(records, workspace)
    paths = write_inventory_reports(cfg.pool_path, payload)

    print(f"扫描完成，共 {len(records)} 条记录。")
    print(f"最新 JSON: {paths['latest_json']}")
    print(f"最新 MD:   {paths['latest_md']}")
    return 0


def _cmd_promote(args: argparse.Namespace) -> int:
    cfg = load_config()
    ensure_pool_layout(cfg.pool_path)
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd()

    if args.list:
        skills = discover_project_skills(workspace)
        if not skills:
            print("当前项目未发现常见 skills 路径。")
            return 0
        print("发现的项目 skills:")
        for idx, path in enumerate(skills, start=1):
            print(f"{idx}. {path}")
        return 0

    if not args.path:
        print("请提供要升格的路径，或使用 --list 先查看。")
        return 1

    result = promote_skill(args.path, cfg.pool_path, workspace)
    if result.success:
        print(result.message)
        print(f"source: {result.source}")
        print(f"target: {result.destination}")
        return 0
    print(f"失败: {result.message}")
    return 2


def _cmd_sync(args: argparse.Namespace) -> int:
    cfg = load_config()
    ensure_pool_layout(cfg.pool_path)
    allowed = granted_platforms(cfg.grant_targets)
    try:
        result = run_sync(
            pool_dir=cfg.pool_path,
            dry_run=args.dry_run,
            prune=args.prune,
            only=args.only,
            backup_conflicts=args.backup_conflicts,
            allowed_platforms=allowed,
        )
    except FileNotFoundError as exc:
        print(str(exc))
        return 2

    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip())
    manual_targets = non_auto_targets(cfg.grant_targets)
    if manual_targets:
        names = ", ".join(item.name for item in manual_targets)
        print(f"提示: 以下目标无自动安装路径，仅记录授权: {names}")
    return result.returncode


def _cmd_ui(_args: argparse.Namespace) -> int:
    cfg = load_config()
    ensure_pool_layout(cfg.pool_path)
    run_ui(cfg)
    return 0


def _cmd_web(args: argparse.Namespace) -> int:
    cfg = load_config()
    ensure_pool_layout(cfg.pool_path)
    try:
        from .web import run_web
    except ModuleNotFoundError as exc:
        print(f"缺少 web 依赖，请安装 fastapi/uvicorn: {exc}")
        return 2
    run_web(host=args.host, port=args.port)
    return 0


def _cmd_maintain(args: argparse.Namespace) -> int:
    cfg = load_config()
    ensure_pool_layout(cfg.pool_path)

    if args.delete:
        try:
            dst = delete_pool_skill(cfg.pool_path, args.delete)
        except FileNotFoundError as exc:
            print(str(exc))
            return 2
        print(f"已移动到回收站: {dst}")
        return 0

    if args.prune_broken:
        removed = prune_broken_dist_symlinks(cfg.pool_path)
        print(f"已清理失效链接: {removed}")

    result = audit_pool(cfg.pool_path)
    paths = write_audit_report(cfg.pool_path, result)
    print(f"维护检查完成，问题数: {len(result.issues)}")
    print(f"最新 JSON: {paths['latest_json']}")
    print(f"最新 MD:   {paths['latest_md']}")
    return 0


def _cmd_index(args: argparse.Namespace) -> int:
    cfg = load_config()
    ensure_pool_layout(cfg.pool_path)

    if args.markets:
        rows = list_market_views(include_missing=True)
        print("常用 Skills Market:")
        for idx, item in enumerate(rows, start=1):
            status = "可用" if item.exists else "不可用"
            print(f"{idx}. {item.id} | {item.name} | {status}")
            print(f"   source: {item.source}")
            print(f"   agent:  {item.agent_hint}")
            print(f"   note:   {item.description}")

        sources = discover_market_sources()
        extras: list[str] = []
        known = {item.source for item in rows}
        for src in sources:
            if str(src) not in known:
                extras.append(str(src))
        if extras:
            print("")
            print("自动发现的其他来源:")
            for idx, src in enumerate(extras, start=1):
                print(f"{idx}. {src}")
        return 0

    try:
        source, source_note = _resolve_source(source=args.source, market=args.market)
    except ValueError as exc:
        print(str(exc))
        return 2
    if not source:
        print("请提供 --source <git地址或本地路径> 或 --market <market_id>，可先用 --markets 查看。")
        return 1

    _root, items = index_source(source, proxy=cfg.proxy_url, no_proxy=cfg.no_proxy)
    paths = write_index_report(cfg.pool_path, source, items)
    if source_note:
        print(f"使用来源: {source_note}")
    print(f"索引完成，共 {len(items)} 个 skill。")
    for idx, item in enumerate(items[:50], start=1):
        print(f"{idx}. {item.name} | {item.rel_dir}")
    if len(items) > 50:
        print(f"... 其余 {len(items) - 50} 条请查看 JSON")
    print(f"最新索引: {paths['latest_json']}")
    return 0


def _cmd_fetch(args: argparse.Namespace) -> int:
    cfg = load_config()
    ensure_pool_layout(cfg.pool_path)

    try:
        source, source_note = _resolve_source(source=args.source, market=args.market)
    except ValueError as exc:
        print(str(exc))
        return 2
    if not source:
        print("请提供 --source <git地址或本地路径> 或 --market <market_id>")
        return 1
    try:
        promoted = fetch_from_source(
            source=source,
            pool_dir=cfg.pool_path,
            selector=args.skill,
            fetch_all=args.all,
            proxy=cfg.proxy_url,
            no_proxy=cfg.no_proxy,
        )
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        print(f"下载失败: {exc}")
        return 2

    if source_note:
        print(f"使用来源: {source_note}")
    print(f"下载/导入完成，共 {len(promoted)} 个 skill。")
    for path in promoted:
        print(f"- {path}")
    return 0


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _resolve_source(source: str | None, market: str | None) -> tuple[str | None, str]:
    src = (source or "").strip()
    market_id = (market or "").strip().lower()
    if src and market_id:
        raise ValueError("不能同时指定 --source 和 --market。")
    if src:
        return src, ""
    if not market_id:
        return None, ""

    item = get_market_view(market_id)
    if not item:
        raise ValueError(f"未知 market: {market_id}（先用 --markets 查看）")
    if not item.exists:
        raise ValueError(f"market 不可用（路径不存在）: {item.id} -> {item.source}")
    return item.source, f"{item.name} ({item.id})"


def _split_csv_with_unknown(raw: str) -> tuple[list[str], list[str]]:
    ids: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        key = part.strip().lower()
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        if _ID_RE.match(key):
            ids.append(key)
        else:
            unknown.append(key)
    return ids, unknown


def _cmd_ecosystem(args: argparse.Namespace) -> int:
    cfg = load_config()
    ensure_pool_layout(cfg.pool_path)
    changed = False

    if args.reset:
        cfg.ecosystem.follow_sources = list(DEFAULT_FOLLOW_SOURCE_IDS)
        cfg.ecosystem.grant_targets = list(DEFAULT_GRANT_TARGET_IDS)
        changed = True

    if args.follow is not None:
        ids, unknown = _split_csv_with_unknown(args.follow)
        cfg.ecosystem.follow_sources = normalize_follow_sources(ids)
        changed = True
        if unknown:
            print(f"已忽略未知来源: {', '.join(unknown)}")

    if args.grant is not None:
        ids, unknown = _split_csv_with_unknown(args.grant)
        cfg.ecosystem.grant_targets = normalize_grant_targets(ids)
        changed = True
        if unknown:
            print(f"已忽略未知目标: {', '.join(unknown)}")

    if changed:
        save_config(cfg)

    payload = load_latest_inventory(cfg.pool_path)
    available = parse_target_platforms(cfg.pool_path)
    rows = ecosystem_status_rows(
        follow_sources=cfg.follow_sources,
        grant_targets=cfg.grant_targets,
        inventory_payload=payload,
        available_platforms=available,
    )

    print("生态配置:")
    print(f"- 关注来源: {', '.join(cfg.follow_sources) if cfg.follow_sources else '(空)'}")
    print(f"- 赋予目标: {', '.join(cfg.grant_targets) if cfg.grant_targets else '(空)'}")
    print(f"- 可自动同步平台: {', '.join(granted_platforms(cfg.grant_targets)) or '(无)'}")
    print(f"- 代理: {cfg.proxy_url or '(未设置)'}")
    print(f"- NO_PROXY: {cfg.no_proxy or '(未设置)'}")
    if not payload:
        print("- 提示: 尚无 inventory，建议先运行 scan。")

    print("")
    print("能力与状态:")
    for row in rows:
        gen = "Y" if row["can_generate"] else "N"
        use = "Y" if row["can_consume"] else "N"
        followed = "Y" if row["followed"] else "N"
        granted = "Y" if row["granted"] else "N"
        auto = "Y" if row["auto_sync_ready"] else "N"
        print(
            f"- {row['name']} ({row['id']}) | 类型:{row['kind']} | 生成:{gen} | 消费:{use} "
            f"| 关注:{followed} | 赋予:{granted} | 自动同步:{auto} | 已发现:{row['discovered_count']}"
        )
        note = str(row.get("note") or "").strip()
        if note:
            print(f"  note: {note}")
    return 0


def _cmd_track_add(args: argparse.Namespace) -> int:
    cfg = load_config()
    ensure_pool_layout(cfg.pool_path)

    try:
        source, source_note = _resolve_source(source=args.source, market=args.market)
    except ValueError as exc:
        print(str(exc))
        return 2
    if not source:
        print("请提供 --source 或 --market。")
        return 1

    row = add_tracked_source(
        cfg=cfg,
        agent_id=args.agent,
        name=args.name,
        source=source,
        source_id=args.id,
        enabled=not args.disabled,
        note=args.note or "",
    )
    if not args.no_follow:
        merged = cfg.follow_sources + [row.agent_id]
        cfg.ecosystem.follow_sources = normalize_follow_sources(merged)
    save_config(cfg)

    print("已新增 tracked source:")
    print(f"- id: {row.id}")
    print(f"- agent: {row.agent_id}")
    print(f"- name: {row.name}")
    print(f"- source: {row.source}")
    print(f"- enabled: {row.enabled}")
    if source_note:
        print(f"- market: {source_note}")
    return 0


def _cmd_track_list(args: argparse.Namespace) -> int:
    cfg = load_config()
    ensure_pool_layout(cfg.pool_path)
    rows = list_tracked_sources(cfg, only_enabled=not args.all)
    if not rows:
        print("暂无 tracked sources。")
        return 0

    print("tracked sources:")
    for row in rows:
        print(
            f"- {row.id} | agent:{row.agent_id} | enabled:{'Y' if row.enabled else 'N'} | "
            f"name:{row.name} | source:{row.source}"
        )
        if row.note:
            print(f"  note: {row.note}")
    return 0


def _cmd_track_remove(args: argparse.Namespace) -> int:
    cfg = load_config()
    ensure_pool_layout(cfg.pool_path)
    ok = remove_tracked_source(cfg, args.id)
    if not ok:
        print(f"未找到 tracked source: {args.id}")
        return 2
    save_config(cfg)
    print(f"已删除 tracked source: {args.id}")
    return 0


def _cmd_track_check(args: argparse.Namespace) -> int:
    cfg = load_config()
    ensure_pool_layout(cfg.pool_path)
    try:
        results = check_tracked_sources(
            cfg=cfg,
            pool_dir=cfg.pool_path,
            source_id=args.id,
            only_enabled=not args.all,
            update_snapshot=not args.no_update,
            proxy=cfg.proxy_url,
            no_proxy=cfg.no_proxy,
        )
    except FileNotFoundError as exc:
        print(str(exc))
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"检查失败: {exc}")
        return 2

    if not results:
        print("无可检查来源。")
        return 0

    for r in results:
        print(
            f"[{r.source.id}] agent:{r.source.agent_id} total:{r.total} +{len(r.added)} -{len(r.removed)} checked:{r.checked_at}"
        )
        if r.added:
            print("  新增:")
            for x in r.added[:50]:
                print(f"    - {x}")
            if len(r.added) > 50:
                print(f"    ... 其余 {len(r.added) - 50} 条")
        if r.removed:
            print("  下线:")
            for x in r.removed[:50]:
                print(f"    - {x}")
            if len(r.removed) > 50:
                print(f"    ... 其余 {len(r.removed) - 50} 条")
        if args.show_all:
            print("  全量:")
            for item in r.all_items:
                print(f"    - {item.name} | {item.rel_dir}")
    return 0


def _cmd_track_import(args: argparse.Namespace) -> int:
    cfg = load_config()
    ensure_pool_layout(cfg.pool_path)
    try:
        imported = import_from_tracked_source(
            cfg=cfg,
            pool_dir=cfg.pool_path,
            source_id=args.id,
            selectors=args.skill or [],
            fetch_all=args.all,
            proxy=cfg.proxy_url,
            no_proxy=cfg.no_proxy,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"导入失败: {exc}")
        return 2

    print(f"导入完成，共 {len(imported)} 个。")
    for item in imported:
        print(f"- {item}")
    return 0


def _cmd_proxy(args: argparse.Namespace) -> int:
    cfg = load_config()
    ensure_pool_layout(cfg.pool_path)
    changed = False

    if args.clear:
        if cfg.proxy_url or cfg.no_proxy:
            cfg.network.proxy_url = ""
            cfg.network.no_proxy = ""
            changed = True

    if args.set_proxy is not None:
        value = args.set_proxy.strip()
        if value != cfg.proxy_url:
            cfg.network.proxy_url = value
            changed = True

    if args.no_proxy is not None:
        value = args.no_proxy.strip()
        if value != cfg.no_proxy:
            cfg.network.no_proxy = value
            changed = True

    if changed:
        save_config(cfg)

    print("代理配置:")
    print(f"- proxy_url: {cfg.proxy_url or '(未设置)'}")
    print(f"- no_proxy: {cfg.no_proxy or '(未设置)'}")
    print("- 作用范围: index / fetch / track check / track import")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillctl",
        description="轻量本地 Skill 管理器（扫描 / 索引 / 下载 / 升格 / 同步 / 维护 / 生态管理 / 外部追踪 / TUI / Web）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="初始化配置与 pool 目录")
    p_init.add_argument("--pool-dir", help="skills pool 根目录")
    p_init.set_defaults(func=_cmd_init)

    p_scan = sub.add_parser("scan", help="扫描当前环境 skills 并记录报告")
    p_scan.add_argument("--workspace", help="指定工作区目录（默认当前目录）")
    p_scan.set_defaults(func=_cmd_scan)

    p_promote = sub.add_parser("promote", help="将项目 skill 升格到统一池")
    p_promote.add_argument("path", nargs="?", help="skill 目录或 SKILL.md 路径")
    p_promote.add_argument("--workspace", help="指定工作区目录（默认当前目录）")
    p_promote.add_argument("--list", action="store_true", help="先列出项目下发现的 skills")
    p_promote.set_defaults(func=_cmd_promote)

    p_sync = sub.add_parser("sync", help="调用 link-skills.sh 同步到各 Agent")
    p_sync.add_argument("--dry-run", action="store_true", help="仅预览，不落盘")
    p_sync.add_argument("--prune", action="store_true", help="清理过期软链接")
    p_sync.add_argument("--only", help="仅同步指定平台，例如 codex,gemini")
    p_sync.add_argument("--backup-conflicts", action="store_true", help="冲突时自动备份")
    p_sync.set_defaults(func=_cmd_sync)

    p_eco = sub.add_parser("ecosystem", help="管理生态配置（关注来源 / 赋予目标 / 能力状态）")
    p_eco.add_argument("--follow", help="设置关注来源 id 列表（逗号分隔）")
    p_eco.add_argument("--grant", help="设置赋予目标 id 列表（逗号分隔）")
    p_eco.add_argument("--reset", action="store_true", help="重置为默认来源与默认目标")
    p_eco.set_defaults(func=_cmd_ecosystem)

    p_track = sub.add_parser("track", help="管理外部来源追踪（新增 Agent、检查动态、选择导入）")
    sub_track = p_track.add_subparsers(dest="track_command", required=True)

    p_track_add = sub_track.add_parser("add", help="新增要追踪的来源（source 或 market 二选一）")
    p_track_add.add_argument("--agent", required=True, help="来源所属 agent id，例如 deepseek")
    p_track_add.add_argument("--name", required=True, help="来源名称")
    p_track_add.add_argument("--source", help="git 地址或本地目录")
    p_track_add.add_argument("--market", help="使用常用 market id 作为来源（先用 index --markets 查看）")
    p_track_add.add_argument("--id", help="自定义 source id（默认自动生成）")
    p_track_add.add_argument("--note", help="备注")
    p_track_add.add_argument("--disabled", action="store_true", help="新增时先禁用")
    p_track_add.add_argument("--no-follow", action="store_true", help="新增后不自动加入关注来源")
    p_track_add.set_defaults(func=_cmd_track_add)

    p_track_list = sub_track.add_parser("list", help="列出 tracked sources")
    p_track_list.add_argument("--all", action="store_true", help="包含 disabled 来源")
    p_track_list.set_defaults(func=_cmd_track_list)

    p_track_remove = sub_track.add_parser("remove", help="删除 tracked source")
    p_track_remove.add_argument("--id", required=True, help="source id")
    p_track_remove.set_defaults(func=_cmd_track_remove)

    p_track_check = sub_track.add_parser("check", help="检查来源变化（新增/下线）")
    p_track_check.add_argument("--id", help="仅检查一个 source id")
    p_track_check.add_argument("--all", action="store_true", help="包含 disabled 来源")
    p_track_check.add_argument("--show-all", action="store_true", help="打印全量技能列表")
    p_track_check.add_argument("--no-update", action="store_true", help="只检查不更新快照")
    p_track_check.set_defaults(func=_cmd_track_check)

    p_track_import = sub_track.add_parser("import", help="从 tracked source 选择性导入")
    p_track_import.add_argument("--id", required=True, help="source id")
    p_track_import.add_argument("--skill", action="append", help="要导入的 skill 名或 rel_dir，可重复")
    p_track_import.add_argument("--all", action="store_true", help="导入该来源全部 skills")
    p_track_import.set_defaults(func=_cmd_track_import)

    p_proxy = sub.add_parser("proxy", help="管理外网访问代理（用于 index/fetch/track）")
    p_proxy.add_argument("--set", dest="set_proxy", help="设置代理地址，例如 http://127.0.0.1:7897")
    p_proxy.add_argument("--no-proxy", help="设置 NO_PROXY，例如 localhost,127.0.0.1")
    p_proxy.add_argument("--clear", action="store_true", help="清空代理配置")
    p_proxy.set_defaults(func=_cmd_proxy)

    p_index = sub.add_parser("index", help="索引外部 skill 来源（git、本地、market）")
    p_index.add_argument("--source", help="git 地址或本地目录")
    p_index.add_argument("--market", help="常用 market id（与 --source 二选一）")
    p_index.add_argument("--markets", action="store_true", help="列出常用 market 列表（含可用状态）")
    p_index.set_defaults(func=_cmd_index)

    p_fetch = sub.add_parser("fetch", help="从外部来源选择性下载 skill 到统一池")
    p_fetch.add_argument("--source", help="git 地址或本地目录")
    p_fetch.add_argument("--market", help="常用 market id（与 --source 二选一）")
    p_fetch.add_argument("--skill", help="指定 skill 名或相对目录")
    p_fetch.add_argument("--all", action="store_true", help="下载来源中的全部 skills")
    p_fetch.set_defaults(func=_cmd_fetch)

    p_maintain = sub.add_parser("maintain", help="日常维护（体检、清理失效链接、回收站删除）")
    p_maintain.add_argument("--prune-broken", action="store_true", help="先清理 dist 下失效软链接")
    p_maintain.add_argument("--delete", help="删除池内某个 skill（移动到 trash）")
    p_maintain.set_defaults(func=_cmd_maintain)

    p_ui = sub.add_parser("ui", help="打开本地 TUI")
    p_ui.set_defaults(func=_cmd_ui)

    p_web = sub.add_parser("web", help="启动本地浏览器 UI（FastAPI 单机服务）")
    p_web.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    p_web.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    p_web.set_defaults(func=_cmd_web)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
