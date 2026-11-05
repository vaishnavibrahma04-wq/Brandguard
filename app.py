"""BrandGuard incident desk: web console + JSON API.

Run:  python app.py        ->  http://127.0.0.1:5000
Env:  BG_DATASET=<incident workbook for the Testing area>   BG_DB=<sqlite file>   PORT=<port>
"""
from __future__ import annotations
import csv, io, os, re, threading
from collections import Counter
from datetime import datetime
import pandas as pd
from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from brandguard import config as C
from brandguard.approval import PolicyViolation
from brandguard.intake import validate
from brandguard.kb import DOCS, FACTS, HOLDING_STATEMENT, PLAYBOOK, TEMPLATES
from brandguard.metrics import compute
from brandguard.orchestrator import Orchestrator
from brandguard.response import next_approver
from brandguard.simulation import load_dataset, raw_from_row, run_dataset
from brandguard.store import Store
from brandguard.verification import SimulatedEvidenceConnector

DATASET = os.environ.get("BG_DATASET", os.path.join(os.path.dirname(os.path.abspath(__file__)), "Data", "incidents.xlsx"))
DB = os.environ.get("BG_DB", os.path.join(os.environ.get("TMPDIR", "/tmp"), "brandguard.db"))
app = Flask(__name__, static_folder="Static")
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
LOCK = threading.RLock()
STATE = {"orch": None, "df": None, "cursor": 0}
MAX_UPLOAD_ROWS = 1000

# --------------------------------------------------------------------------- plain-language vocabulary
TYPE_LABEL = {
    "Safety / Legal": "Safety or legal allegation", "Misinformation": "Misinformation about the brand",
    "Spam / Coordinated Activity": "Suspected coordinated activity", "Product Quality": "Product quality complaint",
    "Service Failure": "Delivery or service problem", "Refund / Return": "Refund or return problem",
    "Influencer Criticism": "Creator or influencer criticism", "General Negative Opinion": "General negative opinion"}
PRIORITY = {"S1": "Critical", "S2": "High", "S3": "Medium", "S4": "Low"}
VER_PLAIN = {"SUPPORTED": "records back up the complaint", "CONTRADICTED": "approved facts contradict the claim",
             "UNVERIFIABLE": "the claim can't be verified", "OPINION": "opinion only, nothing to verify",
             "UNAVAILABLE": "the records system was unavailable"}
AUTHOR_PLAIN = {"MEDIA": "a journalist or media outlet", "INFLUENTIAL": "an influential or high-reach account",
                "CUSTOMER": "an identified customer", "ANONYMOUS": "an anonymous or new account"}
FLAG_PLAIN = {"E1": "Unverified claim", "E2": "Safety or legal", "E3": "Approver overdue", "E4": "Possible coordinated activity",
              "E5": "Low confidence", "E6": "Records unavailable", "E7": "Instructions aimed at the AI"}
OVERRIDE_PLAIN = {
    "SAFETY_LEGAL_MIN_S2": "Safety and legal allegations are always treated as High priority or above, and the system never drafts public text for them.",
    "MEDIA_MIN_S2": "Posts from journalists are always treated as High priority or above.",
    "UNVERIFIABLE_NO_FACTUAL_STATEMENT": "The claim can't be verified, so no factual statement will be made in public.",
    "E4_FORCE_HUMAN_REVIEW": "Looks like coordinated activity, so a person decides the response.",
    "E7_FORCE_HUMAN_REVIEW": "The post contains instructions aimed at an AI assistant. They were ignored.",
    "E6_PROVISIONAL_SCORE": "The records system was down, so the priority assumes the complaint is valid until a person checks."}
EVENT_LABEL = {"virality": "Spreading faster", "new_evidence": "New evidence", "approval_feedback": "Approver feedback",
               "public_resolution": "Customer says it is resolved", "reappearance": "The issue came back", "unclassified_update": "Update"}


def _df():
    if STATE["df"] is None and os.path.exists(DATASET):
        STATE["df"] = load_dataset(DATASET)
    return STATE["df"]


def _table(df):
    return {r.Case_ID: (str(r.Evidence_Available), str(r.Evidence_Basis)) for r in df.itertuples()} if df is not None else {}


def new_orch(path=None):
    path = path or DB
    for ext in ("", "-wal", "-shm"):
        if path != ":memory:" and os.path.exists(path + ext):
            os.remove(path + ext)
    o = Orchestrator(Store(path), connector=SimulatedEvidenceConnector(_table(_df())))
    STATE.update(orch=o, cursor=0)
    return o


def orch() -> Orchestrator:
    if STATE["orch"] is None:
        new_orch()
    return STATE["orch"]


def err(e, code=400):
    return jsonify({"ok": False, "error": friendly(e), "kind": type(e).__name__}), code


