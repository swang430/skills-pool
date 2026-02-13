from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .ecosystem import (
    DEFAULT_FOLLOW_SOURCE_IDS,
    DEFAULT_GRANT_TARGET_IDS,
    known_sync_platforms,
    normalize_follow_sources,
    normalize_grant_targets,
)


@dataclass
class EcosystemPrefs:
    follow_sources: list[str] = field(default_factory=list)
    grant_targets: list[str] = field(default_factory=list)


@dataclass
class NetworkPrefs:
    proxy_url: str = ""
    no_proxy: str = ""


@dataclass
class TrackedSource:
    id: str
    agent_id: str
    name: str
    source: str
    enabled: bool = True
    note: str = ""


@dataclass
class AppConfig:
    pool_dir: str
    manager_root: str
    ecosystem: EcosystemPrefs = field(default_factory=EcosystemPrefs)
    network: NetworkPrefs = field(default_factory=NetworkPrefs)
    tracked_sources: list[TrackedSource] = field(default_factory=list)

    @property
    def pool_path(self) -> Path:
        return Path(self.pool_dir).expanduser().resolve()

    @property
    def manager_root_path(self) -> Path:
        return Path(self.manager_root).expanduser().resolve()

    @property
    def follow_sources(self) -> list[str]:
        return normalize_follow_sources(self.ecosystem.follow_sources)

    @property
    def grant_targets(self) -> list[str]:
        return normalize_grant_targets(self.ecosystem.grant_targets)

    @property
    def proxy_url(self) -> str:
        return str(self.network.proxy_url).strip()

    @property
    def no_proxy(self) -> str:
        return str(self.network.no_proxy).strip()


def default_manager_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_pool_dir() -> Path:
    return default_manager_root()


def config_path() -> Path:
    env_path = Path("~/.config/skillctl/config.json")
    return env_path.expanduser()


def default_config() -> AppConfig:
    return AppConfig(
        pool_dir=str(default_pool_dir()),
        manager_root=str(default_manager_root()),
        ecosystem=EcosystemPrefs(
            follow_sources=list(DEFAULT_FOLLOW_SOURCE_IDS),
            grant_targets=list(DEFAULT_GRANT_TARGET_IDS),
        ),
        network=NetworkPrefs(),
    )


def _to_list(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        return [item.strip() for item in text.split(",") if item.strip()]
    return []


def _to_bool(raw: Any, default: bool = True) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _load_tracked_sources(raw: object) -> list[TrackedSource]:
    if not isinstance(raw, list):
        return []
    rows: list[TrackedSource] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id", "")).strip().lower()
        agent_id = str(item.get("agent_id", "")).strip().lower()
        name = str(item.get("name", "")).strip()
        source = str(item.get("source", "")).strip()
        note = str(item.get("note", "")).strip()
        if not sid or not agent_id or not name or not source:
            continue
        if sid in seen:
            continue
        seen.add(sid)
        rows.append(
            TrackedSource(
                id=sid,
                agent_id=agent_id,
                name=name,
                source=source,
                enabled=_to_bool(item.get("enabled"), default=True),
                note=note,
            )
        )
    return rows


def load_config() -> AppConfig:
    cfg_path = config_path()
    if not cfg_path.exists():
        return default_config()

    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default_config()

    pool_dir = data.get("pool_dir", str(default_pool_dir()))
    manager_root = data.get("manager_root", str(default_manager_root()))

    eco_data = data.get("ecosystem", {})
    if not isinstance(eco_data, dict):
        eco_data = {}

    raw_follow = eco_data.get("follow_sources", data.get("follow_sources"))
    raw_grant = eco_data.get("grant_targets", data.get("grant_targets"))
    ecosystem = EcosystemPrefs(
        follow_sources=normalize_follow_sources(_to_list(raw_follow)) or list(DEFAULT_FOLLOW_SOURCE_IDS),
        grant_targets=normalize_grant_targets(_to_list(raw_grant)) or list(DEFAULT_GRANT_TARGET_IDS),
    )

    network_data = data.get("network", {})
    if not isinstance(network_data, dict):
        network_data = {}
    raw_proxy = network_data.get("proxy_url", data.get("proxy_url", data.get("proxy", "")))
    raw_no_proxy = network_data.get("no_proxy", data.get("no_proxy", ""))
    network = NetworkPrefs(
        proxy_url=str(raw_proxy or "").strip(),
        no_proxy=str(raw_no_proxy or "").strip(),
    )

    tracking_data = data.get("tracking", {})
    if isinstance(tracking_data, dict):
        raw_sources = tracking_data.get("sources", data.get("tracked_sources", []))
    else:
        raw_sources = data.get("tracked_sources", [])
    tracked_sources = _load_tracked_sources(raw_sources)

    return AppConfig(
        pool_dir=pool_dir,
        manager_root=manager_root,
        ecosystem=ecosystem,
        network=network,
        tracked_sources=tracked_sources,
    )


def save_config(cfg: AppConfig) -> Path:
    cfg_path = config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(asdict(cfg), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return cfg_path


def initialize_config(pool_dir: str | None = None) -> tuple[AppConfig, Path]:
    cfg = load_config()
    if pool_dir:
        cfg.pool_dir = str(Path(pool_dir).expanduser().resolve())
    cfg.ecosystem.follow_sources = normalize_follow_sources(cfg.ecosystem.follow_sources) or list(DEFAULT_FOLLOW_SOURCE_IDS)
    cfg.ecosystem.grant_targets = normalize_grant_targets(cfg.ecosystem.grant_targets) or list(DEFAULT_GRANT_TARGET_IDS)
    ensure_pool_layout(cfg.pool_path)
    return cfg, save_config(cfg)


def ensure_pool_layout(pool_path: Path) -> None:
    # `skills`: 统一池；`state`: 清单与审计；`index`: 外部索引缓存；`trash`: 软删除回收站
    base_dirs = ["skills", "state", "index", "trash"]
    dist_dirs = [f"dist/{name}" for name in known_sync_platforms()]
    for rel in [*base_dirs, *dist_dirs]:
        (pool_path / rel).mkdir(parents=True, exist_ok=True)
