"""Build the run-results workbook (formulas, not pasted results, so reviewers can audit every KPI)."""
from __future__ import annotations
import json
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from brandguard.evaluate import evaluate
from brandguard.metrics import compute
from brandguard import config as C

F = "Arial"
HDR = PatternFill("solid", fgColor="153B32")
INP = PatternFill("solid", fgColor="FFFF00")
THIN = Side(style="thin", color="C9D3CF")


def _hdr(ws, row, vals, widths=None):
    for i, v in enumerate(vals, 1):
        c = ws.cell(row, i, v)
        c.font, c.fill = Font(name=F, bold=True, color="FFFFFF", size=10), HDR
        c.alignment = Alignment(wrap_text=True, vertical="center")
    if widths:
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(row + 1, 2)


def build(df, orch, target) -> None:
    store = orch.s
    sub = df[df.Case_ID.isin({c.case_id for c in store.all_cases()})]
    m = compute(sub, store, orch)
    ev = m["eval_rows"]
    wb = Workbook()

    # ---------------- Case_Results ----------------
    ws = wb.active
    ws.title = "Case_Results"
    cols = ["Case_ID", "Group", "Is_Update", "Expected_Type", "Agent_Type", "Type_Match", "Expected_Verification", "Agent_Verification",
            "Verification_Match", "Expected_Tier", "Agent_Tier", "Tier_Match", "Expected_Gate", "Agent_Gate", "Gate_Match",
            "Expected_Exception", "Agent_Exceptions", "Final_Status", "Expected_Template", "Agent_Template"]
    _hdr(ws, 1, cols, [12, 10, 9, 24, 24, 9, 16, 16, 11, 10, 11, 9, 30, 30, 9, 11, 12, 34, 18, 18])
    for i, r in enumerate(ev.itertuples(), 2):
        vals = [r.case_id, r.group, "Yes" if r.is_update else "No", r.exp_type, r.got_type, f'=IF(D{i}=E{i},1,0)', r.exp_ver, r.got_ver,
                f'=IF(G{i}=H{i},1,0)', r.exp_sev, r.got_sev, f'=IF(J{i}=K{i},1,0)', r.exp_gate, r.got_gate, f'=IF(M{i}=N{i},1,0)',
                r.exp_ex or "", r.got_ex, r.status, r.exp_tpl, r.got_tpl]
        for j, v in enumerate(vals, 1):
            c = ws.cell(i, j, v)
            c.font = Font(name=F, size=9)
    last = len(ev) + 1

    # ---------------- Summary ----------------
    sm = wb.create_sheet("KPI_Summary", 0)
    sm.column_dimensions["A"].width = 58
    for col, w in zip("BCDE", (16, 14, 44, 40)):
        sm.column_dimensions[col].width = w
    sm["A1"] = "BrandGuard - prototype run results (synthetic dataset, simulated approvers)"
    sm["A1"].font = Font(name=F, bold=True, size=13)
    sm["A2"] = ("Ground-truth labels are used only here, after the run. 'Initial decision' = what the agents decided when the case first arrived, "
                "before any later what-if update re-planned it. Efficiency KPIs need the team's timed manual baseline (sheet Baseline_Inputs).")
    sm["A2"].font, sm["A2"].alignment = Font(name=F, italic=True, size=9), Alignment(wrap_text=True)
    sm.merge_cells("A2:E2"); sm.row_dimensions[2].height = 40
    _hdr(sm, 4, ["KPI", "Result", "Target", "How measured", "Note"])
    sm.freeze_panes = "A5"
    R = f"Case_Results!$C$2:$C${last}"
    def cnt(extra=""):
        return f'COUNTIFS({R},"No"{extra})'
    rows = [
        ("Base cases evaluated (non-update)", f'=COUNTIF({R},"No")', None, "Case_Results", "", "0"),
        ("Incident-type classification accuracy", f'=SUMIFS(Case_Results!$F$2:$F${last},{R},"No")/B5', "report", "Type_Match over base cases", "Development-set accuracy: classifier cues were refined on this dataset.", "0.0%"),
        ("Verification-status accuracy", f'=SUMIFS(Case_Results!$I$2:$I${last},{R},"No")/B5', "report", "Verification_Match", "Evidence comes from the simulated connector.", "0.0%"),
        ("Severity-tier accuracy, all base cases (initial decision)", f'=SUMIFS(Case_Results!$L$2:$L${last},{R},"No")/B5', ">= 85%", "Tier_Match; withheld cases count as misses", "", "0.0%"),
        ("Severity-tier accuracy, scored cases only", f'=SUMIFS(Case_Results!$L$2:$L${last},{R},"No",Case_Results!$K$2:$K${last},"<>WITHHELD")/COUNTIFS({R},"No",Case_Results!$K$2:$K${last},"<>WITHHELD")', ">= 85%", "Excludes cases the rules send to human review before scoring", "", "0.0%"),
        ("Cases withheld for human review before scoring", f'=COUNTIFS({R},"No",Case_Results!$K$2:$K${last},"WITHHELD")', "report", "Low confidence (<0.70) or invalid mention counts", "By design (E5 + undefined velocity).", "0"),
        ("Action-gate accuracy", f'=SUMIFS(Case_Results!$O$2:$O${last},{R},"No")/B5', "report", "Gate_Match", "", "0.0%"),
        ("Expected S1/S2 cases", f'=COUNTIFS({R},"No",Case_Results!$J$2:$J${last},"S1")+COUNTIFS({R},"No",Case_Results!$J$2:$J${last},"S2")', None, "Case_Results", "", "0"),
        ("S1/S2 recall, escalated to a human (tier S1/S2 or human review)", f'=(COUNTIFS({R},"No",Case_Results!$J$2:$J${last},"S1",Case_Results!$K$2:$K${last},"<>S3",Case_Results!$K$2:$K${last},"<>S4")+COUNTIFS({R},"No",Case_Results!$J$2:$J${last},"S2",Case_Results!$K$2:$K${last},"<>S3",Case_Results!$K$2:$K${last},"<>S4"))/B12', "100%", "Not under-escalated to S3/S4", "Proposal target is 100%.", "0.0%"),
        ("S1/S2 recall, exact tier S1 or S2", f'=(COUNTIFS({R},"No",Case_Results!$J$2:$J${last},"S1",Case_Results!$K$2:$K${last},"S1")+COUNTIFS({R},"No",Case_Results!$J$2:$J${last},"S1",Case_Results!$K$2:$K${last},"S2")+COUNTIFS({R},"No",Case_Results!$J$2:$J${last},"S2",Case_Results!$K$2:$K${last},"S1")+COUNTIFS({R},"No",Case_Results!$J$2:$J${last},"S2",Case_Results!$K$2:$K${last},"S2"))/B12', "100%", "Stricter reading", "", "0.0%"),
        ("Public texts sent", m["texts_sent"], None, "Outbox log (simulated channel)", "Hard-coded from the run log.", "0"),
        ("Unsupported factual statements in sent drafts", m["unsupported_statements_in_sent_text"], "0", "Claim guard re-run over every sent text", "Hard-coded from the run log.", "0"),
        ("Hard-gate violations (text sent on a no-send gate / exception)", m["hard_gate_violations"], "0", "Gate and exceptions recorded at send time", "Hard-coded from the run log.", "0"),
        ("SLA adherence (decisions inside SLA)", m["sla_adherence"], ">= 90%", "Simulated clock; E3 timeouts count as misses", "Hard-coded from the run log.", "0.0%"),
        ("S4 straight-through rate", m["s4_straight_through_rate"], ">= 70%", "S4 cases sent with no human touch", f"{m['s4_straight_through']} of {m['s4_cases']} S4 cases.", "0.0%"),
        ("Automation coverage (share of stages agent-led)", m["automation_coverage_stage_share"], ">= 60%", "Stage 6 always human; stage 8 human for S1/S2", "", "0.0%"),
        ("Human touchpoints per case", m["human_touchpoints_per_case"], "report", "Approval decisions / handled cases", "", "0.00"),
        ("Cases with complete audit trail", m["audit_complete"] / m["audit_total"], "100%", "Audit-completeness check over every case", f"{m['audit_complete']} of {m['audit_total']}.", "0.0%"),
        ("Exception cases handled by the intended path", m["exception_handled"] / m["exception_cases"], "100%", "E1-E9 behaviour check", f"{m['exception_handled']} of {m['exception_cases']}.", "0.0%"),
        ("What-if updates applied to their parent case", m["whatif_applied"] / m["whatif_cases"], "100%", "Re-plan / reopen / close performed", f"{m['whatif_applied']} of {m['whatif_cases']}.", "0.0%"),
        ("Cases closed with evidence", m["closed_with_evidence"], "report", "Final_Status", "Human-owned and awaiting cases stay open until a person acts.", "0"),
    ]
    for i, (k, v, tgt, how, note, fmt) in enumerate(rows, 5):
        sm.cell(i, 1, k); sm.cell(i, 2, v); sm.cell(i, 3, tgt or ""); sm.cell(i, 4, how); sm.cell(i, 5, note)
        sm.cell(i, 2).number_format = fmt
        for j in range(1, 6):
            sm.cell(i, j).font = Font(name=F, size=10, color=("0000FF" if j == 2 and not str(v).startswith("=") and v is not None else "000000"))
            sm.cell(i, j).alignment = Alignment(wrap_text=True, vertical="top")
    sm.cell(len(rows) + 6, 1, "Blue = value copied from the run log; black = live formula over Case_Results.").font = Font(name=F, italic=True, size=9)

    # ---------------- Confusion matrices ----------------
    cm = wb.create_sheet("Confusion_Tier")
    tiers = ["S1", "S2", "S3", "S4", "WITHHELD"]
    cm["A1"] = "Severity tier: rows = expected (ground truth), columns = agent initial decision (base cases)"
    cm["A1"].font = Font(name=F, bold=True)
    for j, t in enumerate(tiers, 2):
        cm.cell(3, j, t).font = Font(name=F, bold=True)
    for i, t in enumerate(tiers[:4], 4):
        cm.cell(i, 1, t).font = Font(name=F, bold=True)
        for j, g in enumerate(tiers, 2):
            cm.cell(i, j, f'=COUNTIFS(Case_Results!$C$2:$C${last},"No",Case_Results!$J$2:$J${last},$A{i},Case_Results!$K$2:$K${last},{get_column_letter(j)}$3)').font = Font(name=F)
    cm2 = wb.create_sheet("Confusion_Type")
    types = C.INCIDENT_TYPES
    cm2["A1"] = "Incident type: rows = expected, columns = agent (base cases)"
    cm2["A1"].font = Font(name=F, bold=True)
    cm2.column_dimensions["A"].width = 28
    for j, t in enumerate(types, 2):
        c = cm2.cell(3, j, t); c.font = Font(name=F, bold=True, size=9); c.alignment = Alignment(wrap_text=True)
        cm2.column_dimensions[get_column_letter(j)].width = 15
    for i, t in enumerate(types, 4):
        cm2.cell(i, 1, t).font = Font(name=F, bold=True, size=9)
        for j in range(2, len(types) + 2):
            cm2.cell(i, j, f'=COUNTIFS(Case_Results!$C$2:$C${last},"No",Case_Results!$D$2:$D${last},$A{i},Case_Results!$E$2:$E${last},{get_column_letter(j)}$3)').font = Font(name=F, size=9)

    # ---------------- Baseline inputs ----------------
    bl = wb.create_sheet("Baseline_Inputs")
    bl.column_dimensions["A"].width = 48
    for col in "BCD":
        bl.column_dimensions[col].width = 22
    bl["A1"] = "Manual baseline (to be measured by the team on the same cases - Section 14)"
    bl["A1"].font = Font(name=F, bold=True, size=12)
    bl["A2"] = "Yellow cells are inputs. Leave blank until the timed manual run is done; reductions then calculate automatically."
    bl["A2"].font = Font(name=F, italic=True, size=9)
    _hdr(bl, 4, ["KPI", "Manual baseline", "Agentic run", "Reduction"])
    wall = [l for l in store.all_log()]
    first_in, first_scored = {}, {}
    for l in wall:
        if l["action"] == "intake_accepted":
            first_in.setdefault(l["case_id"], l["wall_ms"])
        if l["action"] in ("severity_scored", "scoring_withheld"):
            first_scored.setdefault(l["case_id"], l["wall_ms"])
    tri = [first_scored[k] - first_in[k] for k in first_scored if k in first_in]
    avg_s = (sum(tri) / len(tri) / 1000) if tri else None
    items = [("Time to triage per case (seconds: detection to classified and scored)", avg_s, "0.000", "Agentic value = average prototype compute time from the run log; excludes network/LLM latency of a real deployment."),
             ("Human touchpoints per case", m["human_touchpoints_per_case"], "0.00", "Manual baseline = count the people who touch each case in the timed run."),
             ("Analyst time per case (minutes)", None, "0.0", "Enter both values from the timed manual run vs. timed agent-assisted run.")]
    for i, (k, agent, fmt, note) in enumerate(items, 5):
        bl.cell(i, 1, k).font = Font(name=F, size=10)
        bl.cell(i, 2).fill = INP
        c = bl.cell(i, 3, agent); c.number_format = fmt
        if agent is None:
            c.fill = INP
        bl.cell(i, 4, f'=IF(AND(ISNUMBER(B{i}),ISNUMBER(C{i}),B{i}>0),1-C{i}/B{i},"enter baseline")').number_format = "0.0%"
        bl.cell(i, 5, note).font = Font(name=F, italic=True, size=9)
        for j in (2, 3, 4):
            bl.cell(i, j).font = Font(name=F, size=10, color="0000FF" if j < 4 else "000000")
    bl.column_dimensions["E"].width = 80

    # ---------------- Decision log ----------------
    lg = wb.create_sheet("Decision_Log")
    _hdr(lg, 1, ["Case_ID", "Sim_Time", "Stage", "Agent", "Action", "Actor_Role", "Detail"], [12, 20, 7, 24, 30, 24, 120])
    for i, l in enumerate(store.all_log(), 2):
        for j, v in enumerate([l["case_id"], l["sim_time"], l["stage"], l["agent"], l["action"], l["actor_role"], (l["detail"] or "")[:900]], 1):
            lg.cell(i, j, v).font = Font(name=F, size=9)
    lg.auto_filter.ref = f"A1:G{lg.max_row}"

    # ---------------- Approvals + Outbox ----------------
    ap = wb.create_sheet("Approvals")
    _hdr(ap, 1, ["ID", "Case_ID", "Kind", "Role", "State", "Requested", "Due", "Decided", "Within_SLA", "Actor", "Notes"], [6, 12, 10, 34, 22, 20, 20, 20, 10, 24, 40])
    for i, r in enumerate(store.db.execute("SELECT * FROM approvals ORDER BY id").fetchall(), 2):
        for j, v in enumerate([r["id"], r["case_id"], r["kind"], r["role"], r["state"], r["requested_sim"], r["due_sim"], r["decided_sim"], r["within_sla"], r["actor"], r["notes"]], 1):
            ap.cell(i, j, v).font = Font(name=F, size=9)
    ob = wb.create_sheet("Outbox_Simulated")
    _hdr(ob, 1, ["Case_ID", "Sim_Time", "Channel", "Template", "Mode", "Text"], [12, 20, 28, 12, 10, 120])
    for i, r in enumerate(store.db.execute("SELECT * FROM outbox ORDER BY id").fetchall(), 2):
        for j, v in enumerate([r["case_id"], r["sim_time"], r["channel"], r["template_id"], r["mode"], r["text"]], 1):
            ob.cell(i, j, v).font = Font(name=F, size=9)
    wb.save(target)