def friendly(e) -> str:
    m = str(e)
    if "pause control" in m:
        return "Automation is paused. Resume it (top right) before sending."
    if "standing prohibitions" in m:
        found = re.findall(r"'([^']+)'", m)
        return "That wording can't be sent (" + (", ".join(found) if found else "it breaks a standing rule") + ")."
    if "forbids agent-sent" in m or "exception path" in m:
        return "Policy doesn't allow a reply to be sent on this case."
    if "claim guard" in m:
        return "The reply contains wording that isn't approved, so it was not sent."
    if "rate limit" in m:
        return "Too many replies went out in the last hour. Try again shortly."
    if "not pending" in m or "no approval request" in m:
        return "That decision has already been made."
    return m


# --------------------------------------------------------------------------- case views
def bucket(c) -> str:
    s = c.status
    if c.duplicate_of:
        return "duplicate"
    if s == "INTAKE_REJECTED":
        return "rejected"
    if s == "CLOSED_WITH_EVIDENCE" or c.stage == "CLOSED":
        return "resolved"
    if s in ("AWAITING_APPROVAL", "AWAITING_CLOSURE_APPROVAL"):
        return "decide"
    if s in ("AWAITING_HUMAN_ACK", "ESCALATED_POST_ACTION", "ESCALATION_EXHAUSTED", "UPDATE_ORPHAN", "HANDED_TO_HUMAN_OWNER"):
        return "escalated"
    return "monitoring"


def summary_line(c) -> str:
    s = c.status
    if c.duplicate_of:
        return f"Merged with {c.duplicate_of}, which was already open."
    if s == "INTAKE_REJECTED":
        return "Not accepted: required details were missing."
    if s == "CLOSED_WITH_EVIDENCE":
        cl = c.closure or {}
        if cl.get("resolution_evidence"):
            return "Resolved: the customer confirmed the issue is fixed."
        if cl.get("human_evidence"):
            return f"Resolved: {cl.get('approver', 'the owner')} closed it. {cl['human_evidence'][:80]}"
        return "Resolved: the follow-up window passed with no new activity."
    if s == "AWAITING_APPROVAL":
        return f"Reply drafted. Waiting for {c.approver} to decide."
    if s == "AWAITING_CLOSURE_APPROVAL":
        return f"Ready to close. Waiting for {c.approval.get('role', 'the approver')}."
    if s == "AWAITING_HUMAN_ACK":
        extra = " Legal Contact flagged." if c.legal_flag else ""
        return f"Escalated to {c.owner or c.approver}. No public reply drafted.{extra}"
    if s == "HANDED_TO_HUMAN_OWNER":
        return f"{c.owner} is handling this."
    if s == "ESCALATED_POST_ACTION":
        return f"Got worse after the reply. Escalated to {c.owner}."
    if s == "ESCALATION_EXHAUSTED":
        return "Nobody responded to the escalation. Needs urgent attention."
    if s == "SENT_LOW_RISK_TEMPLATE":
        return f"Low-risk reply sent automatically to {c.platform} and logged. Monitoring."
    if s.startswith("SENT"):
        return f"Reply approved and sent to {c.platform}. Monitoring for new activity."
    if s == "REOPENED":
        return "Re-opened because the issue came back."
    return "Monitoring for new activity." if s == "MONITORING" else s.replace("_", " ").capitalize() + "."


def impact(c) -> list[str]:
    out = []
    g = c.velocity_growth
    if c.mentions_1h is not None and c.mentions_6h is not None and g is not None:
        a = f"{c.current_reach} reach, {c.mentions_1h} mentions in the last hour"
        a += ", spreading fast" if g >= 100 else ", picking up" if g >= 25 else ""
    else:
        a = f"{c.current_reach} reach. Mention counts are missing, so spread can't be measured"
    out.append(a + ".")
    out.append(f"Posted by {AUTHOR_PLAIN.get(c.author_class, 'an unknown account')}.")
    ver = c.verification.get("status")
    out.append(f"{TYPE_LABEL.get(c.incident_type, c.incident_type)}" + (f": {VER_PLAIN[ver]}." if ver in VER_PLAIN else "."))
    for o in c.overrides[:2]:
        out.append(OVERRIDE_PLAIN.get(o["rule"], o["effect"]))
    return out[:5]


