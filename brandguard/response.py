"""Stage 4/5 - Orchestrator decision rules and Response & Routing helpers (rules, not model judgement)."""
from __future__ import annotations
import re
from . import config as C
from .kb import KnowledgeBase, HOLDING_STATEMENT
from .models import Case


def next_approver(role: str | None) -> str | None:
    if role in C.APPROVER_LADDER:
        i = C.APPROVER_LADDER.index(role)
        return C.APPROVER_LADDER[i + 1] if i + 1 < len(C.APPROVER_LADDER) else None
    return None


def decide(case: Case, kb: KnowledgeBase) -> dict:
    ex = set(case.exceptions)
    tier, vs, it = case.tier, case.verification.get("status", ""), case.incident_type
    reasons: list[str] = []
    d = dict(gate="", autonomy="", route="", approver=None, legal_flag=False, template_id=None, fact=None,
             plan_action="route_for_approval", reasons=reasons)

    def human_only(gate, route, why, approver=None):
        d.update(gate=gate, autonomy="Human only (no public text)", route=route, approver=approver, plan_action="hand_off")
        reasons.append(why)
        return d

    if "E5" in ex:
        media = case.author_class == "MEDIA"      # journalist/media minimum is S2 even when scoring is withheld
        return human_only(C.GATE_REVIEW, "Human Review", "E5 low confidence: scoring withheld, human review with model reading"
                          + ("; media source: PR Manager minimum" if media else ""), C.PR_MANAGER if media else C.SOCIAL_MGR)
    if "E6" in ex:
        return human_only(C.GATE_REVIEW, "Manual Queue → PR Manager", "E6 tool failure: manual queue with case state saved", C.PR_MANAGER)
    if case.scoring_withheld:
        return human_only(C.GATE_REVIEW, "Human Review", "velocity undefined / invalid counts: human review, raw counts retained", C.SOCIAL_MGR)
    tier_role = C.ESCALATION_MATRIX[tier]
    if "E2" in ex:
        d["legal_flag"] = True
        role = tier_role or C.PR_MANAGER
        return human_only(C.GATE_HARD_BLOCK, f"{role} + Legal Contact", "E2 safety/legal: agent drafting blocked, legal flagged", role)
    if "E7" in ex:
        return human_only(C.GATE_REVIEW, "Human Review / Escalation", "E7 prompt injection: instructions ignored, case flagged and escalated", tier_role or C.SOCIAL_MGR)
    if "E4" in ex:
        return human_only(C.GATE_REVIEW, tier_role or C.PR_MANAGER, "E4 coordinated burst: cluster, preserve evidence, escalate (no individual replies)", tier_role or C.PR_MANAGER)
    if case.author_class == "MEDIA" and vs == "UNVERIFIABLE":
        human_only(C.GATE_MEDIA, f"{tier_role or C.PR_MANAGER} + Legal Contact", "media source + unverifiable claim: brief and escalate", tier_role or C.PR_MANAGER)
        d["legal_flag"] = True
        return d
    if tier == "S1":
        d.update(gate=C.GATE_S1_SUGGEST, autonomy="Suggest", route=C.HEAD_COMMS, approver=C.HEAD_COMMS, plan_action="hand_off")
        reasons.append("S1: suggest only - case brief + evidence + holding-statement option; human decides and acts")
        return d
    if "E1" in ex:
        d["template_id"] = "T-E1-01"
        reasons.append("E1 unverifiable claim: no factual reply; request evidence via T-E1-01 and escalate to PR Manager")
        d.update(gate=C.GATE_HUMAN_APPROVAL, autonomy="Approve", route=C.PR_MANAGER, approver=C.PR_MANAGER)
        return d
    fact = None
    if vs == "CONTRADICTED":
        fact = kb.fact_for_claim(case.text)
        if it == "Misinformation" and fact:
            d["template_id"] = "T-MIS-01"
        elif it == "Misinformation":
            return human_only(C.GATE_REVIEW, tier_role or C.PR_MANAGER,
                              "claim contradicted but no approved KB fact retrieved: cannot draft a factual reply; escalate", tier_role or C.PR_MANAGER)
        else:
            d["template_id"] = "T-MIS-02"
        reasons.append("claim contradicted by approved facts: bounded correction only")
    elif case.author_class == "MEDIA":
        d["template_id"] = "T-MEDIA-01"; reasons.append("media source (opinion): media acknowledgement; PR Manager approval")
    elif it == "Product Quality":
        d["template_id"] = "T-PQ-01"
    elif it == "Refund / Return":
        d["template_id"] = "T-S3-02"
    elif it == "Service Failure":
        d["template_id"] = "T-PROMO-01" if re.search(r"offer code|discount code|promo|checkout|offer", case.text, re.I) else "T-S3-01"
    elif it == "Influencer Criticism":
        d["template_id"] = "T-S1-01"
    elif it == "General Negative Opinion":
        d["template_id"] = "T-S4-01"
    elif it == "Misinformation":
        fact = kb.fact_for_claim(case.text)
        d["template_id"] = "T-MIS-01" if fact else "T-E1-01"
    else:
        d["template_id"] = "T-S3-01"
    d["fact"] = fact
    tpl = kb.template(d["template_id"])
    auto = (tier == "S4" and tpl["auto_send"] and vs in ("OPINION", "SUPPORTED") and not ex and case.author_class != "MEDIA")
    if auto:
        d.update(gate=C.GATE_LOW_RISK, autonomy="Act then report", route="Template / Monitoring", approver=None, plan_action="auto_send_template")
        reasons.append("S4 low-risk pre-approved template: act then report, weekly sample audit")
    else:
        role = tier_role or C.SOCIAL_MGR
        if case.author_class == "MEDIA" and tier not in ("S1", "S2"):
            role = C.PR_MANAGER
        d.update(gate=C.GATE_HUMAN_APPROVAL, autonomy="Approve", route=role, approver=role)
        reasons.append(f"{tier}: human approval by {role} before any public text")
    return d


def build_draft(case: Case, kb: KnowledgeBase, tid: str, fact: dict | None, include_optional: bool = True, version: int = 1) -> dict:
    r = kb.render(tid, fact, include_optional)
    g = kb.guard(r["text"], tid, [fact["id"]] if fact else [])
    return {"version": version, "template_id": tid, "text": r["text"], "kb_refs": r["kb_refs"], "fact_id": fact["id"] if fact else None,
            "guard": g, "author": "agent", "status": "PENDING_APPROVAL"}


def build_case_brief(case: Case, kb: KnowledgeBase, why: str) -> dict:
    hits = [f["id"] for f in kb.search(case.text, 2)]
    brief = {"case_id": case.case_id, "tier": case.tier or "WITHHELD", "incident_type": case.incident_type,
             "classification_confidence": case.type_confidence, "verification": case.verification, "scores": case.scores,
             "raw_score": case.raw_score, "overrides": case.overrides, "exceptions": case.exceptions, "route": case.route,
             "legal_flag": case.legal_flag, "reason": why, "text_excerpt": case.text[:400], "related_kb_facts": hits}
    if case.gate == C.GATE_S1_SUGGEST:
        brief["holding_statement_option"] = HOLDING_STATEMENT
        brief["note"] = "Option for the Head of Communications only. The platform will not send it."
    return brief
