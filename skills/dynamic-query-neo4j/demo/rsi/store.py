from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "state" / "rsi.db"


class EpisodeStore:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self):
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS episodes (
              run_id TEXT PRIMARY KEY,
              question TEXT NOT NULL,
              intent TEXT,
              policy_version INTEGER NOT NULL,
              status TEXT NOT NULL,
              attempts INTEGER NOT NULL,
              failures_json TEXT NOT NULL,
              repairs_json TEXT NOT NULL,
              attempts_json TEXT NOT NULL DEFAULT '[]',
              answer_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evaluations (
              id TEXT PRIMARY KEY,
              parent_version INTEGER NOT NULL,
              candidate_id TEXT NOT NULL,
              candidate_policy_json TEXT NOT NULL,
              evaluation_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS promotions (
              version INTEGER PRIMARY KEY,
              parent_version INTEGER,
              candidate_id TEXT NOT NULL,
              score REAL NOT NULL,
              created_at TEXT NOT NULL
            );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(episodes)")}
            if "attempts_json" not in columns:
                db.execute("ALTER TABLE episodes ADD COLUMN attempts_json TEXT NOT NULL DEFAULT '[]'")

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def save_episode(self, state: dict[str, Any]) -> None:
        answer = state.get("answer") or {}
        if hasattr(answer, "model_dump"):
            answer = answer.model_dump()
        repairs = [x.model_dump() if hasattr(x, "model_dump") else x for x in state.get("repairs", [])]
        validation = state.get("validation")
        failures = validation.failures if hasattr(validation, "failures") else state.get("failures", [])
        with self.connect() as db:
            db.execute("""
            INSERT OR REPLACE INTO episodes
              (run_id, question, intent, policy_version, status, attempts,
               failures_json, repairs_json, attempts_json, answer_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                state["run_id"], state["question"], state.get("intent"),
                state["policy"].version, answer.get("status", "failed"),
                state.get("attempt", 0), json.dumps(failures), json.dumps(repairs),
                json.dumps(state.get("attempt_traces", [])), json.dumps(answer), self.now(),
            ))

    def recent_repairable_failures(self, limit: int = 50) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""
            SELECT * FROM episodes
            WHERE repairs_json != '[]'
            ORDER BY created_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(row) for row in rows]

    def save_evaluation(self, parent_version: int, candidate_id: str,
                        policy: dict, evaluation: dict) -> str:
        evaluation_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute("""
            INSERT INTO evaluations VALUES (?, ?, ?, ?, ?, ?)
            """, (evaluation_id, parent_version, candidate_id, json.dumps(policy),
                  json.dumps(evaluation), self.now()))
        return evaluation_id

    def save_promotion(self, version: int, parent_version: int | None,
                       candidate_id: str, score: float) -> None:
        with self.connect() as db:
            db.execute("""
            INSERT OR REPLACE INTO promotions VALUES (?, ?, ?, ?, ?)
            """, (version, parent_version, candidate_id, score, self.now()))
