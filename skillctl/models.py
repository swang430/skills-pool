from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SkillRecord:
    agent: str
    scope: str
    name: str
    description: str
    source_type: str
    enabled: bool | None
    skill_dir: str
    skill_md: str
    sha256: str
    discovered_at: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PromoteResult:
    success: bool
    message: str
    source: str = ""
    destination: str = ""
    action: str = ""
