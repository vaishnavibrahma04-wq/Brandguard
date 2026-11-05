"""Stage 7/8 helpers - event detection for re-planning, audit-completeness check and closure criteria."""
from __future__ import annotations
import re
from datetime import datetime, timedelta
from . import config as C
from .models import Case

EVENT_RX = [
    ("reappearance", r"reappear|posted again|after (an earlier )?closure|same issue"),
    ("public_resolution", r"issue is resolved|publicly resolves|posts a simulated confirmation|confirmation that the replacement"),
    ("approval_feedback", r"approver (note|feedback|rejects)|human approver rejects|reject the (current )?draft"),
    ("new_evidence", r"new (simulated )?evidence|has confirmed the batch|changes the verification"),
    ("virality", r"views|shared (widely|by multiple)|velocity|mention volume|accelerat|viral|sharply"),
]


def detect_event(text: str) -> str:
    for name, rx in EVENT_RX:
        if re.search(rx, text, re.I):
            return name
    return "unclassified_update"


def audit_complete(case: Case, log: list[dict], approvals: list[dict]) -> tuple[bool, list[str]]:
    miss = []
    if not (case.timestamp and case.platform and case.author_type_raw):
        miss.append("intake fields")
    if not case.incident_type or not case.type_confidence:
        miss.append("classification")
    if not case.verification.get("status"):
        miss.append("verification result")
    if case.scoring_withheld:
        if not any(l["action"] == "scoring_withheld" for l in log):
            miss.append("withheld-scoring reason")
    elif len(case.scores) != 4 or case.raw_score is None or not case.tier or not isinstance(case.overrides, list):
        miss.append("severity scores/overrides")
    if not (case.gate and case.route and case.decision_reasons):
        miss.append("routing decision")
    acts = {l["action"] for l in log}
    if not (approvals or acts & {"sent_to_simulated_channel", "handed_off", "update_applied"}):
        miss.append("approval / hand-off record")
    if "plan" not in acts:
        miss.append("plan trace")
    if not case.final_status and not case.status:
        miss.append("final status")
    return (not miss, miss)


def closure_eligibility(case: Case, now: datetime, observation_ok: bool, log: list[dict], approvals: list[dict],
                        resolution_evidence: dict | None = None) -> tuple[bool, list[str], list[str]]:
    met, failed = [], []
    mon = case.monitoring or {}
    window_h = C.MONITOR_WINDOW_HOURS.get(case.tier or "S3", 12)
    started = datetime.fromisoformat(mon["started"]) if mon.get("started") else None
    elapsed_ok = bool(started and now >= started + timedelta(hours=window_h))
    if resolution_evidence:
        met.append("customer resolution evidence recorded")
    elif elapsed_ok:
        met.append(f"monitoring window ({window_h}h) elapsed")
    else:
        failed.append(f"monitoring window ({window_h}h) not elapsed and no resolution evidence")
    (met if observation_ok else failed).append("no tier rise / velocity alert in follow-up")
    if case.sent or case.plan_action == "hand_off":
        if case.sent or any(a["state"] in ("APPROVED", "APPROVED_WITH_EDITS") for a in approvals):
            met.append("action completed and recorded")
        else:
            failed.append("no completed approved action")
    ok, miss = audit_complete(case, log, approvals)
    (met if ok else failed).append("audit record complete" if ok else f"audit incomplete: {miss}")
    return (not failed, met, failed)
