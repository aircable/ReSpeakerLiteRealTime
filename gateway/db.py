import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  goal TEXT NOT NULL DEFAULT '',
  instructions TEXT NOT NULL DEFAULT '',
  pinned_notes TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  decisions_json TEXT NOT NULL DEFAULT '[]',
  open_questions_json TEXT NOT NULL DEFAULT '[]',
  plan_markdown TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_project ON projects(active) WHERE active = 1;
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  device_id TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  end_reason TEXT,
  realtime_model TEXT NOT NULL,
  usage_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  ordinal INTEGER NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('user','assistant')),
  item_id TEXT,
  text TEXT NOT NULL DEFAULT '',
  interrupted INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(session_id, ordinal)
);
CREATE TABLE IF NOT EXISTS plan_revisions (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  session_id INTEGER REFERENCES sessions(id),
  summary TEXT NOT NULL,
  decisions_json TEXT NOT NULL,
  open_questions_json TEXT NOT NULL,
  plan_markdown TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            if conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0:
                stamp = now()
                conn.execute(
                    "INSERT INTO projects(name, goal, active, created_at, updated_at) VALUES(?,?,?,?,?)",
                    ("First project", "Define the project goal", 1, stamp, stamp),
                )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_projects(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM projects ORDER BY updated_at DESC")]

    def get_project(self, project_id: int | None = None) -> dict[str, Any]:
        with self.connect() as conn:
            if project_id is None:
                row = conn.execute("SELECT * FROM projects WHERE active=1").fetchone()
            else:
                row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if row is None:
                raise KeyError("project not found")
            return dict(row)

    def create_project(self, name: str, goal: str = "") -> dict[str, Any]:
        stamp = now()
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO projects(name, goal, created_at, updated_at) VALUES(?,?,?,?)",
                (name, goal, stamp, stamp),
            )
            project_id = cursor.lastrowid
        return self.get_project(project_id)

    def update_project(self, project_id: int, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {"name", "goal", "instructions", "pinned_notes", "summary", "plan_markdown"}
        values = {key: value for key, value in values.items() if key in allowed}
        if not values:
            return self.get_project(project_id)
        values["updated_at"] = now()
        assignments = ", ".join(f"{key}=?" for key in values)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE projects SET {assignments} WHERE id=?",  # noqa: S608 - keys allowlisted
                (*values.values(), project_id),
            )
        return self.get_project(project_id)

    def activate_project(self, project_id: int) -> None:
        with self.connect() as conn:
            if conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                raise KeyError("project not found")
            conn.execute("UPDATE projects SET active=0 WHERE active=1")
            conn.execute("UPDATE projects SET active=1, updated_at=? WHERE id=?", (now(), project_id))

    def start_session(self, project_id: int, device_id: str, model: str) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO sessions(project_id, device_id, started_at, realtime_model) VALUES(?,?,?,?)",
                (project_id, device_id, now(), model),
            )
            return int(cursor.lastrowid)

    def end_session(self, session_id: int, reason: str, usage: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE sessions SET ended_at=?, end_reason=?, usage_json=? WHERE id=?",
                (now(), reason, json.dumps(usage), session_id),
            )

    def add_turn(
        self, session_id: int, role: str, text: str, item_id: str | None = None, interrupted: bool = False
    ) -> int:
        with self.connect() as conn:
            ordinal = conn.execute(
                "SELECT COALESCE(MAX(ordinal),0)+1 FROM turns WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO turns(session_id, ordinal, role, item_id, text, interrupted, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (session_id, ordinal, role, item_id, text, interrupted, now()),
            )
            return ordinal

    def recent_turns(self, project_id: int, limit: int = 12) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT t.* FROM turns t JOIN sessions s ON s.id=t.session_id "
                "WHERE s.project_id=? ORDER BY t.id DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def session_turns(self, session_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute("SELECT * FROM turns WHERE session_id=? ORDER BY ordinal", (session_id,))
            ]

    def apply_plan_update(self, project_id: int, session_id: int, update: dict[str, Any]) -> None:
        """Write the current project view and immutable revision in one transaction."""
        decisions = json.dumps(update["decisions"])
        questions = json.dumps(update["open_questions"])
        stamp = now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE projects SET summary=?, decisions_json=?, open_questions_json=?, "
                "plan_markdown=?, updated_at=? WHERE id=?",
                (update["summary"], decisions, questions, update["plan_markdown"], stamp, project_id),
            )
            conn.execute(
                "INSERT INTO plan_revisions(project_id, session_id, summary, decisions_json, "
                "open_questions_json, plan_markdown, created_at) VALUES(?,?,?,?,?,?,?)",
                (project_id, session_id, update["summary"], decisions, questions, update["plan_markdown"], stamp),
            )

    def plan_history(self, project_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM plan_revisions WHERE project_id=? ORDER BY id DESC", (project_id,)
                )
            ]

    def project_turns(self, project_id: int, limit: int = 100) -> list[dict[str, Any]]:
        return self.recent_turns(project_id, min(max(limit, 1), 500))

    def setting_overrides(self) -> dict[str, Any]:
        with self.connect() as conn:
            return {row["key"]: json.loads(row["value_json"]) for row in conn.execute("SELECT * FROM settings")}

    def update_settings(self, values: dict[str, Any]) -> None:
        with self.connect() as conn:
            for key, value in values.items():
                conn.execute(
                    "INSERT INTO settings(key, value_json) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                    (key, json.dumps(value)),
                )
