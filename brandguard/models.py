"""Structured incident record exchanged between agents (Section 8: Hand-offs)."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Case:
    case_id: str
    timestamp: str = ""
    platform: str = ""
    author_type_raw: str = ""
    author_class: str = ""
    initial_reach: str = ""
    current_reach: str = ""
    text: str = ""
    likes: int = 0
    comments: int = 0
    shares: int = 0
    mentions_1h: int | None = None
    mentions_6h: int | None = None
    parent_case_id: str | None = None
    stage: str = "NEW"
    status: str = "NEW"
    owner: str = ""
    velocity_growth: float | None = None
    velocity_valid: bool = True
    incident_type: str = ""
    type_confidence: float = 0.0
    classifier_notes: list = field(default_factory=list)
    entities: dict = field(default_factory=dict)
    injection_detected: bool = False
    coordination_detected: bool = False
    duplicate_of: str | None = None
    verification: dict = field(default_factory=dict)
    scores: dict = field(default_factory=dict)
    raw_score: int | None = None
    base_tier: str = ""
    overrides: list = field(default_factory=list)
    tier: str = ""
    scoring_withheld: bool = False
    provisional: bool = False
    exceptions: list = field(default_factory=list)
    gate: str = ""
    autonomy: str = ""
    route: str = ""
    approver: str | None = None
    legal_flag: bool = False
    template_id: str | None = None
    plan_action: str = ""
    decision_reasons: list = field(default_factory=list)
    draft: dict = field(default_factory=dict)
    draft_versions: list = field(default_factory=list)
    case_brief: dict = field(default_factory=dict)
    approval: dict = field(default_factory=dict)
    sent: bool = False
    events: list = field(default_factory=list)
    monitoring: dict = field(default_factory=dict)
    closure: dict = field(default_factory=dict)
    final_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Case":
        return cls(**d)


def author_class(raw: str) -> str:
    r = (raw or "").lower()
    if "journalist" in r or "media" in r:
        return "MEDIA"
    if "verified" in r or "influen" in r or "high-reach" in r:
        return "INFLUENTIAL"
    if "identified" in r:
        return "CUSTOMER"
    return "ANONYMOUS"
