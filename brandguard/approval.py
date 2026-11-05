"""Stage 6 - Human approval, SLA clock, reminder and next-approver escalation (E3), and the guarded Outbox."""
from __future__ import annotations
from datetime import datetime, timedelta
from . import config as C
from .models import Case
from .response import next_approver
from .store import Store


class PolicyViolation(Exception):
    pass


class ApprovalService:
    def __init__(self, store: Store):
        self.s = store

    def _iso(self, dt: datetime) -> str:
        return dt.isoformat(timespec="seconds")

    def request(self, case: Case, role: str, draft_version: int | None, kind: str = "response") -> int:
        now = self.s.clock.now()
        sla = C.ROLE_SLA_MINUTES.get(role, C.SLA_MINUTES.get(case.tier or "S3", 240))
        if kind == "response" and case.tier in C.SLA_MINUTES and role == C.ESCALATION_MATRIX.get(case.tier):
            sla = C.SLA_MINUTES[case.tier]
        due = now + timedelta(minutes=sla)
        with self.s.lock:
            cur = self.s.db.execute("INSERT INTO approvals(case_id,role,state,requested_sim,due_sim,draft_version,kind,sla_minutes) VALUES(?,?,?,?,?,?,?,?)",
                                    (case.case_id, role, "PENDING", self._iso(now), self._iso(due), draft_version, kind, sla))
            self.s.db.commit()
        aid = cur.lastrowid
        case.approval = {"id": aid, "role": role, "state": "PENDING", "due": self._iso(due), "sla_minutes": sla, "kind": kind}
        self.s.log(case.case_id, "6", "Response & Routing", "approval_requested",
                   {"approval_id": aid, "role": role, "due": self._iso(due), "sla_minutes": sla, "draft_version": draft_version, "kind": kind})
        return aid

    def get(self, aid: int) -> dict | None:
        with self.s.lock:
            r = self.s.db.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
        return dict(r) if r else None

    def pending(self, role: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM approvals WHERE state IN ('PENDING','REMINDED')", ()
        if role:
            q, a = q + " AND role=?", (role,)
        with self.s.lock:
            return [dict(r) for r in self.s.db.execute(q + " ORDER BY due_sim", a).fetchall()]

    def history(self, case_id: str) -> list[dict]:
        with self.s.lock:
            return [dict(r) for r in self.s.db.execute("SELECT * FROM approvals WHERE case_id=? ORDER BY id", (case_id,)).fetchall()]

    def decide(self, aid: int, actor: str, action: str, notes: str = "", edited_text: str | None = None) -> dict:
        if action not in ("approve", "edit", "reject", "escalate"):
            raise ValueError("action must be approve/edit/reject/escalate")
        ap = self.get(aid)
        if not ap or ap["state"] not in ("PENDING", "REMINDED"):
            raise PolicyViolation("approval is not pending")
        now = self.s.clock.now()
        within = 1 if now <= datetime.fromisoformat(ap["due_sim"]) else 0
        state = {"approve": "APPROVED", "edit": "APPROVED_WITH_EDITS", "reject": "REJECTED", "escalate": "ESCALATED"}[action]
        with self.s.lock:
            self.s.db.execute("UPDATE approvals SET state=?,decided_sim=?,action=?,actor=?,notes=?,within_sla=? WHERE id=?",
                              (state, self._iso(now), action, actor, notes, within, aid))
            self.s.db.commit()
        self.s.log(ap["case_id"], "6", "Human approval", f"approval_{action}",
                   {"approval_id": aid, "role": ap["role"], "within_sla": bool(within), "notes": notes, "edited_text": edited_text}, actor_role=ap["role"])
        return self.get(aid)

    def check_timeouts(self, case: Case) -> dict | None:
        """E3: reminder at SLA expiry, escalate to next approver after grace period. NEVER auto-sends the pending draft."""
        aid = case.approval.get("id")
        ap = self.get(aid) if aid else None
        if not ap or ap["state"] not in ("PENDING", "REMINDED"):
            return None
        now = self.s.clock.now()
        due = datetime.fromisoformat(ap["due_sim"])
        if now < due:
            return None
        grace = timedelta(minutes=ap["sla_minutes"] * C.REMINDER_GRACE_FRACTION)
        if ap["state"] == "PENDING":
            with self.s.lock:
                self.s.db.execute("UPDATE approvals SET state='REMINDED',reminders=1,reminder_sim=? WHERE id=?", (self._iso(now), aid))
                self.s.db.commit()
            self.s.log(case.case_id, "6", "Monitoring & Resolution", "approval_reminder_sent",
                       {"approval_id": aid, "role": ap["role"], "overdue_min": (now - due).total_seconds() / 60})
            if "E3" not in case.exceptions:
                case.exceptions.append("E3")
            return {"event": "reminder", "approval_id": aid}
        if now < datetime.fromisoformat(ap["reminder_sim"]) + grace:
            return None
        nxt = next_approver(ap["role"])
        with self.s.lock:
            self.s.db.execute("UPDATE approvals SET state='TIMED_OUT',decided_sim=?,action='timeout',within_sla=0 WHERE id=?", (self._iso(now), aid))
            self.s.db.commit()
        delay = (now - datetime.fromisoformat(ap["requested_sim"])).total_seconds() / 60
        if nxt is None:
            self.s.log(case.case_id, "6", "Monitoring & Resolution", "escalation_exhausted",
                       {"approval_id": aid, "note": "top of ladder unresponsive; draft NOT sent; alert raised", "delay_min": delay})
            case.status = "ESCALATION_EXHAUSTED"
            return {"event": "exhausted", "approval_id": aid}
        self.s.log(case.case_id, "6", "Monitoring & Resolution", "approver_timeout_escalated",
                   {"from": ap["role"], "to": nxt, "delay_min": round(delay, 1), "approval_id": aid, "draft_not_sent": True})
        new_id = self.request(case, nxt, ap["draft_version"], kind=ap["kind"])
        case.approver, case.route = nxt, nxt
        return {"event": "escalated", "from": ap["role"], "to": nxt, "approval_id": new_id}


class Outbox:
    """The only path for public text; enforces every functional restriction in code."""

    def __init__(self, store: Store, approvals: ApprovalService, kb):
        self.s, self.approvals, self.kb = store, approvals, kb

    def send(self, case: Case, approval_id: int | None, mode: str = "approved") -> int:
        if self.s.flag("paused") == "1":
            raise PolicyViolation("pause control is ON: all automated actions stopped")
        if case.gate in C.NO_SEND_GATES:
            raise PolicyViolation(f"gate {case.gate} forbids agent-sent public text")
        if set(case.exceptions) & {"E2", "E4", "E5", "E6", "E7"}:
            raise PolicyViolation("exception path active: public send blocked")
        d = case.draft
        if not d or not d.get("text"):
            raise PolicyViolation("no draft to send")
        g = self.kb.guard(d["text"], d.get("template_id"), [d["fact_id"]] if d.get("fact_id") else [],
                          mode="human" if d.get("author") == "human" else "agent")
        if not g["passed"]:
            raise PolicyViolation(f"claim guard failed: {g}")
        if mode == "auto":
            tpl = self.kb.template(d["template_id"])
            if not (case.tier == "S4" and tpl and tpl["auto_send"] and case.gate == C.GATE_LOW_RISK):
                raise PolicyViolation("auto-send only for S4 pre-approved low-risk templates")
        else:
            ap = self.approvals.get(approval_id) if approval_id else None
            if not ap or ap["state"] not in ("APPROVED", "APPROVED_WITH_EDITS") or ap["case_id"] != case.case_id:
                raise PolicyViolation("no recorded human approval for this case")
        now = self.s.clock.now()
        lo = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        with self.s.lock:
            n = self.s.db.execute("SELECT COUNT(*) c FROM outbox WHERE sim_time>? AND sim_time<=?", (lo, self.s.clock.iso())).fetchone()["c"]
            if n >= C.OUTBOUND_RATE_LIMIT_PER_HOUR:
                raise PolicyViolation("outbound rate limit reached")
            cur = self.s.db.execute("INSERT INTO outbox(case_id,sim_time,channel,text,approval_id,template_id,mode) VALUES(?,?,?,?,?,?,?)",
                                    (case.case_id, self.s.clock.iso(), "SIMULATED:" + case.platform, d["text"], approval_id, d.get("template_id"), mode))
            self.s.db.commit()
        case.sent = True
        self.s.log(case.case_id, "6", "Response & Routing", "sent_to_simulated_channel",
                   {"mode": mode, "approval_id": approval_id, "template": d.get("template_id"), "channel": case.platform,
                    "gate_at_send": case.gate, "tier_at_send": case.tier, "exceptions_at_send": list(case.exceptions)})
        return cur.lastrowid
