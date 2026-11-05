"""SQLite case store, decision log (audit trail), approvals, outbox and long-term incident log."""
from __future__ import annotations
import json, sqlite3, threading, time
from typing import Any
from .clock import SimClock
from .models import Case

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases(case_id TEXT PRIMARY KEY, status TEXT, tier TEXT, data TEXT, updated_sim TEXT);
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, sim_time TEXT, wall_ms REAL,
    stage TEXT, agent TEXT, action TEXT, actor_role TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS approvals(id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, role TEXT, state TEXT,
    requested_sim TEXT, due_sim TEXT, decided_sim TEXT, action TEXT, actor TEXT, notes TEXT, draft_version INTEGER,
    reminders INTEGER DEFAULT 0, reminder_sim TEXT, kind TEXT DEFAULT 'response', sla_minutes INTEGER, within_sla INTEGER);
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, sim_time TEXT, channel TEXT,
    text TEXT, approval_id INTEGER, template_id TEXT, mode TEXT);
CREATE TABLE IF NOT EXISTS incident_log(case_id TEXT PRIMARY KEY, norm_key TEXT, incident_type TEXT, tier TEXT,
    status TEXT, created_sim TEXT, closed_sim TEXT, parent TEXT);
CREATE TABLE IF NOT EXISTS flags(k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS ix_log_case ON decision_log(case_id);
CREATE INDEX IF NOT EXISTS ix_appr_case ON approvals(case_id);
"""


class Store:
    def __init__(self, path: str = ":memory:", clock: SimClock | None = None):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript(SCHEMA)
        self.clock = clock or SimClock()
        self._t0 = time.perf_counter()

    def save(self, case: Case) -> None:
        with self.lock:
            self.db.execute("INSERT INTO cases(case_id,status,tier,data,updated_sim) VALUES(?,?,?,?,?) "
                            "ON CONFLICT(case_id) DO UPDATE SET status=excluded.status,tier=excluded.tier,"
                            "data=excluded.data,updated_sim=excluded.updated_sim",
                            (case.case_id, case.status, case.tier, json.dumps(case.to_dict(), default=str), self.clock.iso()))
            self.db.commit()

    def get(self, case_id: str) -> Case | None:
        with self.lock:
            r = self.db.execute("SELECT data FROM cases WHERE case_id=?", (case_id,)).fetchone()
        return Case.from_dict(json.loads(r["data"])) if r else None

    def all_cases(self) -> list[Case]:
        with self.lock:
            rows = self.db.execute("SELECT data FROM cases ORDER BY rowid").fetchall()
        return [Case.from_dict(json.loads(r["data"])) for r in rows]

    def log(self, case_id: str, stage: str, agent: str, action: str, detail: Any = None, actor_role: str = "system") -> None:
        with self.lock:
            self.db.execute("INSERT INTO decision_log(case_id,sim_time,wall_ms,stage,agent,action,actor_role,detail) VALUES(?,?,?,?,?,?,?,?)",
                            (case_id, self.clock.iso(), (time.perf_counter() - self._t0) * 1000, stage, agent, action,
                             actor_role, json.dumps(detail, default=str)))
            self.db.commit()

    def timeline(self, case_id: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM decision_log WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"]) if d["detail"] else None
            out.append(d)
        return out

    def all_log(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM decision_log ORDER BY id").fetchall()]

    def incident_upsert(self, case: Case, norm_key: str, created_sim: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO incident_log(case_id,norm_key,incident_type,tier,status,created_sim,parent) VALUES(?,?,?,?,?,?,?) "
                            "ON CONFLICT(case_id) DO UPDATE SET incident_type=excluded.incident_type,tier=excluded.tier,status=excluded.status",
                            (case.case_id, norm_key, case.incident_type, case.tier, case.status, created_sim, case.parent_case_id))
            self.db.commit()

    def incident_find_key(self, norm_key: str, exclude: str) -> str | None:
        with self.lock:
            r = self.db.execute("SELECT case_id FROM incident_log WHERE norm_key=? AND case_id<>? ORDER BY rowid LIMIT 1",
                                (norm_key, exclude)).fetchone()
        return r["case_id"] if r else None

    def incident_status(self, case_id: str, status: str, tier: str = "") -> None:
        with self.lock:
            self.db.execute("UPDATE incident_log SET status=?, tier=COALESCE(NULLIF(?,''),tier) WHERE case_id=?", (status, tier, case_id))
            if status.startswith("CLOSED"):
                self.db.execute("UPDATE incident_log SET closed_sim=? WHERE case_id=?", (self.clock.iso(), case_id))
            self.db.commit()

    def incident_row(self, case_id: str) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM incident_log WHERE case_id=?", (case_id,)).fetchone()
        return dict(r) if r else None

    def flag(self, k: str, default: str = "") -> str:
        with self.lock:
            r = self.db.execute("SELECT v FROM flags WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def set_flag(self, k: str, v: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO flags(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
            self.db.commit()
