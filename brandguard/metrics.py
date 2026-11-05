"""KPI computation (Section 14) from a completed run: quality, automation, efficiency (simulated) and outcome KPIs."""
from __future__ import annotations
import json
from datetime import datetime
import pandas as pd
from . import config as C
from .evaluate import evaluate
from .monitoring import audit_complete


def _pr(df, col_exp, col_got, labels):
    out = {}
    for l in labels:
        tp = ((df[col_exp] == l) & (df[col_got] == l)).sum()
        fp = ((df[col_exp] != l) & (df[col_got] == l)).sum()
        fn = ((df[col_exp] == l) & (df[col_got] != l)).sum()
        out[l] = {"precision": tp / (tp + fp) if tp + fp else None, "recall": tp / (tp + fn) if tp + fn else None, "support": int((df[col_exp] == l).sum())}
    return out


def compute(df_src: pd.DataFrame, store, orch) -> dict:
    ev = evaluate(df_src, store)["rows"]
    base = ev[~ev.is_update]
    cases = store.all_cases()
    log = store.all_log()
    sev_scored = base[base.got_sev != "WITHHELD"]
    s12 = base[base.exp_sev.isin(["S1", "S2"])]
    s12_escal = s12[(s12.got_sev.isin(["S1", "S2"])) | (s12.got_sev == "WITHHELD")]
    s12_strict = s12[s12.got_sev.isin(["S1", "S2"])]
    # claim guard: every sent text must pass the guard
    unsupported = 0
    with store.lock:
        sent = [dict(r) for r in store.db.execute("SELECT o.*, c.data FROM outbox o JOIN cases c ON c.case_id=o.case_id").fetchall()]
    for r in sent:
        c = json.loads(r["data"])
        d = c["draft"]
        g = orch.kb.guard(r["text"], r["template_id"], [d["fact_id"]] if d.get("fact_id") else [], mode="human" if d.get("author") == "human" else "agent")
        unsupported += 0 if g["passed"] else 1
    # hard-gate violations: any outbox row for a no-send gate / exception case
    viol, viol_ids = 0, []
    for l in log:
        if l["action"] == "sent_to_simulated_channel":
            d = json.loads(l["detail"])
            if d.get("gate_at_send") in C.NO_SEND_GATES or set(d.get("exceptions_at_send", [])) & {"E2", "E4", "E5", "E6", "E7"}:
                viol += 1
                viol_ids.append(l["case_id"])
    cmap = {c.case_id: c for c in cases}
    # audit completeness for every case
    comp = 0
    miss_examples = []
    for c in cases:
        ok, miss = audit_complete(c, store.timeline(c.case_id), orch.approvals.history(c.case_id))
        comp += ok
        if not ok and len(miss_examples) < 5:
            miss_examples.append((c.case_id, miss))
    # SLA adherence
    with store.lock:
        aps = [dict(r) for r in store.db.execute("SELECT * FROM approvals WHERE state IN ('APPROVED','APPROVED_WITH_EDITS','TIMED_OUT','REJECTED','ESCALATED')").fetchall()]
    sla_ok = sum(1 for a in aps if a["within_sla"] == 1)
    # S4 straight-through
    s4 = [c for c in cases if c.tier == "S4" and not c.parent_case_id]
    s4_auto = [c for c in s4 if c.gate == C.GATE_LOW_RISK and c.sent]
    # human touchpoints / stage automation
    appr = {}
    for a in aps:
        appr[a["case_id"]] = appr.get(a["case_id"], 0) + 1
    # what-if / exception handling correctness
    exc_rows = base[base.exp_ex.notna()]
    def exc_hit(r):
        code = r.exp_ex if r.exp_ex not in ("E8", "E9") else None
        base_path = {"E8": "E1", "E9": "E2"}.get(r.exp_ex, r.exp_ex)
        return base_path in r.got_ex.split(",") or (base_path in ("E1", "E2") and r.got_gate in (C.GATE_HARD_BLOCK, C.GATE_MEDIA, C.GATE_HUMAN_APPROVAL, C.GATE_S1_SUGGEST))
    exc_ok = exc_rows.apply(exc_hit, axis=1)
    wi = ev[ev.is_update]
    wi_ok = sum(1 for r in wi.itertuples() if cmap[r.case_id].status == "UPDATE_APPLIED")
    # stage-level automation coverage
    stage_auto = {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 0, 7: 1, 8: 0}   # 6 = human by design; 8 human only for S1/S2
    handled = [c for c in cases if c.status != "MERGED_DUPLICATE" and not c.parent_case_id]
    stages_total = len(handled) * 8
    human_stage = sum(1 for c in handled if True)                      # stage 6 per case
    human_stage += sum(1 for c in handled if c.tier in ("S1", "S2"))   # stage 8 approval for S1/S2
    coverage = 1 - human_stage / stages_total if stages_total else 0
    return {
        "n_cases": len(cases), "n_base": len(base), "n_updates": len(wi),
        "type_accuracy": float(base.type_ok.mean()),
        "type_pr": _pr(base, "exp_type", "got_type", C.INCIDENT_TYPES),
        "severity_accuracy_all_first_decision": float(base.sev_ok.mean()),
        "severity_accuracy_scored_only": float(sev_scored.sev_ok.mean()) if len(sev_scored) else None,
        "scoring_withheld_for_review": int((base.got_sev == "WITHHELD").sum()),
        "s1_s2_recall_strict_tier": float(len(s12_strict) / len(s12)) if len(s12) else None,
        "s1_s2_recall_escalated_to_human": float(len(s12_escal) / len(s12)) if len(s12) else None,
        "s1_s2_n": len(s12),
        "verification_accuracy": float(base.ver_ok.mean()),
        "gate_accuracy": float(base.gate_ok.mean()),
        "unsupported_statements_in_sent_text": unsupported, "texts_sent": len(sent), "hard_gate_violations": viol, "hard_gate_violation_ids": viol_ids,
        "sla_adherence": sla_ok / len(aps) if aps else None, "sla_n": len(aps),
        "s4_cases": len(s4), "s4_straight_through": len(s4_auto), "s4_straight_through_rate": len(s4_auto) / len(s4) if s4 else None,
        "automation_coverage_stage_share": coverage,
        "audit_complete": comp, "audit_total": len(cases), "audit_examples_missing": miss_examples,
        "closed_with_evidence": sum(1 for c in cases if c.final_status == "CLOSED_WITH_EVIDENCE"),
        "exception_cases": int(len(exc_rows)), "exception_handled": int(exc_ok.sum()),
        "whatif_cases": len(wi), "whatif_applied": wi_ok,
        "human_touchpoints_total": len(aps), "human_touchpoints_per_case": len(aps) / max(1, len(handled)),
        "eval_rows": ev,
    }
