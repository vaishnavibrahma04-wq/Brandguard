"""Stage 1 - Incident intake: accept an incident record and validate required fields."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from .config import REACH_TIERS, KNOWN_PLATFORMS
from .models import Case, author_class

REQUIRED = ["case_id", "timestamp", "platform", "author_type", "text", "current_reach"]
# Mention counts are optional: when missing or invalid the case is held for human review (Data_Dictionary: velocity edge cases).


@dataclass
class IntakeResult:
    ok: bool
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def _blank(v):
    return v is None or (isinstance(v, float) and v != v) or str(v).strip() == ""


def _int(v):
    try:
        if v is None or (isinstance(v, float) and v != v) or str(v).strip() == "":
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


def validate(raw: dict) -> IntakeResult:
    errs, warns = [], []
    for k in REQUIRED:
        v = raw.get(k)
        if v is None or (isinstance(v, float) and v != v) or str(v).strip() == "":
            errs.append(f"missing required field: {k}")
    if errs:
        return IntakeResult(False, errs, warns)
    if str(raw["current_reach"]) not in REACH_TIERS:
        errs.append(f"current_reach must be one of {REACH_TIERS}")
    ir = raw.get("initial_reach")
    if ir not in (None, "") and str(ir) not in REACH_TIERS:
        errs.append(f"initial_reach must be one of {REACH_TIERS}")
    for k in ("mentions_1h", "mentions_6h"):
        if not _blank(raw.get(k)) and _int(raw.get(k)) is None:
            errs.append(f"{k} must be a whole number")
    try:
        datetime.fromisoformat(str(raw["timestamp"]).replace("Z", ""))
    except ValueError:
        errs.append("timestamp is not ISO-8601")
    text = str(raw["text"]).strip()
    if len(text) < 3:
        errs.append("text too short")
    if len(text) > 5000:
        errs.append("text exceeds 5000 characters")
    if str(raw["platform"]) not in KNOWN_PLATFORMS:
        warns.append(f"unrecognised platform '{raw['platform']}' accepted for human review of source")
    m1, m6 = _int(raw.get("mentions_1h")), _int(raw.get("mentions_6h"))
    if m1 is None or m6 is None:
        warns.append("mention counts missing: velocity not calculated; held for human review")
    elif m1 < 0 or m6 < 0 or m1 > m6 or m6 == 0:
        warns.append("mention counts invalid for velocity (negative, 1h>6h or 6h=0): velocity not calculated; human review")
    return IntakeResult(not errs, errs, warns)


def build_case(raw: dict) -> Case:
    c = Case(case_id=str(raw["case_id"]).strip())
    c.timestamp = str(raw["timestamp"])
    c.platform = str(raw["platform"])
    c.author_type_raw = str(raw["author_type"])
    c.author_class = author_class(c.author_type_raw)
    c.current_reach = str(raw["current_reach"])
    c.initial_reach = str(raw.get("initial_reach") or raw["current_reach"])
    c.text = str(raw["text"]).strip()
    c.likes, c.comments, c.shares = (_int(raw.get(k)) or 0 for k in ("likes", "comments", "shares"))
    c.mentions_1h, c.mentions_6h = _int(raw.get("mentions_1h")), _int(raw.get("mentions_6h"))
    p = raw.get("parent_case_id")
    c.parent_case_id = None if p is None or (isinstance(p, float) and p != p) or str(p).strip() == "" else str(p)
    return c
