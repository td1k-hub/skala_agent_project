"""Persistent local metadata plus verified LangSmith trace lookup."""
import json
import sqlite3
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    def __init__(self, path):
        self.path = path

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("""CREATE TABLE IF NOT EXISTS analysis_runs (
          run_id TEXT PRIMARY KEY, equipment_id TEXT NOT NULL, mode TEXT NOT NULL,
          model TEXT, state TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
          error_code TEXT, metrics TEXT NOT NULL DEFAULT '{}', trace_id TEXT,
          tracing_enabled INTEGER NOT NULL DEFAULT 0)""")
        con.execute("""CREATE TABLE IF NOT EXISTS agent_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
          event TEXT NOT NULL, latency_ms REAL NOT NULL)""")
        con.execute("CREATE INDEX IF NOT EXISTS ix_agent_events_run ON agent_events(run_id)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_analysis_runs_started ON analysis_runs(started_at)")
        if "kind" not in {row[1] for row in con.execute("PRAGMA table_info(analysis_runs)")}:
            con.execute("ALTER TABLE analysis_runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'analysis'")
        return con

    def begin(self, run_id, equipment_id, settings, trace_id, kind="analysis"):
        with self.connect() as con:
            con.execute("INSERT INTO analysis_runs (run_id,equipment_id,mode,model,state,started_at,trace_id,tracing_enabled,kind) VALUES (?,?,?,?,?,?,?,?,?)",
                        (run_id, equipment_id, settings.llm_mode, settings.active_model,
                         "queued", now(), trace_id, int(settings.langsmith_tracing), kind))
        con.close()

    def running(self, run_id):
        with self.connect() as con:
            con.execute("UPDATE analysis_runs SET state='running' WHERE run_id=?", (run_id,))
        con.close()

    def event(self, run_id, report, latency_ms):
        with self.connect() as con:
            con.execute("INSERT INTO agent_events (run_id,event,latency_ms) VALUES (?,?,?)",
                        (run_id, json.dumps(report, ensure_ascii=False), latency_ms))
        con.close()

    def finish(self, run_id, state, metrics=None, error_code=None):
        with self.connect() as con:
            con.execute("UPDATE analysis_runs SET state=?,completed_at=?,error_code=? WHERE run_id=?",
                        (state, now(), error_code, run_id))
            if metrics is not None:
                con.execute("UPDATE analysis_runs SET metrics=? WHERE run_id=?", (metrics.model_dump_json(), run_id))
        con.close()

    def decode(self, row):
        item = dict(row)
        item["metrics"] = json.loads(item["metrics"])
        item["tracing_enabled"] = bool(item["tracing_enabled"])
        return item

    def list(self, limit=50):
        with self.connect() as con:
            rows = con.execute("SELECT * FROM analysis_runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        con.close()
        return [self.decode(row) for row in rows]

    def detail(self, run_id):
        with self.connect() as con:
            row = con.execute("SELECT * FROM analysis_runs WHERE run_id=?", (run_id,)).fetchone()
            events = con.execute("SELECT event,latency_ms FROM agent_events WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        con.close()
        if row is None:
            return None
        return {**self.decode(row), "agents": [{**json.loads(e["event"]), "latency_ms": e["latency_ms"]} for e in events]}

    def summary(self):
        # Explicitly a latest-100-run window, not lifetime totals.
        rows = self.list(100)
        times = [r["metrics"].get("total_ms", 0) for r in rows if r["state"] == "completed"]
        calls = [c for r in rows for c in r["metrics"].get("llm_calls", [])]
        return {"window": 100, "runs": len(rows), "completed": sum(r["state"] == "completed" for r in rows),
                "failed": sum(r["state"] == "failed" for r in rows), "cancelled": sum(r["state"] == "cancelled" for r in rows),
                "active": sum(r["state"] in {"running", "queued"} for r in rows),
                "average_ms": sum(times) / len(times) if times else None,
                "input_tokens": sum(c.get("input_tokens") or 0 for c in calls),
                "output_tokens": sum(c.get("output_tokens") or 0 for c in calls),
                "unreported_usage_calls": sum(c.get("input_tokens") is None or c.get("output_tokens") is None for c in calls)}