def recommendation(c, kind) -> tuple[str, str]:
    ex, g = set(c.exceptions), c.gate
    if kind == "closure":
        return "Close this case", "Follow-up is complete and nothing new has appeared. Closing records the final outcome and keeps the full history."
    if kind == "response":
        if "E1" in ex:
            return "Send this reply", "It asks for evidence and neither confirms nor denies the claim."
        if c.template_id == "T-MIS-01":
            return "Send this reply", "It corrects the claim using an approved fact."
        return "Send this reply", "It uses approved wording only and makes no promises."
    if "E5" in ex:
        return "Read the post and classify it", "The system wasn't confident enough to score this, so it needs your reading before anything else happens."
    if "E6" in ex:
        return "Check the records by hand", "The records system didn't respond, so nothing could be verified. The provisional priority assumes the complaint is valid."
    if c.scoring_withheld:
        return "Review the details", "Mention counts were missing or invalid, so the priority couldn't be calculated."
    if g == C.GATE_HARD_BLOCK:
        return "Take this case and write the response yourself", "The system never drafts public text for safety or legal allegations. Involve the Legal Contact before replying."
    if "E7" in ex:
        return "Review before any response", "The post contains instructions aimed at an AI assistant. They were ignored and no reply was drafted."
    if "E4" in ex:
        return "Treat as possible coordinated activity", "Keep the posts as evidence and don't reply to individual accounts."
    if g == C.GATE_MEDIA:
        return "Prepare a media response with Legal", "A journalist raised a claim that can't be verified, so no factual statement is made until it is checked."
    if g == C.GATE_S1_SUGGEST:
        return "Decide how to respond", "This is a critical case, so the system only suggests. A holding statement is offered below; it will not be posted for you."
    return "Take ownership of this case", "The system has stopped drafting. A person decides the response."


def pending_map(o) -> dict:
    out = {}
    for a in o.approvals.pending():
        out.setdefault(a["case_id"], a)
    return out


