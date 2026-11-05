"""Compare agent outputs with the dataset's human ground truth (labels used ONLY here, after the run)."""
from __future__ import annotations
import pandas as pd
from . import config as C

TYPE_MAP = None


def _route_ok(exp: str, agent_route: str, approver: str | None, tier: str) -> bool:
    e, a = (exp or "").lower(), (agent_route or "").lower()
    if e in ("template / monitoring",):
        return "template" in a
    if e in ("close after monitoring evidence", "re-open and re-plan"):
        return True      # lifecycle outcome, evaluated through status not route
    keys = {"pr manager + legal contact": ["pr manager", "legal"], "manual queue → pr manager": ["pr manager"],
            "human review / escalation": ["human review", "pr manager", "head", "social"], "human review": ["human review"]}
    if e in keys:
        return any(k in a for k in keys[e]) if e != "pr manager + legal contact" else ("legal" in a and "pr manager" in a) or ("legal" in a)
    return e.split(" + ")[0] in a or (approver or "").lower() == e


def evaluate(df: pd.DataFrame, store) -> dict:
    cases = {c.case_id: c for c in store.all_cases()}
    first = {}                                  # FIRST decision per case (what the agent decided on arrival)
    for l in store.all_log():
        f = first.setdefault(l["case_id"], {})
        import json as _j
        det = _j.loads(l["detail"]) if l["detail"] else {}
        if l["action"] == "severity_scored" and "tier" not in f:
            f["tier"] = det["final_tier"]; f["ver"] = None
        elif l["action"] == "scoring_withheld" and "tier" not in f:
            f["tier"] = "WITHHELD"
        elif l["action"] == "decision" and "gate" not in f:
            f.update(gate=det["gate"], route=det["route"], tpl=det["template"], approver=det["approver"])
        elif l["action"] in ("verified", "tool_failure_fallback", "verification_carried_from_parent") and "ver" not in f:
            f["ver"] = (det.get("status") or "")
        elif l["action"] == "verified" and f.get("ver") is None:
            f["ver"] = det.get("status")
    rows = []
    for r in df.itertuples():
        c = cases[r.Case_ID]
        exp_ex = r.Exception_Flag if isinstance(r.Exception_Flag, str) else None
        rows.append(dict(
            case_id=r.Case_ID, group=r.Case_Group, is_update=isinstance(r.Parent_Case_ID, str),
            exp_type=r.Incident_Type_GroundTruth, got_type=c.incident_type, type_ok=c.incident_type == r.Incident_Type_GroundTruth,
            exp_ver=r.Verification_GroundTruth.upper().replace(" / N.A.", "").replace("OPINION", "OPINION"),
            got_ver=first.get(r.Case_ID, {}).get("ver") or c.verification.get("status", ""),
            exp_sev=r.Severity_GroundTruth, got_sev=first.get(r.Case_ID, {}).get("tier", "WITHHELD"), sev_ok=first.get(r.Case_ID, {}).get("tier") == r.Severity_GroundTruth,
            exp_gate=r.Expected_Action_Gate_Final, got_gate=first.get(r.Case_ID, {}).get("gate", ""), gate_ok=first.get(r.Case_ID, {}).get("gate") == r.Expected_Action_Gate_Final,
            exp_tpl=r.Expected_Template_ID_Final, got_tpl=first.get(r.Case_ID, {}).get("tpl") or "", exp_ex=exp_ex,
            got_ex=",".join(c.exceptions), route_exp=r.Expected_Route, route_got=first.get(r.Case_ID, {}).get("route", ""), status=c.final_status,
            override=r.Override_Type if isinstance(r.Override_Type, str) else ""))
    d = pd.DataFrame(rows)
    d["ver_ok"] = d.exp_ver == d.got_ver
    return {"rows": d}
