"""Orchestrator (supervisor) - plan-act-observe loop over case state and policy rules (Section 8).
Severity, tier, approver and permitted actions come from rules; specialist stages are delegated and observed."""
from __future__ import annotations
from datetime import datetime
from . import config as C
from .approval import ApprovalService, Outbox, PolicyViolation
from .intake import validate, build_case
from .kb import KnowledgeBase
from .models import Case
from .monitoring import audit_complete, closure_eligibility, detect_event
from .response import decide, build_draft, build_case_brief, next_approver
from .severity import velocity_growth, score as rubric_score, at_least
from .store import Store
from .triage import RuleClassifier, norm_key, detect_injection, detect_coordination
from .verification import ConnectorError, SimulatedEvidenceConnector

MAX_REJECTIONS = 2


class Orchestrator:
    def __init__(self, store: Store | None = None, kb: KnowledgeBase | None = None, classifier=None, connector=None):
        self.s = store or Store()
        self.kb = kb or KnowledgeBase()
        self.classifier = classifier or RuleClassifier()
        self.connector = connector or SimulatedEvidenceConnector({})
        self.approvals = ApprovalService(self.s)
        self.outbox = Outbox(self.s, self.approvals, self.kb)

    def _log(self, case, stage, agent, action, detail=None, actor="system"):
        self.s.log(case.case_id, str(stage), agent, action, detail, actor)

    def _save(self, case):
        self.s.save(case)
        self.s.incident_upsert(case, norm_key(case.text), self.s.clock.iso())

    def _add_exc(self, case, code):
        if code not in case.exceptions:
            case.exceptions.append(code)

    def _cancel_pending(self, case, reason):
        n = 0
        for a in self.approvals.history(case.case_id):
            if a["state"] in ("PENDING", "REMINDED"):
                with self.s.lock:
                    self.s.db.execute("UPDATE approvals SET state='CANCELLED',notes=? WHERE id=?", (reason, a["id"]))
                    self.s.db.commit()
                n += 1
        if n:
            self._log(case, 6, "Orchestrator", "pending_approval_cancelled", {"reason": reason, "count": n})
            for d in case.draft_versions:
                if d.get("status") == "PENDING_APPROVAL":
                    d["status"] = "WITHDRAWN"
            if case.draft.get("status") == "PENDING_APPROVAL":
                case.draft["status"] = "WITHDRAWN"
        return n

    # ---------------------------------------------------------------- public API
    def ingest(self, raw: dict, run: bool = True, sync_clock: bool = True) -> Case:
        res = validate(raw)
        cid = str(raw.get("case_id") or "UNKNOWN")
        if not res.ok:
            case = Case(case_id=cid, text=str(raw.get("text", ""))[:200], status="INTAKE_REJECTED", stage="REJECTED", final_status="INTAKE_REJECTED")
            self.s.save(case)
            self._log(case, 1, "Intake", "intake_rejected", {"errors": res.errors})
            return case
        case = build_case(raw)
        if sync_clock:
            try:
                self.s.clock.set(max(self.s.clock.now(), datetime.fromisoformat(case.timestamp.replace("Z", ""))))
            except ValueError:
                pass
        case.stage, case.status = "VALIDATED", "NEW"
        self._log(case, 1, "Intake", "intake_accepted", {"warnings": res.warnings, "platform": case.platform, "author_class": case.author_class})
        self._save(case)
        if run:
            if case.parent_case_id:
                self._process_update(case)
            else:
                self.run(case)
        return case

    def run(self, case: Case) -> Case:
        for _ in range(40):
            action, why = self._plan(case)
            if action is None:
                break
            self._log(case, 4, "Orchestrator", "plan", {"state": case.stage, "next": action, "reason": why})
            getattr(self, "_a_" + action)(case)
            self._save(case)
        return case

    def _plan(self, case: Case):
        table = {"VALIDATED": ("triage", "new validated incident: classify and de-duplicate"),
                 "TRIAGED": ("verify", "classified: retrieve approved evidence"),
                 "VERIFIED": ("score", "evidence retrieved: apply fixed rubric and mandatory overrides"),
                 "SCORED": ("decide", "score recorded: determine gate, approver, template, autonomy"),
                 "DECIDED": ("prepare", "decision made: prepare draft or case brief from approved content")}
        if case.stage in table:
            return table[case.stage]
        if case.stage == "PREPARED":
            return {"route_for_approval": ("request_approval", f"{case.tier} requires approval by {case.approver}"),
                    "auto_send_template": ("auto_send", "S4 low-risk pre-approved template permitted"),
                    "hand_off": ("hand_off", f"gate {case.gate}: human owner {case.approver or case.route} takes the case")}[case.plan_action]
        return None, ""

    # ---------------------------------------------------------------- stage actions
    def _a_triage(self, case):
        dup = self.s.incident_find_key(norm_key(case.text), case.case_id)
        if dup:
            case.duplicate_of, case.status, case.stage, case.final_status = dup, "MERGED_DUPLICATE", "CLOSED", "MERGED_DUPLICATE"
            self._log(case, 2, "Listening & Triage", "duplicate_merged", {"duplicate_of": dup})
            return
        cls = self.classifier.classify(case.text, case.author_class, case.platform)
        case.incident_type, case.type_confidence = cls.incident_type, cls.confidence
        case.classifier_notes, case.entities = cls.notes, cls.entities
        case.velocity_growth = velocity_growth(case.mentions_1h, case.mentions_6h)
        case.velocity_valid = case.velocity_growth is not None
        case.injection_detected = detect_injection(case.text)
        case.coordination_detected = detect_coordination(case.text, case.velocity_growth)
        if case.injection_detected:
            self._add_exc(case, "E7")
        if case.coordination_detected or case.incident_type == "Spam / Coordinated Activity":
            self._add_exc(case, "E4")
        self._log(case, 2, "Listening & Triage", "classified",
                  {"type": cls.incident_type, "confidence": cls.confidence, "cue_scores": cls.scores, "notes": cls.notes,
                   "velocity_growth_pct": case.velocity_growth, "classifier": getattr(self.classifier, "name", "?"),
                   "injection": case.injection_detected, "coordination": case.coordination_detected})
        case.stage = "TRIAGED"

    def _a_verify(self, case):
        last = None
        for attempt in range(1, C.TOOL_RETRIES + 2):
            try:
                ev = self.connector.fetch(case.case_id)
                facts = [f["id"] for f in (self.kb.fact_for_claim(case.text), self.kb.policy_fact(case.text)) if f]
                case.verification = {"status": ev.status, "evidence_ref": ev.ref, "note": ev.note,
                                     "retrieved_at": self.s.clock.iso(), "kb_refs": facts, "attempts": attempt}
                self._log(case, 3, "Severity & Verification", "verified", case.verification)
                last = None
                break
            except ConnectorError as ex:
                last = ex
                self._log(case, 3, "Severity & Verification", "tool_retry", {"attempt": attempt, "error": str(ex)})
        if last is not None:
            self._add_exc(case, "E6")
            case.verification = {"status": "UNAVAILABLE", "failed": True, "retrieved_at": self.s.clock.iso(), "attempts": C.TOOL_RETRIES + 1,
                                 "note": "connector failure after retries; case state saved; manual queue"}
            self._log(case, 3, "Severity & Verification", "tool_failure_fallback", case.verification)
        case.stage = "VERIFIED"

    def _a_score(self, case):
        case.overrides, case.scores, case.raw_score, case.base_tier, case.tier = [], {}, None, "", ""
        case.scoring_withheld = case.provisional = False
        if case.type_confidence < C.CONFIDENCE_THRESHOLD:
            self._add_exc(case, "E5")
            case.scoring_withheld = True
            if case.author_class == "MEDIA":
                case.overrides.append({"rule": "MEDIA_MIN_S2", "effect": "journalist/media: minimum S2 even though scoring is withheld; PR Manager reviews"})
            self._log(case, 3, "Severity & Verification", "scoring_withheld",
                      {"reason": f"classification confidence {case.type_confidence} < {C.CONFIDENCE_THRESHOLD}: human review before scoring",
                       "model_reading": case.incident_type, "confidence": case.type_confidence})
        elif not case.velocity_valid:
            case.scoring_withheld = True
            self._log(case, 3, "Severity & Verification", "scoring_withheld",
                      {"reason": "velocity undefined or counts invalid: no velocity score invented; human review",
                       "mentions_1h": case.mentions_1h, "mentions_6h": case.mentions_6h})
        else:
            vs = case.verification.get("status", "UNVERIFIABLE")
            if vs == "UNAVAILABLE":
                vs, case.provisional = "SUPPORTED", True
            r = rubric_score(case.incident_type, case.author_class, vs, case.current_reach, case.velocity_growth)
            case.scores, case.raw_score, case.base_tier = r["scores"], r["raw"], r["base_tier"]
            tier = r["base_tier"]
            if case.incident_type == "Safety / Legal":
                self._add_exc(case, "E2")
                case.legal_flag = True
                tier = at_least(tier, "S2")
                case.overrides.append({"rule": "SAFETY_LEGAL_MIN_S2", "effect": "tier >= S2; agent-drafted public text blocked; legal contact flagged"})
            if case.author_class == "MEDIA":
                tier = at_least(tier, "S2")
                case.overrides.append({"rule": "MEDIA_MIN_S2", "effect": "tier >= S2"})
            if vs == "UNVERIFIABLE":
                case.overrides.append({"rule": "UNVERIFIABLE_NO_FACTUAL_STATEMENT", "effect": "no factual public statement; escalate with evidence"})
                if not set(case.exceptions) & {"E2", "E4", "E5", "E6", "E7"}:
                    self._add_exc(case, "E1")
            for code, label in (("E4", "coordinated burst"), ("E7", "prompt injection")):
                if code in case.exceptions:
                    case.overrides.append({"rule": f"{code}_FORCE_HUMAN_REVIEW", "effect": f"{label}: human review/routing; numeric tier unchanged"})
            if case.provisional:
                case.overrides.append({"rule": "E6_PROVISIONAL_SCORE", "effect": "evidence unavailable: scored as if supported (never under-escalate); human confirms"})
            case.tier = tier
            self._log(case, 3, "Severity & Verification", "severity_scored",
                      {"scores": case.scores, "raw": case.raw_score, "base_tier": case.base_tier, "final_tier": tier,
                       "overrides": case.overrides, "provisional": case.provisional})
        case.stage = "SCORED"

    def _a_decide(self, case):
        d = decide(case, self.kb)
        case.gate, case.autonomy, case.route = d["gate"], d["autonomy"], d["route"]
        case.approver, case.template_id, case.plan_action = d["approver"], d["template_id"], d["plan_action"]
        case.legal_flag = case.legal_flag or d["legal_flag"]
        case.decision_reasons = list(d["reasons"])
        case.draft = {"pending_fact_id": d["fact"]["id"]} if d["fact"] else case.draft
        self._log(case, 4, "Orchestrator", "decision",
                  {"gate": case.gate, "autonomy": case.autonomy, "route": case.route, "approver": case.approver,
                   "template": case.template_id, "legal_flag": case.legal_flag, "reasons": case.decision_reasons})
        case.stage = "DECIDED"

    def _a_prepare(self, case):
        if case.plan_action in ("route_for_approval", "auto_send_template"):
            fid = (case.draft or {}).get("pending_fact_id") or (case.draft or {}).get("fact_id")
            fact = self.kb.facts.get(fid) if fid else None
            ver = len(case.draft_versions) + 1
            d = build_draft(case, self.kb, case.template_id, fact, True, ver)
            if not d["guard"]["passed"]:
                self._log(case, 5, "Response & Routing", "draft_blocked_by_claim_guard", d["guard"])
                case.plan_action, case.gate = "hand_off", C.GATE_REVIEW
                case.approver = case.approver or C.PR_MANAGER
                case.case_brief = build_case_brief(case, self.kb, "claim guard blocked agent draft")
            else:
                case.draft = d
                case.draft_versions.append(d)
                self._log(case, 5, "Response & Routing", "draft_prepared",
                          {"version": ver, "template": d["template_id"], "kb_refs": d["kb_refs"], "guard": d["guard"]})
        else:
            case.case_brief = build_case_brief(case, self.kb, "; ".join(case.decision_reasons))
            self._log(case, 5, "Response & Routing", "case_brief_prepared",
                      {"sections": list(case.case_brief), "holding_option": "holding_statement_option" in case.case_brief})
        case.stage = "PREPARED"

    def _a_request_approval(self, case):
        self.approvals.request(case, case.approver, case.draft.get("version"))
        case.stage, case.status, case.owner = "AWAITING_APPROVAL", "AWAITING_APPROVAL", case.approver

    def _a_auto_send(self, case):
        self.outbox.send(case, None, mode="auto")
        case.status = "SENT_LOW_RISK_TEMPLATE"
        self._log(case, 6, "Response & Routing", "auto_sent_logged", {"template": case.template_id, "audit": "weekly sample audit"})
        self._start_monitoring(case)

    def _a_hand_off(self, case):
        role = case.approver or C.PR_MANAGER
        self.approvals.request(case, role, None, kind="handoff")
        case.stage, case.status, case.owner = "HANDED_OFF", "AWAITING_HUMAN_ACK", role
        self._log(case, 5, "Response & Routing", "handed_off",
                  {"owner": role, "gate": case.gate, "route": case.route, "legal_flag": case.legal_flag, "exceptions": case.exceptions,
                   "case_file": "complete (brief, evidence, scores, overrides, timeline)"})

    # ---------------------------------------------------------------- monitoring & closure
    def _start_monitoring(self, case):
        case.monitoring = {"started": self.s.clock.iso(), "window_hours": C.MONITOR_WINDOW_HOURS.get(case.tier or "S3", 12),
                           "baseline": {"growth": case.velocity_growth, "reach": case.current_reach, "tier": case.tier}, "observations": []}
        if case.stage != "HANDED_OFF":
            case.stage = "MONITORING"
            case.status = case.status if case.status.startswith("SENT") else "MONITORING"
        self._log(case, 7, "Monitoring & Resolution", "monitoring_started", case.monitoring)

    def observe(self, case_id: str, mentions_1h: int, mentions_6h: int, reach: str, note: str = "") -> dict:
        case = self.s.get(case_id)
        g = velocity_growth(mentions_1h, mentions_6h)
        if g is None:
            self._log(case, 7, "Monitoring & Resolution", "observation_invalid", {"m1": mentions_1h, "m6": mentions_6h})
            return {"replanned": False, "reason": "invalid counts"}
        vs = case.verification.get("status", "UNVERIFIABLE")
        vs = "SUPPORTED" if vs == "UNAVAILABLE" else vs
        t = rubric_score(case.incident_type, case.author_class, vs, reach, g)["base_tier"]
        if case.incident_type == "Safety / Legal" or case.author_class == "MEDIA":
            t = at_least(t, "S2")
        obs = {"at": self.s.clock.iso(), "growth": round(g, 1), "reach": reach, "tier": t, "note": note}
        case.monitoring.setdefault("observations", []).append(obs)
        worse = bool(case.tier) and C.TIER_ORDER.index(t) < C.TIER_ORDER.index(case.tier)
        alert = g >= C.VELOCITY_ALERT_PCT and (case.velocity_growth or 0) < C.VELOCITY_ALERT_PCT
        out = {"replanned": False, "new_tier": t, "old_tier": case.tier}
        if worse or alert:
            self._log(case, 7, "Monitoring & Resolution", "monitoring_trigger", {"tier_rise": worse, "velocity_alert": alert, **obs})
            self._reevaluate(case, reach, mentions_1h, mentions_6h, "monitoring: " + ("tier rise" if worse else "velocity alert"))
            out["replanned"] = True
        else:
            self._log(case, 7, "Monitoring & Resolution", "monitoring_observation", obs)
        self._save(case)
        return out

    def try_close(self, case_id: str, resolution: bool = False) -> str:
        case = self.s.get(case_id)
        if case.stage not in ("MONITORING", "HANDED_OFF") or not case.monitoring:
            return "not_monitoring"
        if case.plan_action == "hand_off" and not resolution:
            return "human_owned"
        obs = case.monitoring.get("observations", [])
        ok = not obs or C.TIER_ORDER.index(obs[-1]["tier"]) >= C.TIER_ORDER.index(case.tier or "S4")
        ev = case.closure.get("resolution_evidence") if resolution else None
        elig, met, failed = closure_eligibility(case, self.s.clock.now(), ok, self.s.timeline(case.case_id), self.approvals.history(case.case_id), ev)
        if not elig:
            self._log(case, 8, "Monitoring & Resolution", "closure_deferred", {"failed": failed})
            self._save(case)
            return "deferred"
        if case.tier in ("S1", "S2"):
            hist = self.approvals.history(case.case_id)
            if not any(a["kind"] == "closure" and a["state"] in ("APPROVED", "APPROVED_WITH_EDITS") for a in hist):
                if not any(a["kind"] == "closure" and a["state"] in ("PENDING", "REMINDED") for a in hist):
                    self.approvals.request(case, C.ESCALATION_MATRIX[case.tier], None, kind="closure")
                    case.status = "AWAITING_CLOSURE_APPROVAL"
                    self._log(case, 8, "Monitoring & Resolution", "closure_approval_requested", {"met": met})
                self._save(case)
                return "awaiting_closure_approval"
        self._close(case, met)
        return "closed"

    def _close(self, case, met, approver="system"):
        ok, miss = audit_complete(case, self.s.timeline(case.case_id), self.approvals.history(case.case_id))
        case.closure = {**case.closure, "closed_at": self.s.clock.iso(), "criteria_met": met, "approver": approver,
                        "audit_complete": ok, "audit_missing": miss}
        case.stage, case.status, case.final_status = "CLOSED", "CLOSED_WITH_EVIDENCE", "CLOSED_WITH_EVIDENCE"
        self.s.incident_status(case.case_id, "CLOSED", case.tier)
        self._log(case, 8, "Monitoring & Resolution", "closure_recorded", case.closure, actor="system" if approver == "system" else approver)
        self._save(case)

    def human_close(self, case_id: str, actor: str, evidence: str) -> Case:
        case = self.s.get(case_id)
        case.closure["human_evidence"] = evidence
        self._close(case, ["human owner confirmed resolution", "evidence recorded"], approver=actor)
        return self.s.get(case_id)

    # ---------------------------------------------------------------- re-planning
    def _reevaluate(self, case, reach, m1, m6, reason, verification=None, reopen=False):
        old = {"tier": case.tier, "approver": case.approver, "gate": case.gate, "template": case.template_id}
        was_sent = case.sent
        case.current_reach, case.mentions_1h, case.mentions_6h = reach, m1, m6
        case.velocity_growth = velocity_growth(m1, m6)
        case.velocity_valid = case.velocity_growth is not None
        if verification:
            case.verification = verification
        case.exceptions = [e for e in case.exceptions if e not in ("E1", "E2", "E5")]
        self._cancel_pending(case, f"re-plan: {reason}")
        self._a_score(case)
        self._a_decide(case)
        self._a_prepare(case)
        new = {"tier": case.tier, "approver": case.approver, "gate": case.gate, "template": case.template_id}
        self._log(case, 4, "Orchestrator", "replan", {"reason": reason, "old": old, "new": new, "pending_actions_cancelled": True})
        if reopen:
            case.status, case.final_status = "REOPENED", ""
        if was_sent and not reopen and case.plan_action == "route_for_approval":
            role = case.approver or C.PR_MANAGER
            self.approvals.request(case, role, None, kind="handoff")
            case.stage, case.status, case.owner = "HANDED_OFF", "ESCALATED_POST_ACTION", role
            self._log(case, 7, "Monitoring & Resolution", "post_action_escalation", {"to": role, "note": "reply already sent; no new public text"})
        elif case.plan_action == "route_for_approval":
            self._a_request_approval(case)
        elif case.plan_action == "auto_send_template" and not was_sent:
            self._a_auto_send(case)
        elif case.plan_action == "auto_send_template":
            self._log(case, 7, "Monitoring & Resolution", "no_new_action", {"note": "low-risk reply already sent"})
        else:
            self._a_hand_off(case)
        self._save(case)

    def _reject_current(self, case, notes, actor_role, approval_id=None):
        rejected = [v for v in case.draft_versions if v.get("status") == "REJECTED"]
        for v in case.draft_versions:
            if v.get("status") == "PENDING_APPROVAL":
                v["status"], v["rejection_reason"] = "REJECTED", notes
        if len(rejected) + 1 >= MAX_REJECTIONS + 1 or not case.draft_versions:
            case.plan_action, case.gate = "hand_off", C.GATE_REVIEW
            self._log(case, 5, "Response & Routing", "redraft_limit_reached", {"rejections": len(rejected) + 1})
            self._a_hand_off(case)
            return
        low = (notes or "").lower()
        tid = "T-VER-01" if ("verification-only" in low or "verification only" in low) else case.draft.get("template_id") or case.template_id
        fact = self.kb.facts.get(case.draft.get("fact_id")) if case.draft.get("fact_id") else None
        d = build_draft(case, self.kb, tid, fact, include_optional=False, version=len(case.draft_versions) + 1)
        if d["text"] == case.draft.get("text") and tid != "T-VER-01":
            d = build_draft(case, self.kb, "T-VER-01", None, include_optional=False, version=len(case.draft_versions) + 1)
        if not d["guard"]["passed"]:
            case.plan_action, case.gate = "hand_off", C.GATE_REVIEW
            self._a_hand_off(case)
            return
        case.draft = d
        case.draft_versions.append(d)
        case.template_id = d["template_id"]
        self._log(case, 5, "Response & Routing", "replan_redraft",
                  {"new_version": d["version"], "template": d["template_id"], "reason": notes, "previous_versions_preserved": len(case.draft_versions) - 1})
        role = actor_role or case.approver
        self.approvals.request(case, role, d["version"])
        case.stage, case.status, case.approver, case.owner = "AWAITING_APPROVAL", "AWAITING_APPROVAL", role, role

    # ---------------------------------------------------------------- human decisions
    def human_decide(self, case_id: str, actor: str, action: str, notes: str = "", edited_text: str | None = None) -> Case:
        case = self.s.get(case_id)
        aid = case.approval.get("id")
        ap = self.approvals.get(aid) if aid else None
        if not ap:
            raise PolicyViolation("no approval request is open for this case")
        kind = ap["kind"]
        if kind == "response" and action == "edit":
            if not edited_text:
                raise ValueError("edit requires edited_text")
            g = self.kb.guard(edited_text, None, [], mode="human")
            if g["prohibited"]:
                self._log(case, 6, "Human approval", "edit_blocked_standing_prohibition", g, actor=ap["role"])
                raise PolicyViolation(f"edited text violates standing prohibitions: {g['prohibited']}")
        self.approvals.decide(aid, actor, action, notes, edited_text)
        role = ap["role"]
        if action == "escalate" or (kind == "handoff" and action == "reject"):
            nxt = next_approver(role)
            if nxt is None:
                self._log(case, 6, "Orchestrator", "escalation_exhausted", {"note": "top of ladder"})
                case.status = "ESCALATION_EXHAUSTED"
            else:
                self.approvals.request(case, nxt, ap["draft_version"], kind=kind)
                case.approver, case.route, case.owner = nxt, nxt, nxt
                self._log(case, 6, "Orchestrator", "escalated_to_next_approver", {"from": role, "to": nxt})
        elif kind == "response":
            if action == "reject":
                self._reject_current(case, notes, role, aid)
            else:
                if action == "edit":
                    v = {"version": len(case.draft_versions) + 1, "template_id": case.draft.get("template_id"), "text": edited_text,
                         "kb_refs": case.draft.get("kb_refs"), "fact_id": None, "guard": self.kb.guard(edited_text, None, [], mode="human"),
                         "author": "human", "status": "APPROVED_WITH_EDITS"}
                    case.draft_versions.append(v)
                    case.draft = v
                else:
                    case.draft["status"] = "APPROVED"
                    for v in case.draft_versions:
                        if v.get("version") == case.draft.get("version"):
                            v["status"] = "APPROVED"
                try:
                    self.outbox.send(case, aid)
                except PolicyViolation as ex:               # roll the approval back: nothing was sent, the approver can retry
                    with self.s.lock:
                        self.s.db.execute("UPDATE approvals SET state='PENDING',decided_sim=NULL,action=NULL,actor=NULL,within_sla=NULL WHERE id=?", (aid,))
                        self.s.db.commit()
                    self._log(case, 6, "Orchestrator", "send_blocked_approval_rolled_back", {"reason": str(ex), "approval_id": aid})
                    self._save(case)
                    raise
                case.status = "SENT_MONITORING"
                self._start_monitoring(case)
        elif kind == "handoff":
            case.status, case.owner = "HANDED_TO_HUMAN_OWNER", actor
            self._log(case, 5, "Human approval", "handoff_acknowledged", {"owner": actor, "within_sla": bool(self.approvals.get(aid)["within_sla"])}, actor=role)
            self._start_monitoring(case)
        elif kind == "closure":
            if action == "approve":
                self._close(case, ["closure approved by " + role], approver=role)
            else:
                case.status = "MONITORING"
                self._log(case, 8, "Human approval", "closure_declined", {"notes": notes}, actor=role)
        self._save(case)
        return self.s.get(case_id)

    def check_timeouts(self, case_id: str) -> dict | None:
        case = self.s.get(case_id)
        ev = self.approvals.check_timeouts(case)
        if ev:
            self._save(case)
        return ev

    def tick(self) -> list[dict]:
        out = []
        for ap in self.approvals.pending():
            ev = self.check_timeouts(ap["case_id"])
            if ev:
                out.append({"case_id": ap["case_id"], **ev})
        return out

    # ---------------------------------------------------------------- what-if updates
    def _process_update(self, child: Case):
        parent = self.s.get(child.parent_case_id)
        if parent is None:
            child.status, child.stage = "UPDATE_ORPHAN", "HANDED_OFF"
            self._log(child, 7, "Monitoring & Resolution", "update_orphan", {"parent": child.parent_case_id})
            self._save(child)
            return
        event = detect_event(child.text)
        self._log(child, 4, "Orchestrator", "plan", {"state": "UPDATE_RECEIVED", "next": "re-plan parent " + parent.case_id,
                                                      "reason": f"state update of type '{event}' linked to parent; revise plan and cancel inappropriate pending actions"})
        child.events.append({"type": event, "parent": parent.case_id})
        self._log(child, 7, "Monitoring & Resolution", "update_received", {"parent": parent.case_id, "event": event})
        child.incident_type, child.type_confidence = parent.incident_type, parent.type_confidence
        child.entities, child.classifier_notes = parent.entities, [f"taxonomy inherited from parent {parent.case_id}"]
        child.velocity_growth = velocity_growth(child.mentions_1h, child.mentions_6h)
        child.velocity_valid = child.velocity_growth is not None
        child.exceptions = [e for e in parent.exceptions if e == "E7"]
        if event == "new_evidence":
            self._a_verify(child)
        else:
            child.verification = {**parent.verification, "carried_from": parent.case_id}
            self._log(child, 3, "Severity & Verification", "verification_carried_from_parent", child.verification)
            child.stage = "VERIFIED"
        self._a_score(child)
        self._a_decide(child)
        child.stage = "UPDATE_DECIDED"
        self._apply_event(parent, child, event)
        child.status = "UPDATE_APPLIED"
        child.final_status = f"UPDATE_APPLIED -> {self.s.get(parent.case_id).status}"
        self._log(child, 7, "Monitoring & Resolution", "update_applied", {"parent": parent.case_id, "event": event, "parent_status": self.s.get(parent.case_id).status})
        self._save(child)

    def _apply_event(self, parent: Case, child: Case, event: str):
        parent = self.s.get(parent.case_id)
        parent.events.append({"type": event, "from": child.case_id, "at": self.s.clock.iso()})
        self._log(parent, 7, "Monitoring & Resolution", "event_received", {"event": event, "from": child.case_id})
        if event == "virality":
            child.plan_action = "rescore_and_escalate"
            self._reevaluate(parent, child.current_reach, child.mentions_1h, child.mentions_6h, f"virality update {child.case_id}")
        elif event == "new_evidence":
            child.plan_action = "reverify_and_rescore"
            self._reevaluate(parent, parent.current_reach, parent.mentions_1h, parent.mentions_6h,
                             f"new evidence {child.case_id}: {parent.verification.get('status')} -> {child.verification.get('status')}",
                             verification=child.verification)
        elif event == "approval_feedback":
            child.plan_action = "redraft_and_resubmit"
            ap = self.approvals.get(parent.approval.get("id")) if parent.approval.get("id") else None
            if ap and ap["state"] in ("PENDING", "REMINDED") and ap["kind"] == "response":
                self.approvals.decide(ap["id"], ap["role"], "reject", child.text)
                self._reject_current(parent, child.text, ap["role"], ap["id"])
            else:
                self._log(parent, 5, "Response & Routing", "approver_feedback_recorded", {"note": "no pending draft; feedback stored for the human owner", "text": child.text})
            self._save(parent)
        elif event == "public_resolution":
            child.plan_action = "continue_monitoring_then_close"
            self._cancel_pending(parent, "customer resolved publicly: pending reply no longer required")
            parent = self.s.get(parent.case_id)
            parent.closure["resolution_evidence"] = {"from": child.case_id, "reach": child.current_reach, "excerpt": child.text[:200], "at": self.s.clock.iso()}
            if not parent.monitoring:
                self._start_monitoring(parent)
                if parent.stage != "HANDED_OFF":
                    parent.stage = "MONITORING"
            self._save(parent)
            self.observe(parent.case_id, child.mentions_1h or 0, max(child.mentions_6h or 1, child.mentions_1h or 1), child.current_reach, "resolution update")
            res = self.try_close(parent.case_id, resolution=True)
            if res in ("closed", "awaiting_closure_approval"):
                child.plan_action = "close_with_evidence"
            if res == "human_owned":
                self._log(parent, 8, "Monitoring & Resolution", "owner_notified_of_resolution", {"owner": parent.owner})
        elif event == "reappearance":
            child.plan_action = "reopen_and_replan"
            self._log(parent, 7, "Monitoring & Resolution", "incident_log_lookup", {"prior": self.s.incident_row(parent.case_id)})
            was_closed = parent.stage == "CLOSED"
            self._reevaluate(parent, child.current_reach, child.mentions_1h, child.mentions_6h, f"reappearance {child.case_id}", reopen=True)
            self.s.incident_status(parent.case_id, "REOPENED", self.s.get(parent.case_id).tier)
            self._log(parent, 7, "Monitoring & Resolution", "case_reopened", {"was_closed": was_closed, "linked_update": child.case_id})
        else:
            child.plan_action = "human_review"
            self._log(parent, 7, "Monitoring & Resolution", "unclassified_update_to_human", {"text": child.text[:200]})

    def pause(self, on: bool = True):
        self.s.set_flag("paused", "1" if on else "0")