def decision_view(o, c, a) -> dict:
    mins = int((datetime.fromisoformat(a["due_sim"]) - o.s.clock.now()).total_seconds() // 60)
    kind = a["kind"]
    title, detail = recommendation(c, kind)
    d = {"approval_id": a["id"], "kind": kind, "role": a["role"], "due_in_min": mins, "overdue": mins < 0,
         "reminded": a["state"] == "REMINDED", "next_role": next_approver(a["role"]),
         "question": {"response": "Send this reply?", "handoff": "Will you take this case?", "closure": "Close this case?"}[kind],
         "rec_title": title, "rec_detail": detail}
    if kind == "response":
        g = c.draft.get("guard") or {}
        d.update(draft=c.draft.get("text", ""), draft_ok=bool(g.get("passed", True)), draft_version=c.draft.get("version"))
    if kind == "handoff" and c.gate == C.GATE_S1_SUGGEST:
        d["holding"] = (c.case_brief or {}).get("holding_statement_option")
    return d


def row(c, o, pend, kids) -> dict:
    a = pend.get(c.case_id)
    return {"case_id": c.case_id, "headline": TYPE_LABEL.get(c.incident_type, c.incident_type or "Incident"), "platform": c.platform,
            "author": AUTHOR_PLAIN.get(c.author_class, ""), "excerpt": c.text[:150], "tier": c.tier, "priority": PRIORITY.get(c.tier, ""),
            "withheld": c.scoring_withheld, "bucket": bucket(c), "summary": summary_line(c), "owner": c.owner or c.approver or "",
            "flags": [FLAG_PLAIN[e] for e in c.exceptions if e in FLAG_PLAIN], "updates": kids.get(c.case_id, 0),
            "reopened": any(e.get("type") == "reappearance" for e in c.events), "timestamp": c.timestamp,
            "decision": decision_view(o, c, a) if a else None, "impact": impact(c) if a else None}


def visible_cases(o):
    cases = o.s.all_cases()
    kids = Counter(c.parent_case_id for c in cases if c.parent_case_id)
    shown = [c for c in cases if not (c.parent_case_id and c.status == "UPDATE_APPLIED")]
    return cases, shown, kids


def counts_for(shown) -> dict:
    n = Counter(bucket(c) for c in shown)
    return {k: n.get(k, 0) for k in ("decide", "escalated", "monitoring", "resolved")} | {"all": len(shown)}


# --------------------------------------------------------------------------- plain-language history
def plain_event(l) -> tuple[str, str] | None:
    a, d, who = l["action"], l["detail"] or {}, l["actor_role"]
    P = lambda t: (t, "person")
    W = lambda t: (t, "warn")
    N = lambda t: (t, "")
    if a == "intake_accepted":
        return N(f"Incident received from {d.get('platform', 'a channel')}.")
    if a == "classified":
        t = f"Classified as {TYPE_LABEL.get(d.get('type'), d.get('type'))} ({round(d.get('confidence', 0) * 100)}% confident)."
        if d.get("injection"):
            t += " It contains instructions aimed at an AI, which were ignored."
        return N(t)
    if a == "verified":
        return N(f"Records check: {VER_PLAIN.get(d.get('status'), d.get('status'))}.")
    if a == "tool_retry":
        return W("The records system didn't respond. Trying again.")
    if a == "tool_failure_fallback":
        return W("The records system stayed down. Sent to the manual queue.")
    if a == "severity_scored":
        t = f"Priority set to {d['final_tier']} ({PRIORITY[d['final_tier']]})."
        if d.get("base_tier") != d.get("final_tier"):
            t += f" Raised from {d['base_tier']} by a mandatory rule."
        return N(t)
    if a == "scoring_withheld":
        return W("Priority held back: " + d.get("reason", "needs human review").split(":")[0] + ". A person reviews it first.")
    if a == "decision":
        return N("Plan: " + " ".join(r.rstrip(".") + "." for r in d.get("reasons", [])))
    if a == "draft_prepared":
        return N(f"Reply drafted from approved wording (version {d.get('version')}).")
    if a == "draft_blocked_by_claim_guard":
        return W("The draft was blocked because it contained wording that isn't approved.")
    if a == "case_brief_prepared":
        return N("Case summary prepared for the owner.")
    if a == "approval_requested":
        what = {"response": "decide on the reply", "handoff": "take ownership", "closure": "approve closing the case"}.get(d.get("kind"), "decide")
        return N(f"Waiting for {d.get('role')} to {what}.")
    if a == "approval_approve":
        return P(f"{who} approved.")
    if a == "approval_edit":
        return P(f"{who} edited the wording and approved.")
    if a == "approval_reject":
        return P(f"{who} rejected it." + (f" Reason: {d['notes']}" if d.get("notes") else ""))
    if a == "approval_escalate":
        return P(f"{who} passed it up.")
    if a == "sent_to_simulated_channel":
        return N("Low-risk reply sent automatically and logged." if d.get("mode") == "auto" else f"Reply sent to {d.get('channel')}.")
    if a == "handed_off":
        return N(f"Handed to {d.get('owner')}. No public reply drafted.")
    if a == "handoff_acknowledged":
        return P(f"{d.get('owner')} took ownership.")
    if a == "approval_reminder_sent":
        return W(f"Reminder sent to {d.get('role')}: decision overdue.")
    if a == "approver_timeout_escalated":
        return W(f"No decision from {d.get('from')}. Passed to {d.get('to')}. The draft was not sent.")
    if a == "escalated_to_next_approver":
        return P(f"Passed from {d.get('from')} to {d.get('to')}.")
    if a == "escalation_exhausted":
        return W("Nobody responded at the top of the escalation ladder. Nothing was sent.")
    if a == "monitoring_started":
        return N(f"Follow-up started ({d.get('window_hours')} hour window).")
    if a == "monitoring_observation":
        return N("Follow-up check: no change.")
    if a == "monitoring_trigger":
        return W("Follow-up check: the situation got worse. Re-assessing.")
    if a == "replan":
        o_, n_ = d.get("old", {}), d.get("new", {})
        return N(f"Re-assessed ({d.get('reason')}). Priority {o_.get('tier') or 'unset'} to {n_.get('tier') or 'unset'}. Earlier pending approvals were withdrawn.")
    if a == "replan_redraft":
        return N(f"New draft created after feedback (version {d.get('new_version')}). Earlier versions are kept.")
    if a == "closure_deferred":
        return N("Not ready to close: " + "; ".join(d.get("failed", [])) + ".")
    if a == "closure_approval_requested":
        return N("Ready to close. Waiting for approval.")
    if a == "closure_recorded":
        return P("Closed with evidence.") if d.get("approver") not in (None, "system") else N("Closed with evidence.")
    if a == "closure_declined":
        return P("Closure declined. Monitoring continues.")
    if a == "case_reopened":
        return W("Re-opened because the issue came back.")
    if a == "event_received":
        return N(f"Update received: {EVENT_LABEL.get(d.get('event'), d.get('event'))}.")
    if a == "pending_approval_cancelled":
        return N(f"An earlier approval request was withdrawn ({d.get('reason', 'no longer needed')}).")
    if a == "duplicate_merged":
        return N(f"Merged with {d.get('duplicate_of')}.")
    if a == "redraft_limit_reached":
        return W("The draft was rejected twice. Handed to a person.")
    if a == "send_blocked_approval_rolled_back":
        return W("Sending was blocked, so the approval was rolled back. Nothing went out.")
    if a == "edit_blocked_standing_prohibition":
        return W("An edit was blocked because it broke a standing rule.")
    if a == "post_action_escalation":
        return W("The situation got worse after the reply. Escalated to a person.")
    if a == "approver_feedback_recorded":
        return N("Approver feedback recorded for the owner.")
    if a in ("plan", "update_received", "verification_carried_from_parent", "incident_log_lookup", "update_applied",
             "owner_notified_of_resolution", "no_new_action", "observation_invalid", "pause_on", "pause_off"):
        return None
    return N(a.replace("_", " ").capitalize() + ".")


def phases(c) -> dict:
    if c.duplicate_of:
        return {"steps": [], "note": f"Merged with {c.duplicate_of}."}
    names = ["Received", "Checked", "Decision", "Follow-up", "Closed"]
    s = c.status
    if bucket(c) == "resolved":
        cur, done_all = 4, True
    elif s == "AWAITING_CLOSURE_APPROVAL":
        cur, done_all = 4, False
    elif s in ("AWAITING_APPROVAL", "AWAITING_HUMAN_ACK", "ESCALATED_POST_ACTION", "ESCALATION_EXHAUSTED"):
        cur, done_all = 2, False
    elif s == "HANDED_TO_HUMAN_OWNER" or s.startswith("SENT") or s in ("MONITORING", "REOPENED"):
        cur, done_all = 3, False
    else:
        cur, done_all = 1, False
    sub = {0: c.platform, 1: TYPE_LABEL.get(c.incident_type, ""), 2: "", 3: "", 4: ""}
    if c.tier:
        sub[1] = f"{c.tier} {PRIORITY[c.tier]}"
    sub[2] = c.approver or c.owner or ""
    sub[3] = "with " + c.owner if s == "HANDED_TO_HUMAN_OWNER" and c.owner else ""
    return {"steps": [{"name": n, "state": "done" if (i < cur or done_all) else "current" if i == cur else "todo", "sub": sub[i] if (i <= cur or done_all) else ""}
                      for i, n in enumerate(names)]}


# --------------------------------------------------------------------------- pages and state
@app.get("/")
def index():
    return send_from_directory("Static", "index.html")


@app.get("/api/state")
def state():
    with LOCK:
        o = orch()
        _, shown, _ = visible_cases(o)
        df = _df()
        return jsonify({"ok": True, "clock": o.s.clock.iso(), "paused": o.s.flag("paused") == "1", "counts": counts_for(shown),
                        "dataset_loaded": df is not None, "dataset_rows": 0 if df is None else len(df), "feed_cursor": STATE["cursor"],
                        "total_cases": len(shown),
                        "roles": [C.SOCIAL_MGR, C.PR_MANAGER, C.HEAD_COMMS, C.LEGAL]})


@app.get("/api/desk")
def desk():
    with LOCK:
        o = orch()
        _, shown, kids = visible_cases(o)
        pend = pending_map(o)
        b, role, q = request.args.get("bucket", "all"), request.args.get("role", ""), (request.args.get("q") or "").lower()
        items = []
        for c in shown:
            if b != "all" and bucket(c) != b:
                continue
            a = pend.get(c.case_id)
            if role and not (a and a["role"] == role) and role not in (c.owner, c.approver):
                continue
            if q and q not in f"{c.case_id} {c.text} {TYPE_LABEL.get(c.incident_type, '')} {c.platform}".lower():
                continue
            items.append(row(c, o, pend, kids))
        waiting = sorted([i for i in items if i["decision"]], key=lambda i: i["decision"]["due_in_min"])
        rest = [i for i in items if not i["decision"]][::-1]
        out = (waiting + rest)[:150]
        return jsonify({"ok": True, "counts": counts_for(shown), "items": out, "total": len(items), "paused": o.s.flag("paused") == "1"})


@app.get("/api/activity")
def activity():
    """Latest meaningful events across all incidents, in plain language."""
    import json as _json
    with LOCK:
        o = orch()
        rows = o.s.db.execute("SELECT case_id, sim_time, action, actor_role, detail FROM decision_log ORDER BY id DESC LIMIT 400").fetchall()
        out = []
        for r in rows:
            if r["case_id"] == "SYSTEM":
                continue
            d = dict(r)
            d["detail"] = _json.loads(d["detail"]) if d["detail"] else None
            d["actor_role"] = d["actor_role"]
            if d["action"] not in ("sent_to_simulated_channel", "approval_approve", "approval_edit", "approval_reject", "approval_escalate", "handed_off",
                                   "handoff_acknowledged", "approver_timeout_escalated", "approval_reminder_sent", "closure_recorded", "case_reopened",
                                   "monitoring_trigger", "intake_rejected", "scoring_withheld", "escalation_exhausted"):
                continue
            pe = plain_event(d)
            if pe:
                out.append({"case_id": d["case_id"], "when": d["sim_time"], "text": pe[0] if len(pe[0]) < 120 else pe[0][:117] + "...", "kind": pe[1]})
            if len(out) >= 14:
                break
        return jsonify({"ok": True, "items": out})


@app.get("/api/cases/<cid>")
def case_detail(cid):
    with LOCK:
        o = orch()
        c = o.s.get(cid)
        if not c:
            return err("Incident not found", 404)
        cases, shown, kids = visible_cases(o)
        pend = pending_map(o)
        tl = o.s.timeline(cid)
        hist = []
        for l in tl:
            pe = plain_event(l)
            if pe:
                hist.append({"when": l["sim_time"], "text": pe[0], "kind": pe[1]})
        children = [{"case_id": x.case_id, "event": EVENT_LABEL.get((x.events[0]["type"] if x.events else "unclassified_update"), "Update"),
                     "text": x.text, "when": x.timestamp} for x in cases if x.parent_case_id == cid]
        outbox = [dict(r) for r in o.s.db.execute("SELECT sim_time, channel, text, mode FROM outbox WHERE case_id=? ORDER BY id", (cid,)).fetchall()]
        r = row(c, o, pend, kids)
        sc = c.scores or {}
        factors = [{"label": n, "points": sc.get(k), "max": m} for k, n, m in
                   (("reach_velocity", "Reach and spread", 30), ("issue_risk", "Type of issue", 30), ("source_type", "Who posted it", 20), ("verification", "Records check", 20))] if sc else []
        mon = c.monitoring or {}
        owner_open = c.status == "HANDED_TO_HUMAN_OWNER"
        return jsonify({
            "ok": True, "row": r, "text": c.text, "meta": {"platform": c.platform, "author": AUTHOR_PLAIN.get(c.author_class, ""), "reach": c.current_reach,
                                                         "received": c.timestamp, "m1": c.mentions_1h, "m6": c.mentions_6h, "confidence": c.type_confidence,
                                                         "parent": c.parent_case_id},
            "impact": impact(c), "phases": phases(c), "factors": factors, "raw_score": c.raw_score, "withheld": c.scoring_withheld,
            "verification": {"text": VER_PLAIN.get(c.verification.get("status"), ""), "ref": c.verification.get("evidence_ref", "")},
            "plan": c.decision_reasons, "overrides": [OVERRIDE_PLAIN.get(x["rule"], x["effect"]) for x in c.overrides],
            "drafts": [{"version": d.get("version"), "text": d.get("text"), "status": (d.get("status") or "").replace("_", " ").lower(),
                        "reason": d.get("rejection_reason"), "by": d.get("author")} for d in c.draft_versions],
            "no_draft": c.gate == C.GATE_HARD_BLOCK, "holding": (c.case_brief or {}).get("holding_statement_option"),
            "sent": outbox, "history": hist, "children": children, "technical": tl,
            "closure": c.closure if bucket(c) == "resolved" else None,
            "follow_up": {"can_check": bool(mon.get("started")) and bucket(c) in ("monitoring",) and c.plan_action != "hand_off",
                          "window_hours": mon.get("window_hours"), "started": mon.get("started"),
                          "owner_can_close": owner_open}})


# --------------------------------------------------------------------------- actions
def evidence_label(v) -> str:
    t = str(v or "").strip().lower()
    if not t:
        return ""
    if "contradict" in t:
        return "Yes - contradicts"
    if "unavailable" in t:
        return "unavailable to the agent"
    if "support" in t:
        return "Yes - supports"
    if "not applicable" in t or "opinion" in t:
        return "Not applicable"
    if "partial" in t or "unverif" in t or "no matching" in t or "no record" in t:
        return "Partial"
    return ""


def next_id(o) -> str:
    n = o.s.db.execute("SELECT COUNT(*) c FROM cases WHERE case_id LIKE 'INC-%'").fetchone()["c"]
    while o.s.get(f"INC-{n + 1:04d}"):
        n += 1
    return f"INC-{n + 1:04d}"


def submit_one(o, raw: dict, evidence: str = "") -> tuple[bool, object]:
    """Validate, register the stand-in evidence state, ingest. Returns (ok, case-or-errors)."""
    raw = dict(raw)
    raw["case_id"] = str(raw.get("case_id") or "").strip() or next_id(o)
    if not str(raw.get("timestamp") or "").strip():
        raw["timestamp"] = o.s.clock.iso()
    for k, dflt in (("platform", "Not stated"), ("author_type", "Anonymous Account"), ("current_reach", "Low")):
        if not str(raw.get(k) or "").strip():
            raw[k] = dflt          # missing context falls back to the cautious, lowest-trust default
    raw["current_reach"] = str(raw["current_reach"]).strip().title()
    res = validate(raw)
    if not res.ok:
        o.s.log(raw["case_id"], "1", "Intake", "intake_rejected", {"errors": res.errors})
        return False, res.errors
    if o.s.get(raw["case_id"]):
        return False, [f"incident ID {raw['case_id']} already exists"]
    lab = evidence_label(evidence)
    if lab:
        o.connector.table[raw["case_id"]] = (lab, "Evidence state supplied at intake (stand-in for the records lookup)")
    return True, o.ingest(raw, sync_clock=False)


@app.post("/api/intake")
def intake():
    with LOCK:
        try:
            b = request.get_json(force=True)
            ok, res = submit_one(orch(), b, b.pop("evidence", ""))
            if not ok:
                return jsonify({"ok": False, "error": "Not accepted: " + "; ".join(res) + "."}), 400
            return jsonify({"ok": True, "case_id": res.case_id, "bucket": bucket(res), "summary": summary_line(res)})
        except Exception as e:                                       # noqa: BLE001
            return err(e)


COLS = {"text": ("text", "incidenttext", "complaint", "post", "message", "content", "description"),
        "case_id": ("caseid", "id", "ticketid", "reference", "incidentid"),
        "timestamp": ("timestamp", "date", "datetime", "createdat", "time", "received"),
        "platform": ("platform", "channel", "source"),
        "author_type": ("authortype", "author", "accounttype", "poster"),
        "current_reach": ("currentreach", "currentreachtier", "reach", "reachtier"),
        "mentions_1h": ("mentions1h", "mentionslasthour", "mentions1hour"),
        "mentions_6h": ("mentions6h", "mentionslast6hours", "mentions6hours"),
        "parent_case_id": ("parentcaseid", "parentid"),
        "evidence": ("evidence", "evidencestate", "evidenceavailable"),
        "likes": ("likes",), "comments": ("comments",), "shares": ("shares",)}


def read_upload(f) -> pd.DataFrame:
    name = (f.filename or "").lower()
    if name.endswith(".csv"):
        raw = f.read()
        for enc in ("utf-8-sig", "cp1252"):
            try:
                return pd.read_csv(io.StringIO(raw.decode(enc)), dtype=str, keep_default_na=False)
            except UnicodeDecodeError:
                continue
        raise ValueError("The file's text encoding isn't recognised. Save it as UTF-8 CSV.")
    if name.endswith((".xlsx", ".xlsm")):
        x = pd.ExcelFile(f)
        sheet = "Incident_Cases" if "Incident_Cases" in x.sheet_names else x.sheet_names[0]
        return x.parse(sheet, dtype=str).fillna("")
    raise ValueError("Upload a .csv or .xlsx file.")


@app.post("/api/upload")
def upload():
    with LOCK:
        f = request.files.get("file")
        if not f or not f.filename:
            return err("Choose a file first.")
        try:
            df = read_upload(f)
        except Exception as e:                                       # noqa: BLE001
            return err(f"Couldn't read that file. {e}")
        if len(df) > MAX_UPLOAD_ROWS:
            return err(f"That file has {len(df)} rows. Upload at most {MAX_UPLOAD_ROWS} at a time.")
        norm = {re.sub(r"[^a-z0-9]", "", str(c).lower()): c for c in df.columns}
        colmap = {k: next((norm[a] for a in alts if a in norm), None) for k, alts in COLS.items()}
        if not colmap["text"]:
            return err("No column with the complaint text was found. Name it 'text' (or use the template).")
        o = orch()
        before = {c.case_id for c in o.s.all_cases()}
        problems, skipped = [], 0
        for i, r in df.iterrows():
            raw = {k: (str(r[col]).strip() if col else "") for k, col in colmap.items()}
            if raw["case_id"].upper().startswith("EXAMPLE"):
                skipped += 1
                continue
            ev = raw.pop("evidence", "")
            ok, res = submit_one(o, raw, ev)
            if not ok:
                problems.append({"row": int(i) + 2, "id": raw["case_id"] or "", "reasons": res})
        cases, shown, _ = visible_cases(o)
        new = [c for c in shown if c.case_id not in before]
        counts = Counter(bucket(c) for c in new)
        return jsonify({"ok": True, "processed": len(new), "counts": {k: counts.get(k, 0) for k in ("decide", "escalated", "monitoring", "resolved", "duplicate")},
                        "problems": problems[:50], "problem_total": len(problems), "skipped": skipped,
                        "evidence_column": bool(colmap["evidence"]), "dataset_ids": any(c.case_id in _table(_df()) for c in new)})


@app.get("/api/template.csv")
def template():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["case_id", "timestamp", "platform", "author_type", "text", "current_reach", "mentions_1h", "mentions_6h", "evidence"])
    w.writerow(["EXAMPLE", "", "Instagram", "Identified Customer", "Delete this row. Example: my parcel has not moved for two days.", "Low", "10", "60", "supports"])
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=complaints_template.csv"})


