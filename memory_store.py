"""SQLite-backed, entity-scoped long-term memory for the agent runtime.

Memories are soft preference data.  This module deliberately has no access to
policy or approval state, so a stored record cannot become an authorization.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


MEMORY_STORE_PATH = os.environ.get("MEMORY_STORE_PATH", "memories.db")
VALID_SUBJECT_TYPES = {"actor", "customer", "route"}
VALID_SOURCE_TYPES = {"explicit", "inferred"}
VALID_STATUSES = {"active", "superseded", "deleted"}
DEFAULT_PER_TYPE_LIMIT = 3
DEFAULT_INFERRED_TTL_DAYS = 90

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    value_json TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_run_id TEXT NOT NULL,
    evidence TEXT NOT NULL,
    evidence_count INTEGER NOT NULL DEFAULT 1,
    confidence REAL NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    CHECK(subject_type IN ('actor', 'customer', 'route')),
    CHECK(source_type IN ('explicit', 'inferred')),
    CHECK(status IN ('active', 'superseded', 'deleted'))
);
CREATE INDEX IF NOT EXISTS idx_memories_subject_active
    ON memories(subject_type, subject_id, status, memory_type, updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_run_idempotency
    ON memories(source_run_id, subject_type, subject_id, memory_type, normalized_value);
CREATE TABLE IF NOT EXISTS memory_evidence (
    memory_id TEXT NOT NULL,
    source_run_id TEXT NOT NULL,
    evidence TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(memory_id, source_run_id),
    FOREIGN KEY(memory_id) REFERENCES memories(memory_id)
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    dirname = os.path.dirname(MEMORY_STORE_PATH)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    conn = sqlite3.connect(MEMORY_STORE_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_CREATE_TABLE_SQL)
    conn.commit()
    return conn


def _normalized_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_record(record: dict) -> None:
    missing = [
        key for key in ("subject_type", "subject_id", "memory_type", "value_json", "source_run_id")
        if record.get(key) in (None, "")
    ]
    if missing:
        raise ValueError(f"Missing memory fields: {', '.join(missing)}")
    if record["subject_type"] not in VALID_SUBJECT_TYPES:
        raise ValueError(f"Invalid subject_type: {record['subject_type']!r}")
    if record.get("source_type", "explicit") not in VALID_SOURCE_TYPES:
        raise ValueError(f"Invalid source_type: {record.get('source_type')!r}")
    confidence = float(record.get("confidence", 1.0))
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")


def _row_to_record(row: sqlite3.Row) -> dict:
    record = dict(row)
    record["value_json"] = json.loads(record["value_json"])
    try:
        record["evidence"] = json.loads(record["evidence"])
    except (TypeError, json.JSONDecodeError):
        pass
    record.pop("normalized_value", None)
    return record


def upsert_memory(record: dict) -> dict:
    """Idempotently insert or merge the same normalized memory.

    Replaying the same run is a no-op.  Matching evidence from a different run
    increments evidence_count; explicit evidence always outranks inferred.
    """
    _validate_record(record)
    now = _utc_now()
    value_text = _normalized_json(record["value_json"])
    source_type = record.get("source_type", "explicit")
    confidence = float(record.get("confidence", 1.0 if source_type == "explicit" else 0.5))
    evidence_text = _normalized_json(record.get("evidence", ""))
    expires_at = record.get("expires_at")
    if source_type == "inferred" and not expires_at:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=DEFAULT_INFERRED_TTL_DAYS)).isoformat()

    conn = _connect()
    try:
        same_run = conn.execute(
            """
            SELECT * FROM memories
            WHERE source_run_id = ? AND subject_type = ? AND subject_id = ?
              AND memory_type = ? AND normalized_value = ?
            """,
            (
                record["source_run_id"], record["subject_type"], record["subject_id"],
                record["memory_type"], value_text,
            ),
        ).fetchone()
        if same_run is not None:
            return _row_to_record(same_run)

        existing = conn.execute(
            """
            SELECT * FROM memories
            WHERE subject_type = ? AND subject_id = ? AND memory_type = ?
              AND normalized_value = ? AND status = 'active'
            ORDER BY updated_at DESC LIMIT 1
            """,
            (record["subject_type"], record["subject_id"], record["memory_type"], value_text),
        ).fetchone()
        if existing is not None:
            seen_evidence = conn.execute(
                "SELECT 1 FROM memory_evidence WHERE memory_id = ? AND source_run_id = ?",
                (existing["memory_id"], record["source_run_id"]),
            ).fetchone()
            if seen_evidence is not None:
                return _row_to_record(existing)
            merged_source = "explicit" if "explicit" in {existing["source_type"], source_type} else "inferred"
            merged_confidence = max(float(existing["confidence"]), confidence)
            conn.execute(
                """
                UPDATE memories SET source_type = ?, evidence = ?,
                    evidence_count = evidence_count + 1, confidence = ?, updated_at = ?, expires_at = ?
                WHERE memory_id = ?
                """,
                (
                    merged_source, evidence_text, merged_confidence,
                    now, None if merged_source == "explicit" else expires_at, existing["memory_id"],
                ),
            )
            conn.execute(
                "INSERT INTO memory_evidence(memory_id, source_run_id, evidence, created_at) VALUES (?, ?, ?, ?)",
                (existing["memory_id"], record["source_run_id"], evidence_text, now),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM memories WHERE memory_id = ?", (existing["memory_id"],)).fetchone()
            return _row_to_record(row)

        memory_id = record.get("memory_id") or str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO memories (
                memory_id, subject_type, subject_id, memory_type, value_json,
                normalized_value, source_type, source_run_id, evidence,
                evidence_count, confidence, status, created_at, updated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'active', ?, ?, ?)
            """,
            (
                memory_id, record["subject_type"], record["subject_id"], record["memory_type"],
                value_text, value_text, source_type, record["source_run_id"], evidence_text,
                confidence, now, now, expires_at,
            ),
        )
        conn.execute(
            "INSERT INTO memory_evidence(memory_id, source_run_id, evidence, created_at) VALUES (?, ?, ?, ?)",
            (memory_id, record["source_run_id"], evidence_text, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM memories WHERE memory_id = ?", (memory_id,)).fetchone()
        return _row_to_record(row)
    finally:
        conn.close()


def supersede_memory(old_id: str, new_record: dict) -> dict:
    """Insert a replacement and mark one active record superseded."""
    old = get_memory(old_id)
    if old is None:
        raise KeyError(f"Unknown memory_id: {old_id}")
    replacement = dict(new_record)
    replacement.setdefault("subject_type", old["subject_type"])
    replacement.setdefault("subject_id", old["subject_id"])
    replacement.setdefault("memory_type", old["memory_type"])
    created = upsert_memory(replacement)
    if created["memory_id"] != old_id:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE memories SET status = 'superseded', updated_at = ? WHERE memory_id = ? AND status = 'active'",
                (_utc_now(), old_id),
            )
            conn.commit()
        finally:
            conn.close()
    return created


def get_memory(memory_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM memories WHERE memory_id = ?", (memory_id,)).fetchone()
        return _row_to_record(row) if row else None
    finally:
        conn.close()


def _normalize_subjects(subjects: Iterable[dict | tuple[str, str]]) -> list[tuple[str, str]]:
    normalized = []
    for subject in subjects:
        if isinstance(subject, dict):
            pair = (subject.get("subject_type"), subject.get("subject_id"))
        else:
            pair = tuple(subject)
        if len(pair) != 2 or pair[0] not in VALID_SUBJECT_TYPES or not pair[1]:
            raise ValueError(f"Invalid memory subject: {subject!r}")
        normalized.append((str(pair[0]), str(pair[1])))
    return normalized


def retrieve_memories(subjects, task_context: dict | None = None, limit: int = 9) -> list[dict]:
    """Retrieve scoped active memories, ranked without semantic/vector search."""
    if limit <= 0:
        return []
    normalized = _normalize_subjects(subjects)
    if not normalized:
        return []
    task_context = task_context or {}
    allowed_types = set(task_context.get("memory_types") or [])
    now = _utc_now()
    clauses = " OR ".join("(subject_type = ? AND subject_id = ?)" for _ in normalized)
    params = [item for pair in normalized for item in pair]
    conn = _connect()
    try:
        rows = conn.execute(
            f"""
            SELECT * FROM memories
            WHERE status = 'active' AND (expires_at IS NULL OR expires_at > ?)
              AND ({clauses})
            ORDER BY CASE source_type WHEN 'explicit' THEN 0 ELSE 1 END,
                     confidence DESC, evidence_count DESC, updated_at DESC
            """,
            [now, *params],
        ).fetchall()
    finally:
        conn.close()

    selected: list[dict] = []
    per_type: dict[tuple[str, str, str], int] = {}
    per_type_limit = int(task_context.get("per_type_limit", DEFAULT_PER_TYPE_LIMIT))
    for row in rows:
        if allowed_types and row["memory_type"] not in allowed_types:
            continue
        key = (row["subject_type"], row["subject_id"], row["memory_type"])
        if per_type.get(key, 0) >= per_type_limit:
            continue
        selected.append(_row_to_record(row))
        per_type[key] = per_type.get(key, 0) + 1
        if len(selected) >= limit:
            break
    return selected


def list_memories(subject_type: str, subject_id: str, include_inactive: bool = False) -> list[dict]:
    _normalize_subjects([(subject_type, subject_id)])
    conn = _connect()
    try:
        status_clause = "" if include_inactive else "AND status = 'active'"
        rows = conn.execute(
            f"""SELECT * FROM memories WHERE subject_type = ? AND subject_id = ? {status_clause}
                 ORDER BY updated_at DESC""",
            (subject_type, subject_id),
        ).fetchall()
        return [_row_to_record(row) for row in rows]
    finally:
        conn.close()


def forget_memory(memory_id: str) -> bool:
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE memories SET status = 'deleted', updated_at = ? WHERE memory_id = ? AND status != 'deleted'",
            (_utc_now(), memory_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def clear_memories(subject_id: str, subject_type: str | None = None) -> int:
    conn = _connect()
    try:
        if subject_type is None:
            cursor = conn.execute(
                "UPDATE memories SET status = 'deleted', updated_at = ? WHERE subject_id = ? AND status = 'active'",
                (_utc_now(), subject_id),
            )
        else:
            _normalize_subjects([(subject_type, subject_id)])
            cursor = conn.execute(
                """UPDATE memories SET status = 'deleted', updated_at = ?
                   WHERE subject_type = ? AND subject_id = ? AND status = 'active'""",
                (_utc_now(), subject_type, subject_id),
            )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def compact_memories(subject_id: str, memory_type: str, subject_type: str | None = None) -> int:
    """Enforce the per-scope cap, preferring explicit, consistent, recent data."""
    conn = _connect()
    try:
        params: list[Any] = [subject_id, memory_type, _utc_now()]
        subject_clause = ""
        if subject_type is not None:
            _normalize_subjects([(subject_type, subject_id)])
            subject_clause = "AND subject_type = ?"
            params.append(subject_type)
        rows = conn.execute(
            f"""
            SELECT memory_id FROM memories
            WHERE subject_id = ? AND memory_type = ? AND status = 'active'
              AND (expires_at IS NULL OR expires_at > ?) {subject_clause}
            ORDER BY CASE source_type WHEN 'explicit' THEN 0 ELSE 1 END,
                     confidence DESC, evidence_count DESC, updated_at DESC
            """,
            params,
        ).fetchall()
        stale_ids = [row["memory_id"] for row in rows[DEFAULT_PER_TYPE_LIMIT:]]
        if stale_ids:
            placeholders = ",".join("?" for _ in stale_ids)
            conn.execute(
                f"UPDATE memories SET status = 'superseded', updated_at = ? WHERE memory_id IN ({placeholders})",
                [_utc_now(), *stale_ids],
            )
            conn.commit()
        return len(stale_ids)
    finally:
        conn.close()


_LONG_TERM_CUE = re.compile(r"请记住|记住我|以后|今后|一直|通常|默认|我(?:明确)?偏好|我喜欢|我不喜欢|不要再|改为|改成|不再")
_CORRECTION_CUE = re.compile(r"改为|改成|不再|不要再|纠正|之前.*(?:不对|作废)")
_HOTEL_PREFERENCE = re.compile(
    r"(?P<negative>不喜欢|避免|不要再?|不再选择)|(?P<positive>偏好|喜欢|优先(?:选择|考虑)?)"
    r"\s*(?:改为|改成|是|为)?\s*"
    r"(?P<brand>[A-Za-z][A-Za-z .&'-]{1,30}?|[\u3400-\u9fff]{2,12}?)"
    r"(?=酒店|集团|品牌|[，,。；;\s]|$)(?:酒店|集团|品牌)?"
)
_FLIGHT_TERMS = {
    "直飞": "direct",
    "早班": "morning",
    "晚班": "evening",
    "靠窗": "window_seat",
    "靠过道": "aisle_seat",
    "过道座位": "aisle_seat",
    "公务舱": "business_class",
    "经济舱": "economy_class",
}
_CONTACT_PATTERN = re.compile(
    r"(?:联系(?:这个|该)?客户时|给(?:这个|该)?客户发邮件时|客户联系惯例(?:是|为)?)"
    r"[，,:：\s]*(?P<value>[^。；;\n]{2,100})"
)


def _subject_for_text(text: str, subjects: dict, memory_type: str) -> tuple[str, str] | None:
    customer_id = subjects.get("customer")
    company = subjects.get("company") or ""
    if customer_id and (
        memory_type == "contact_convention"
        or "这个客户" in text or "该客户" in text or (company and company in text)
    ):
        return "customer", str(customer_id)
    route_id = subjects.get("route")
    if route_id and memory_type == "flight_preference" and any(cue in text for cue in ("这条航线", "该航线", "往返")):
        return "route", str(route_id)
    actor_id = subjects.get("actor")
    return ("actor", str(actor_id)) if actor_id else None


def extract_explicit_memories(texts: Iterable[str], subjects: dict, source_run_id: str) -> list[dict]:
    """Conservatively extract only explicitly stated durable preferences.

    This deterministic MVP intentionally misses ambiguous prose rather than
    turning one-off booking behavior into a preference.
    """
    records: list[dict] = []
    for text in texts:
        if not isinstance(text, str) or not _LONG_TERM_CUE.search(text):
            continue
        correction = bool(_CORRECTION_CUE.search(text))
        for match in _HOTEL_PREFERENCE.finditer(text):
            brand = match.group("brand").strip(" ，,。.;；")
            if not brand or "航班" in brand or any(term in brand for term in _FLIGHT_TERMS):
                continue
            subject = _subject_for_text(text, subjects, "hotel_brand")
            if subject:
                records.append({
                    "subject_type": subject[0], "subject_id": subject[1],
                    "memory_type": "hotel_brand",
                    "value_json": {"brand": brand, "preference": "avoid" if match.group("negative") else "prefer"},
                    "source_type": "explicit", "source_run_id": source_run_id,
                    "evidence": text, "confidence": 1.0, "is_correction": correction,
                })

        flight_values = [value for term, value in _FLIGHT_TERMS.items() if term in text]
        if flight_values and any(cue in text for cue in ("航班", "航程", "飞行", "航线", "出差")):
            subject = _subject_for_text(text, subjects, "flight_preference")
            if subject:
                records.append({
                    "subject_type": subject[0], "subject_id": subject[1],
                    "memory_type": "flight_preference", "value_json": {"preferences": sorted(set(flight_values))},
                    "source_type": "explicit", "source_run_id": source_run_id,
                    "evidence": text, "confidence": 1.0, "is_correction": correction,
                })

        contact = _CONTACT_PATTERN.search(text)
        if contact:
            subject = _subject_for_text(text, subjects, "contact_convention")
            if subject:
                records.append({
                    "subject_type": subject[0], "subject_id": subject[1],
                    "memory_type": "contact_convention", "value_json": {"convention": contact.group("value").strip()},
                    "source_type": "explicit", "source_run_id": source_run_id,
                    "evidence": text, "confidence": 1.0, "is_correction": correction,
                })
    return records


def persist_explicit_memories(texts: Iterable[str], subjects: dict, source_run_id: str) -> list[dict]:
    """Write extracted memories; explicit corrections supersede the old slot."""
    written: list[dict] = []
    for extracted in extract_explicit_memories(texts, subjects, source_run_id):
        record = dict(extracted)
        is_correction = bool(record.pop("is_correction", False))
        if is_correction:
            old_records = list_memories(record["subject_type"], record["subject_id"])
            conflicts = [item for item in old_records if item["memory_type"] == record["memory_type"]]
            if conflicts:
                created = supersede_memory(conflicts[0]["memory_id"], record)
                for extra in conflicts[1:]:
                    forget_memory(extra["memory_id"])
            else:
                created = upsert_memory(record)
        else:
            created = upsert_memory(record)
        compact_memories(record["subject_id"], record["memory_type"], record["subject_type"])
        written.append(created)
    return written
