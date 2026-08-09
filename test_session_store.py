"""
Tests for session_store.py: Session serialization round-trip.

Run:
    SESSION_STORE_PATH=/tmp/test_sessions_phase1.db python3 -m pytest test_session_store.py -v
"""

import dataclasses
import os
import sys
from pathlib import Path

TEST_DB_PATH = os.environ.get("SESSION_STORE_PATH") or "/tmp/test_sessions_phase1.db"
os.environ["SESSION_STORE_PATH"] = TEST_DB_PATH

sys.path.insert(0, str(Path(__file__).resolve().parent))

import session_store  # noqa: E402
from main import Session  # noqa: E402


def _make_nondefault_session() -> Session:
    """Build a Session with all fields set to non-default values."""
    return Session(
        messages=[
            {"role": "system", "content": "你是一个商务拜访执行 Agent……"},
            {"role": "user", "content": "下周二安排去上海拜访客户，把事情办妥"},
            {
                "role": "assistant",
                "content": "我先查一下客户信息。",
                "tool_calls": [
                    {
                        "id": "call_abc123",
                        "type": "function",
                        "function": {"name": "get_customer_info", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_abc123",
                "content": '{"company_name": "上海蓝湾科技有限公司"}',
            },
        ],
        checklist={
            "flight": True,
            "hotel": True,
            "meeting": True,
            "calendar_sync": False,
            "email": False,
        },
        approval_state={"hotel_over_budget_approved": True},
        action_log=[
            {"name": "search_flight", "args": {"date": "下周二"}, "result": {"status": "success"}},
            {
                "name": "book_flight",
                "args": {"flight_id": "CA1835"},
                "result": {"status": "success", "flight_id": "CA1835", "order_id": "FLT-1000"},
            },
            {
                "name": "book_hotel",
                "args": {"hotel_id": "HTL-A"},
                "result": {"status": "success", "hotel_id": "HTL-A", "confirmation_id": "HTL-2000"},
            },
        ],
        approval_records=[
            {
                "reason": "酒店超出预算",
                "proposed_action": "预订 HTL-A",
                "approved": True,
                "user_note": "特殊情况，同意超支",
            }
        ],
        booked_flights={
            "CA1835": {"status": "success", "flight_id": "CA1835", "order_id": "FLT-1000"},
        },
        booked_hotels={
            "HTL-A": {"status": "success", "hotel_id": "HTL-A", "confirmation_id": "HTL-2000"},
        },
        lifecycle_state="AWAITING_APPROVAL",
        model_calls=5,
        seen_readonly_calls={
            ("search_flight", '{"date": "下周二"}'): 2,
            ("get_travel_policy", "{}"): 1,
        },
    )


def test_roundtrip_equal_fields():
    """save_session then load_session should produce an equal Session."""
    session = _make_nondefault_session()
    run_id = "test-run-roundtrip-001"

    session_store.save_session(run_id, session)
    loaded = session_store.load_session(run_id)

    assert loaded is not None
    assert loaded == session
    for f in dataclasses.fields(Session):
        assert getattr(loaded, f.name) == getattr(session, f.name), f"field {f.name} mismatch"


def test_load_nonexistent_run_id_returns_none():
    """load_session with unknown run_id returns None, no exception."""
    assert session_store.load_session("this-run-id-does-not-exist-xyz") is None


def test_overwrite_same_run_id_single_row():
    """Saving twice with same run_id overwrites, not inserts a second row."""
    import sqlite3

    session = _make_nondefault_session()
    run_id = "test-run-overwrite-001"

    session_store.save_session(run_id, session)
    session.model_calls = 99
    session.lifecycle_state = "DONE"
    session_store.save_session(run_id, session)

    conn = sqlite3.connect(TEST_DB_PATH)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1

    loaded = session_store.load_session(run_id)
    assert loaded.model_calls == 99
    assert loaded.lifecycle_state == "DONE"


def test_plain_json_types_no_custom_encoder_needed():
    """Verify Session fields are plain JSON-serializable types."""
    import json

    session = _make_nondefault_session()
    d = dataclasses.asdict(session)
    d["seen_readonly_calls"] = session_store._encode_seen_readonly_calls(
        d["seen_readonly_calls"]
    )
    raw = json.dumps(d, ensure_ascii=False)
    back = json.loads(raw)
    assert back["model_calls"] == 5
    assert back["lifecycle_state"] == "AWAITING_APPROVAL"


if __name__ == "__main__":
    test_roundtrip_equal_fields()
    print("✅ test_roundtrip_equal_fields")
    test_load_nonexistent_run_id_returns_none()
    print("✅ test_load_nonexistent_run_id_returns_none")
    test_overwrite_same_run_id_single_row()
    print("✅ test_overwrite_same_run_id_single_row")
    test_plain_json_types_no_custom_encoder_needed()
    print("✅ test_plain_json_types_no_custom_encoder_needed")

    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    print("\nAll round-trip tests passed.")