@app.post("/api/cases/<cid>/decide")
def decide(cid):
    with LOCK:
        try:
            b = request.get_json(force=True)
            c = orch().human_decide(cid, b.get("actor", "Approver"), b["action"], b.get("notes", ""), b.get("edited_text"))
            return jsonify({"ok": True, "summary": summary_line(c), "bucket": bucket(c)})
        except PolicyViolation as e:
            return err(e, 409)
        except Exception as e:                                       # noqa: BLE001
            return err(e)


@app.post("/api/cases/<cid>/followup")
def followup(cid):
    """Record a follow-up check and, if conditions allow, close: one continuous step."""
    with LOCK:
        try:
            o, b = orch(), request.get_json(force=True)
            m1, m6 = b.get("mentions_1h"), b.get("mentions_6h")
            res = {"replanned": False}
            if str(m1).strip() != "" and str(m6).strip() != "":
                res = o.observe(cid, int(m1), int(m6), b.get("reach", "Low"), b.get("note", ""))
            if res.get("replanned"):
                return jsonify({"ok": True, "message": "Things got worse, so the case was re-assessed and needs a decision."})
            r = o.try_close(cid)
            if r in ("deferred",):
                last = [l for l in o.s.timeline(cid) if l["action"] == "closure_deferred"][-1]["detail"]["failed"]
                return jsonify({"ok": True, "message": "Recorded. Not ready to close yet: " + "; ".join(last) + "."})
            return jsonify({"ok": True, "message": {"closed": "Closed. Follow-up completed with no new activity.",
                                                    "awaiting_closure_approval": "Ready to close. It now needs approval.",
                                                    "human_owned": "A person owns this case, so only the owner can close it.",
                                                    "not_monitoring": "This case isn't in follow-up."}.get(r, r)})
        except Exception as e:                                       # noqa: BLE001
            return err(e)


