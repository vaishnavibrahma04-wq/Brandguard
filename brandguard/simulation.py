"""Test harness: loads the 400-case dataset, simulates evidence connector, approvers and time, and drives the orchestrator.
Ground-truth labels are NEVER passed to the agents; only raw feed fields reach ingest()."""
from __future__ import annotations
import hashlib
from datetime import datetime, timedelta
import pandas as pd
from . import config as C
from .monitoring import detect_event
from .orchestrator import Orchestrator
from .store import Store
from .verification import SimulatedEvidenceConnector


def raw_from_row(r) -> dict:
    return dict(case_id=r.Case_ID, timestamp=str(r.Timestamp), platform=r.Platform, author_type=r.Author_Type,
                initial_reach=r.Initial_Reach_Tier, current_reach=r.Current_Reach_Tier, text=r.Incident_Text,
                likes=r.Likes, comments=r.Comments, shares=r.Shares, mentions_1h=r.Mentions_1h, mentions_6h=r.Mentions_6h,
                parent_case_id=r.Parent_Case_ID if isinstance(r.Parent_Case_ID, str) else None)


def _h(case_id: str) -> int:
    return int(hashlib.md5(case_id.encode()).hexdigest()[:8], 16)


class SimHumans:
    """Deterministic simulated approvers: approve normally, edit ~8%, stay silent for E3 scenarios, acknowledge hand-offs."""

    def __init__(self, orch: Orchestrator, silent: set[str]):
        self.o, self.silent, self.escalated = orch, silent, set()

    def respond(self, case_id: str, max_steps: int = 8) -> None:
        o = self.o
        for _ in range(max_steps):
            case = o.s.get(case_id)
            aid = case.approval.get("id")
            ap = o.approvals.get(aid) if aid else None
            if not ap or ap["state"] not in ("PENDING", "REMINDED"):
                return
            if case_id in self.silent and case_id not in self.escalated and ap["kind"] in ("response", "handoff"):
                due = datetime.fromisoformat(ap["due_sim"])
                o.s.clock.set(due + timedelta(minutes=1))
                o.check_timeouts(case_id)
                o.s.clock.set(due + timedelta(minutes=ap["sla_minutes"] * C.REMINDER_GRACE_FRACTION + 2))
                o.check_timeouts(case_id)
                self.escalated.add(case_id)
                continue
            requested = datetime.fromisoformat(ap["requested_sim"])
            frac = 0.10 + 0.60 * ((_h(case_id) % 100) / 100)
            o.s.clock.set(requested + timedelta(minutes=ap["sla_minutes"] * frac))
            if ap["kind"] == "response" and _h(case_id) % 13 == 0:
                tid = case.draft.get("template_id")
                fact = o.kb.facts.get(case.draft.get("fact_id")) if case.draft.get("fact_id") else None
                alt = o.kb.render(tid, fact, include_optional=False)["text"] if tid in o.kb.templates else None
                if alt and alt != case.draft.get("text"):
                    o.human_decide(case_id, ap["role"], "edit", "tightened wording", alt)
                    continue
            o.human_decide(case_id, ap["role"], "approve", "approved")

    def finish_monitoring(self, case_id: str) -> str:
        o = self.o
        case = o.s.get(case_id)
        if case.stage not in ("MONITORING", "HANDED_OFF") or not case.monitoring:
            return case.status
        if case.plan_action == "hand_off":
            return case.status
        started = datetime.fromisoformat(case.monitoring["started"])
        o.s.clock.set(started + timedelta(hours=case.monitoring["window_hours"] + 1))
        m1 = max(0, (case.mentions_1h or 0) // 2)
        o.observe(case_id, m1, max(case.mentions_6h or 1, m1, 1), case.current_reach, "scheduled follow-up: stable")
        if o.try_close(case_id) == "awaiting_closure_approval":
            self.respond(case_id)
        return o.s.get(case_id).status

    def prepare_parent(self, parent_id: str, event: str) -> None:
        o = self.o
        p = o.s.get(parent_id)
        if p is None:
            return
        if event == "public_resolution" and p.stage in ("AWAITING_APPROVAL", "HANDED_OFF"):
            self.respond(parent_id)
        elif event == "reappearance":
            if p.stage in ("AWAITING_APPROVAL", "HANDED_OFF"):
                self.respond(parent_id)
            p = o.s.get(parent_id)
            if p.stage in ("MONITORING", "HANDED_OFF") and p.status != "CLOSED_WITH_EVIDENCE":
                if p.plan_action == "hand_off":
                    o.human_close(parent_id, "Human owner", "owner confirmed the original issue was resolved")
                else:
                    self.finish_monitoring(parent_id)


def load_dataset(path: str) -> pd.DataFrame:
    return pd.read_excel(path, sheet_name="Incident_Cases")


def run_dataset(df: pd.DataFrame, db_path: str = ":memory:", limit: int | None = None):
    table = {r.Case_ID: (str(r.Evidence_Available), str(r.Evidence_Basis)) for r in df.itertuples()}
    silent = {r.Case_ID for r in df.itertuples() if r.Exception_Flag == "E3"}
    store = Store(db_path)
    orch = Orchestrator(store, connector=SimulatedEvidenceConnector(table))
    humans = SimHumans(orch, silent)
    rows = list(df.itertuples())[:limit] if limit else list(df.itertuples())
    base = [r for r in rows if not isinstance(r.Parent_Case_ID, str)]
    upd = [r for r in rows if isinstance(r.Parent_Case_ID, str)]
    for r in base:
        store.clock.set(pd.Timestamp(r.Timestamp).to_pydatetime())      # each case is processed at its own feed time
        orch.ingest(raw_from_row(r))
    for r in upd:
        humans.prepare_parent(r.Parent_Case_ID, detect_event(r.Incident_Text))
        store.clock.set(max(store.clock.now(), pd.Timestamp(r.Timestamp).to_pydatetime()))
        orch.ingest(raw_from_row(r))
    for c in store.all_cases():
        if c.stage in ("AWAITING_APPROVAL", "HANDED_OFF") or c.status in ("AWAITING_HUMAN_ACK", "AWAITING_CLOSURE_APPROVAL"):
            humans.respond(c.case_id)
    for c in store.all_cases():
        if c.stage == "MONITORING":
            humans.finish_monitoring(c.case_id)
    for c in store.all_cases():
        if c.status in ("AWAITING_HUMAN_ACK", "AWAITING_APPROVAL", "AWAITING_CLOSURE_APPROVAL"):
            humans.respond(c.case_id)
    for c in store.all_cases():
        if not c.final_status:
            c.final_status = c.status
            store.save(c)
    return orch, store
