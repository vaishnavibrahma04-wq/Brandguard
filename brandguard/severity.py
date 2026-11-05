"""Stage 3b - deterministic severity rubric and mandatory overrides (Section 10). No model judgement here."""
from __future__ import annotations
from .config import (ISSUE_RISK_POINTS, SOURCE_POINTS, VERIFICATION_POINTS, TIER_THRESHOLDS, TIER_ORDER, VELOCITY_ALERT_PCT)


def velocity_growth(m1, m6):
    """((Mentions_1h*6)/Mentions_6h - 1)*100; None when counts missing/invalid (never invent a velocity)."""
    if m1 is None or m6 is None or m1 < 0 or m6 <= 0 or m1 > m6:
        return None
    return ((m1 * 6) / m6 - 1) * 100


def reach_velocity_points(reach: str, growth: float) -> int:
    if reach == "High" or growth >= VELOCITY_ALERT_PCT:
        return 30
    if reach == "Negligible":
        return 0
    return 20 if growth >= 25 else 10


def tier_for(score: int) -> str:
    for t, floor in TIER_THRESHOLDS:
        if score >= floor:
            return t
    return "S4"


def at_least(tier: str, minimum: str) -> str:
    return tier if TIER_ORDER.index(tier) <= TIER_ORDER.index(minimum) else minimum


def score(incident_type: str, author_class: str, verification_status: str, reach: str, growth: float) -> dict:
    s = {"reach_velocity": reach_velocity_points(reach, growth), "issue_risk": ISSUE_RISK_POINTS[incident_type],
         "source_type": SOURCE_POINTS[author_class], "verification": VERIFICATION_POINTS[verification_status]}
    raw = sum(s.values())
    return {"scores": s, "raw": raw, "base_tier": tier_for(raw)}