@app.post("/api/cases/<cid>/resolve")
def resolve(cid):
    with LOCK:
        try:
            b = request.get_json(force=True)
            if not str(b.get("evidence", "")).strip():
                return err("Say what shows the issue is resolved.")
            orch().human_close(cid, b.get("actor", "Case owner"), b["evidence"].strip())
            return jsonify({"ok": True})
        except Exception as e:                                       # noqa: BLE001
            return err(e)


@app.post("/api/pause")
def pause():
    with LOCK:
        on = bool((request.get_json(silent=True) or {}).get("on", True))
        orch().pause(on)
        orch().s.log("SYSTEM", "0", "Control", "pause_on" if on else "pause_off", {}, "operator")
        return jsonify({"ok": True, "paused": on})


# --------------------------------------------------------------------------- testing area
@app.post("/api/clock")
def clock():
    with LOCK:
        b = request.get_json(force=True)
        o = orch()
        o.s.clock.advance(minutes=float(b.get("minutes", 0)), hours=float(b.get("hours", 0)))
        return jsonify({"ok": True, "clock": o.s.clock.iso(), "events": o.tick()})


@app.post("/api/feed")
def feed():
    with LOCK:
        df = _df()
        if df is None:
            return err("The sample dataset wasn't found. Set BG_DATASET to its path.", 404)
        o, n = orch(), min(int((request.get_json(silent=True) or {}).get("n", 5)), 200)
        rows = list(df.itertuples())[STATE["cursor"]:STATE["cursor"] + n]
        if STATE["cursor"] == 0 and not o.s.all_cases() and rows:
            o.s.clock.set(pd.Timestamp(rows[0].Timestamp).to_pydatetime())
        for r in rows:
            o.ingest(raw_from_row(r), sync_clock=False)
        STATE["cursor"] += len(rows)
        return jsonify({"ok": True, "loaded": len(rows), "cursor": STATE["cursor"], "total": len(df)})


