"""
Session serialization and SQLite persistence.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from main import Session


SESSION_STORE_PATH = os.environ.get("SESSION_STORE_PATH", "sessions.db")


def _encode_seen_readonly_calls(seen_readonly_calls: dict) -> list:
    """Convert {(name, args_json): count} to a JSON-serializable list."""
    return [[list(key), count] for key, count in seen_readonly_calls.items()]


def _decode_seen_readonly_calls(encoded: list) -> dict:
    """Restore {(name, args_json): count} from encoded list."""
    return {tuple(key): count for key, count in encoded}


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    run_id TEXT PRIMARY KEY,
    session_json TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    """Open a new connection per call; ensure table exists."""
    dirname = os.path.dirname(SESSION_STORE_PATH)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    conn = sqlite3.connect(SESSION_STORE_PATH)
    conn.execute(_CREATE_TABLE_SQL)
    conn.commit()
    return conn


def save_session(run_id: str, session: "Session") -> None:
    """Serialize session to JSON and upsert into SQLite. Blocking — caller wraps with asyncio.to_thread."""
    session_dict = dataclasses.asdict(session)
    session_dict["seen_readonly_calls"] = _encode_seen_readonly_calls(
        session_dict["seen_readonly_calls"]
    )
    session_json = json.dumps(session_dict, ensure_ascii=False)
    updated_at = datetime.now(timezone.utc).isoformat()

    conn = _connect()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO sessions (run_id, session_json, lifecycle_state, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (run_id, session_json, session.lifecycle_state, updated_at),
        )
        conn.commit()
    finally:
        conn.close()


def load_session(run_id: str) -> "Session | None":
    """Load session by run_id. Returns None if not found. Blocking — caller wraps with asyncio.to_thread."""
    from main import Session  # deferred import to avoid circular dependency

    conn = _connect()
    try:
        row = conn.execute(
            "SELECT session_json FROM sessions WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    session_dict = json.loads(row[0])
    session_dict["seen_readonly_calls"] = _decode_seen_readonly_calls(
        session_dict["seen_readonly_calls"]
    )
    return Session(**session_dict)