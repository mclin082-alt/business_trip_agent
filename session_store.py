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
CURRENT_SESSION_SCHEMA_VERSION = 3


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


def _migrate_session_dict(session_dict: dict, session_class: type) -> dict:
    """Upgrade persisted Session JSON to the current dataclass shape."""
    migrated = dict(session_dict)
    stored_version = migrated.get("schema_version", 0)
    if not isinstance(stored_version, int) or stored_version < 0:
        raise ValueError(f"Invalid Session schema_version: {stored_version!r}")
    if stored_version > CURRENT_SESSION_SCHEMA_VERSION:
        raise ValueError(
            f"Session schema_version {stored_version} is newer than supported "
            f"version {CURRENT_SESSION_SCHEMA_VERSION}"
        )

    # A paused pre-v3 run has no Booking proposal or Policy review artifacts.
    # Keep it on the legacy execution contract when resumed instead of
    # retroactively making its already-generated book call impossible.
    if stored_version < 3 and "multi_agent_enabled" not in migrated:
        migrated["multi_agent_enabled"] = False

    # Version 0 is every Session written before schema_version was introduced.
    # Dataclass defaults provide backward compatibility for fields added over
    # time without duplicating those defaults in the persistence layer.
    for field in dataclasses.fields(session_class):
        if field.name in migrated:
            continue
        if field.default is not dataclasses.MISSING:
            migrated[field.name] = field.default
        elif field.default_factory is not dataclasses.MISSING:
            migrated[field.name] = field.default_factory()

    migrated["schema_version"] = CURRENT_SESSION_SCHEMA_VERSION
    return migrated


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

    session_dict = _migrate_session_dict(json.loads(row[0]), Session)
    session_dict["seen_readonly_calls"] = _decode_seen_readonly_calls(
        session_dict.get("seen_readonly_calls", [])
    )
    return Session(**session_dict)