@app.post("/api/simulate")
def simulate():
    with LOCK:
        df = _df()
        if df is None:
            return err("The sample dataset wasn't found. Set BG_DATASET to its path.", 404)
        for ext in ("", "-wal", "-shm"):
            if DB != ":memory:" and os.path.exists(DB + ext):
                os.remove(DB + ext)
        o, s = run_dataset(df, DB)
        STATE.update(orch=o, cursor=len(df))
        return jsonify({"ok": True, "cases": len(s.all_cases())})


@app.post("/api/reset")
def reset():
    with LOCK:
        new_orch()
        return jsonify({"ok": True})


@app.get("/api/kpis")
def kpis():
    with LOCK:
        df = _df()
        if df is None:
            return err("The sample dataset wasn't found.", 404)
        o = orch()
        done = {c.case_id for c in o.s.all_cases()}
        sub = df[df.Case_ID.isin(done)]
        if not len(sub):
            return jsonify({"ok": True, "empty": True})
        m = compute(sub, o.s, o)
        m.pop("eval_rows")
        return jsonify({"ok": True, "kpis": m})


@app.get("/api/kb")
def kb():
    return jsonify({"ok": True, "docs": DOCS, "facts": FACTS, "holding": HOLDING_STATEMENT, "playbook": PLAYBOOK,
                    "templates": {k: {"name": v["name"], "auto_send": v["auto_send"], "sentences": [s for s, _ in v["sentences"]]} for k, v in TEMPLATES.items()},
                    "matrix": {t: {"approver": C.ESCALATION_MATRIX[t], "sla_minutes": C.SLA_MINUTES[t], "label": PRIORITY[t]} for t in C.TIER_ORDER},
                    "rubric": {"issue_risk": C.ISSUE_RISK_POINTS, "source": C.SOURCE_POINTS, "verification": C.VERIFICATION_POINTS},
                    "confidence_threshold": C.CONFIDENCE_THRESHOLD})


@app.get("/api/export")
def export():
    with LOCK:
        import export_run
        if _df() is None:
            return err("The sample dataset wasn't found.", 404)
        buf = io.BytesIO()
        export_run.build(_df(), orch(), buf)
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name="BrandGuard_Run_Results.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)), threaded=True)
