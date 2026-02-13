from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MarketEntry:
    id: str
    name: str
    source: str
    agent_hint: str
    description: str


@dataclass(frozen=True)
class MarketView:
    id: str
    name: str
    source: str
    agent_hint: str
    description: str
    exists: bool


BUILTIN_MARKETS: tuple[MarketEntry, ...] = (
    MarketEntry(
        id="openai-skills",
        name="OpenAI Skills (GitHub)",
        source="https://github.com/openai/skills.git",
        agent_hint="openai",
        description="OpenAI 官方 skills 仓库（远程 GitHub）。",
    ),
    MarketEntry(
        id="claude-marketplace",
        name="Claude Skill Marketplace (local)",
        source="~/.claude/plugins/marketplaces",
        agent_hint="claude",
        description="Claude 插件市场目录（包含 marketplace skills）。",
    ),
    MarketEntry(
        id="claude-skills",
        name="Claude Skills (local)",
        source="~/.claude/skills",
        agent_hint="claude",
        description="Claude 用户级 skills 目录。",
    ),
    MarketEntry(
        id="gemini-market-repos",
        name="Gemini Repos (local)",
        source="~/.gemini/skills/_repos",
        agent_hint="gemini",
        description="Gemini 已拉取的 skills repo 聚合目录。",
    ),
    MarketEntry(
        id="gemini-skills",
        name="Gemini Skills (local)",
        source="~/.gemini/skills",
        agent_hint="gemini",
        description="Gemini 用户级 skills 目录。",
    ),
    MarketEntry(
        id="antigravity-skills",
        name="Antigravity Skills (local)",
        source="~/.gemini/antigravity/skills",
        agent_hint="antigravity",
        description="Antigravity skills 目录。",
    ),
    MarketEntry(
        id="codex-skills",
        name="Codex Skills (local)",
        source="~/.codex/skills",
        agent_hint="codex",
        description="Codex 用户级 skills 目录。",
    ),
)

MARKET_ALIASES: dict[str, str] = {
    "openai-skill": "openai-skills",
    "openai": "openai-skills",
}


def _resolve_source(raw: str) -> str:
    path = Path(raw).expanduser()
    if path.is_absolute() or raw.startswith("~"):
        return str(path)
    return raw


def _is_remote_source(raw: str) -> bool:
    s = raw.strip().lower()
    return (
        s.startswith("http://")
        or s.startswith("https://")
        or s.startswith("git@")
        or s.endswith(".git")
    )


def list_market_entries() -> tuple[MarketEntry, ...]:
    return BUILTIN_MARKETS


def list_market_views(include_missing: bool = True) -> list[MarketView]:
    rows: list[MarketView] = []
    for item in BUILTIN_MARKETS:
        resolved = _resolve_source(item.source)
        exists = _is_remote_source(resolved) or Path(resolved).exists()
        view = MarketView(
            id=item.id,
            name=item.name,
            source=resolved,
            agent_hint=item.agent_hint,
            description=item.description,
            exists=exists,
        )
        if include_missing or exists:
            rows.append(view)
    return rows


def get_market_view(market_id: str) -> MarketView | None:
    key = market_id.strip().lower()
    key = MARKET_ALIASES.get(key, key)
    for item in list_market_views(include_missing=True):
        if item.id == key:
            return item
    return None
