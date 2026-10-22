"""Server-owned analysis snapshots and conversation history, separate from telemetry."""
import json
import sqlite3
from contextlib import contextmanager
from time import time
from uuid import uuid4

from .models import AnalysisResult, Measurements
from .monitoring import now


class ChatProblem(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status_code = code, message, status
        super().__init__(code)


class ConversationStore:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            con.execute("""CREATE TABLE IF NOT EXISTS analysis_snapshots (
              run_id TEXT PRIMARY KEY, equipment_id TEXT NOT NULL, created_at TEXT NOT NULL,
              result TEXT NOT NULL, measurements TEXT NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS chat_sessions (
              id TEXT PRIMARY KEY, equipment_id TEXT NOT NULL, analysis_run_id TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              busy_run_id TEXT, busy_until REAL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS chat_messages (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
              session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
              created_at TEXT NOT NULL, response TEXT)""")
            if "research_cycle_id" not in {row[1] for row in con.execute("PRAGMA table_info(chat_sessions)")}:
                con.execute("ALTER TABLE chat_sessions ADD COLUMN research_cycle_id INTEGER")
            con.execute("CREATE INDEX IF NOT EXISTS ix_chat_messages_session ON chat_messages(session_id,sequence)")
            con.execute("CREATE INDEX IF NOT EXISTS ix_chat_sessions_equipment ON chat_sessions(equipment_id,updated_at)")
            con.execute("CREATE INDEX IF NOT EXISTS ix_analysis_equipment_time ON analysis_snapshots(equipment_id,created_at)")
            with con:
                yield con
        finally:
            con.close()

    def save_analysis(self, result, measurements):
        with self.connection() as con:
            con.execute("INSERT INTO analysis_snapshots VALUES (?,?,?,?,?)",
                        (result.run_id, result.equipment_id, now(), result.model_dump_json(), measurements.model_dump_json()))

    def analysis(self, run_id):
        with self.connection() as con:
            row = con.execute("SELECT * FROM analysis_snapshots WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        return {"result": AnalysisResult.model_validate_json(row["result"]).model_dump(mode="json"),
                "measurements": Measurements.model_validate_json(row["measurements"]).model_dump()}

    def list_analyses(self, equipment_id, limit=30, through_run_id=None):
        """Only completed, saved analyses for this equipment; newest first."""
        with self.connection() as con:
            cutoff = con.execute("SELECT rowid FROM analysis_snapshots WHERE run_id=? AND equipment_id=?", (through_run_id, equipment_id)).fetchone() if through_run_id else None
            if through_run_id and cutoff is None:
                raise ChatProblem("ANALYSIS_NOT_FOUND", "연결된 분석 결과를 찾을 수 없습니다.", 404)
            rows = con.execute("SELECT run_id,created_at,result,measurements FROM analysis_snapshots WHERE equipment_id=? AND (? IS NULL OR rowid<=?) ORDER BY rowid DESC LIMIT ?",
                               (equipment_id, cutoff["rowid"] if cutoff else None, cutoff["rowid"] if cutoff else None, min(max(limit, 1), 100))).fetchall()
        return [{"run_id": row["run_id"], "created_at": row["created_at"],
                 "status": (result := AnalysisResult.model_validate_json(row["result"])).status,
                 "measurements": Measurements.model_validate_json(row["measurements"]).model_dump()}
                for row in rows]

    def latest_analysis_run_id(self, equipment_id):
        with self.connection() as con:
            row = con.execute("SELECT run_id FROM analysis_snapshots WHERE equipment_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (equipment_id,)).fetchone()
        return row["run_id"] if row else None

    def create(self, equipment_id, analysis_run_id=None, research_cycle_id=None):
        if analysis_run_id:
            snapshot = self.analysis(analysis_run_id)
            if snapshot is None:
                raise ChatProblem("ANALYSIS_NOT_FOUND", "연결할 분석 결과가 없습니다. 수치 분석을 새로 실행해 주세요.", 404)
            if snapshot["result"]["equipment_id"] != equipment_id:
                raise ChatProblem("ANALYSIS_EQUIPMENT_MISMATCH", "선택한 설비와 분석 결과의 설비가 다릅니다.")
        item = {"id": str(uuid4()), "equipment_id": equipment_id, "analysis_run_id": analysis_run_id,
                "research_cycle_id": research_cycle_id,
                "created_at": now(), "updated_at": now()}
        with self.connection() as con:
            con.execute("INSERT INTO chat_sessions (id,equipment_id,analysis_run_id,research_cycle_id,created_at,updated_at) VALUES (?,?,?,?,?,?)", tuple(item.values()))
        return item

    def list(self, equipment_id):
        with self.connection() as con:
            rows = con.execute("SELECT id,equipment_id,analysis_run_id,research_cycle_id,created_at,updated_at FROM chat_sessions WHERE equipment_id=? ORDER BY updated_at DESC LIMIT 30", (equipment_id,)).fetchall()
        return [dict(row) for row in rows]

    def get(self, session_id):
        with self.connection() as con:
            row = con.execute("SELECT * FROM chat_sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise ChatProblem("CHAT_NOT_FOUND", "상담 대화를 찾을 수 없습니다.", 404)
            # Bounded history. Old messages remain stored until the session is deleted.
            messages = con.execute("SELECT * FROM (SELECT * FROM chat_messages WHERE session_id=? ORDER BY sequence DESC LIMIT 100) ORDER BY sequence", (session_id,)).fetchall()
        item = {k: row[k] for k in ("id", "equipment_id", "analysis_run_id", "research_cycle_id", "created_at", "updated_at")}
        item["busy"] = bool(row["busy_run_id"] and (row["busy_until"] or 0) > time())
        item["messages"] = [{"id": m["id"], "role": m["role"], "content": m["content"], "created_at": m["created_at"],
                              "response": json.loads(m["response"]) if m["response"] else None} for m in messages]
        item["analysis"] = self.analysis(row["analysis_run_id"]) if row["analysis_run_id"] else None
        return item

    def claim(self, session_id, run_id, lease_seconds):
        with self.connection() as con:
            count = con.execute("UPDATE chat_sessions SET busy_run_id=?,busy_until=? WHERE id=? AND (busy_run_id IS NULL OR busy_until<?)",
                                (run_id, time() + lease_seconds, session_id, time())).rowcount
            if not count:
                if con.execute("SELECT 1 FROM chat_sessions WHERE id=?", (session_id,)).fetchone() is None:
                    raise ChatProblem("CHAT_NOT_FOUND", "상담 대화를 찾을 수 없습니다.", 404)
                raise ChatProblem("CHAT_BUSY", "이 대화의 답변을 생성하고 있습니다. 완료 후 다시 질문해 주세요.")

    def release(self, session_id, run_id):
        with self.connection() as con:
            con.execute("UPDATE chat_sessions SET busy_run_id=NULL,busy_until=NULL WHERE id=? AND busy_run_id=?", (session_id, run_id))

    def commit_turn(self, session_id, run_id, question, response):
        with self.connection() as con:
            row = con.execute("SELECT busy_run_id FROM chat_sessions WHERE id=?", (session_id,)).fetchone()
            if row is None or row["busy_run_id"] != run_id:
                raise ChatProblem("CHAT_LEASE_EXPIRED", "대화 처리 권한이 만료됐습니다. 다시 질문해 주세요.")
            con.execute("INSERT INTO chat_messages (id,session_id,role,content,created_at) VALUES (?,?,?,?,?)",
                        (str(uuid4()), session_id, "user", question, now()))
            con.execute("INSERT INTO chat_messages (id,session_id,role,content,created_at,response) VALUES (?,?,?,?,?,?)",
                        (response.message_id, session_id, "assistant", response.answer, now(), response.model_dump_json()))
            con.execute("UPDATE chat_sessions SET updated_at=? WHERE id=?", (now(), session_id))

    def delete(self, session_id):
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT busy_run_id,busy_until FROM chat_sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise ChatProblem("CHAT_NOT_FOUND", "상담 대화를 찾을 수 없습니다.", 404)
            if row["busy_run_id"] and (row["busy_until"] or 0) > time():
                raise ChatProblem("CHAT_BUSY", "답변 생성 중에는 대화를 삭제할 수 없습니다.")
            con.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
            con.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))
