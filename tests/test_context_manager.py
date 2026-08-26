"""Tests for protocol-safe Phase 1 context assembly."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

import main
from context_manager import (
    ContextStrategy,
    build_model_context,
    build_structured_context,
    estimate_context_tokens,
    get_context_strategy,
)
from main import Session


def test_full_history_is_content_equivalent_and_does_not_alias_list():
    session = Session.new("安排上海出差", "system prompt")
    session.messages.append({"role": "assistant", "content": "处理中"})

    context = build_model_context(session, ContextStrategy.FULL_HISTORY)

    assert context == session.messages
    assert context is not session.messages
    context.append({"role": "user", "content": "local-only"})
    assert len(session.messages) == 3


def _tool_unit(call_id: str, name: str, status: str = "success") -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps({"status": status}, ensure_ascii=False),
        },
    ]


def test_sliding_window_keeps_tool_call_and_result_atomic(monkeypatch):
    monkeypatch.setenv("SLIDING_WINDOW_UNITS", "2")
    session = Session.new("安排上海出差", "system prompt")
    session.messages.extend(_tool_unit("call-old", "old_tool"))
    session.messages.extend(_tool_unit("call-new", "new_tool"))
    session.messages.append({"role": "assistant", "content": "继续"})

    context = build_model_context(session, ContextStrategy.SLIDING_WINDOW)

    assert context[0] == session.messages[0]
    assert [message.get("tool_call_id") for message in context if message["role"] == "tool"] == ["call-new"]
    assert any(
        call["id"] == "call-new"
        for message in context
        for call in message.get("tool_calls", [])
    )


def test_structured_compaction_is_threshold_triggered(monkeypatch):
    session = Session.new("安排上海出差", "system prompt")
    session.messages.append({"role": "assistant", "content": "短回复"})
    monkeypatch.setenv("CONTEXT_TOKEN_THRESHOLD", "10000")
    assert build_model_context(session, ContextStrategy.STRUCTURED_COMPACTION) == session.messages

    monkeypatch.setenv("CONTEXT_TOKEN_THRESHOLD", "1")
    context = build_model_context(session, ContextStrategy.STRUCTURED_COMPACTION)
    assert context != session.messages
    assert "<structured_task_context>" in context[1]["content"]


def test_structured_context_uses_runtime_state_and_retains_failures_and_corrections():
    session = Session.new("安排上海出差", "system prompt")
    session.messages.append({"role": "user", "content": "不要早于上午十点的航班"})
    session.checklist["flight"] = True
    session.action_log.extend(
        [
            {
                "name": "book_flight",
                "args": {"flight_id": "MU5101"},
                "result": {"status": "success", "flight_id": "MU5101", "order_id": "FLT-1000"},
            },
            {
                "name": "book_hotel",
                "args": {"hotel_id": "HTL-A"},
                "result": {"status": "error", "message": "sold out"},
            },
        ]
    )
    session.approval_records.append({"approved": False, "reason": "超预算", "user_note": "换一家"})

    structured = build_structured_context(session)

    assert structured["goal"] == "安排上海出差"
    assert structured["user_corrections"] == ["不要早于上午十点的航班"]
    assert structured["resolved_entities"]["book_flight"][0]["order_id"] == "FLT-1000"
    assert structured["failed_actions"][0]["result"]["message"] == "sold out"
    assert structured["approval_decisions"][0]["approved"] is False


def test_compaction_retains_latest_correction_and_pending_tool_call(monkeypatch):
    monkeypatch.setenv("CONTEXT_TOKEN_THRESHOLD", "1")
    monkeypatch.setenv("STRUCTURED_COMPACTION_RECENT_UNITS", "1")
    session = Session.new("安排上海出差", "system prompt")
    session.messages.append({"role": "user", "content": "改成周三出发"})
    session.messages.extend(_tool_unit("call-middle", "search_flight"))
    pending = _tool_unit("call-pending", "request_human_approval")[0]
    session.messages.append(pending)
    session.pending_approval = {
        "call_id": "call-pending",
        "reason": "酒店超预算",
        "proposed_action": "预订 HTL-A",
    }
    session.messages.append({"role": "assistant", "content": "tail"})

    context = build_model_context(session, ContextStrategy.STRUCTURED_COMPACTION)

    assert any(message.get("content") == "改成周三出发" for message in context)
    assert any(
        call["id"] == "call-pending"
        for message in context
        for call in message.get("tool_calls", [])
    )
    assert not any(message.get("tool_call_id") == "call-middle" for message in context)


def test_long_context_compaction_reduces_estimated_tokens_without_mutating_session(monkeypatch):
    monkeypatch.setenv("CONTEXT_TOKEN_THRESHOLD", "1")
    session = Session.new("安排上海出差", "system prompt")
    for index in range(20):
        session.messages.append({"role": "assistant", "content": f"observation-{index}:" + "x" * 800})
    canonical = deepcopy(session.messages)

    context = build_model_context(session, ContextStrategy.STRUCTURED_COMPACTION)

    assert session.messages == canonical
    assert estimate_context_tokens(context) <= estimate_context_tokens(session.messages) * 0.7


def test_strategy_defaults_to_structured_compaction(monkeypatch):
    monkeypatch.delenv("CONTEXT_STRATEGY", raising=False)
    assert get_context_strategy() is ContextStrategy.STRUCTURED_COMPACTION


def test_token_estimate_is_stable_and_nonzero():
    messages = [{"role": "user", "content": "上海 business trip"}]
    assert estimate_context_tokens(messages) == estimate_context_tokens(messages)
    assert estimate_context_tokens(messages) > 0


def test_run_loop_uses_equivalent_context_and_emits_baseline_metrics(monkeypatch):
    class FakeWebSocket:
        def __init__(self):
            self.events = []

        async def send_text(self, payload):
            self.events.append(json.loads(payload))

    session = Session.new("安排上海出差", "system prompt")
    expected_messages = deepcopy(session.messages)
    captured = []

    def fake_call(messages):
        captured.append(deepcopy(messages))
        return SimpleNamespace(content="还需要更多信息", tool_calls=[]), None

    monkeypatch.setattr(main, "client", object())
    monkeypatch.setattr(main, "_call_with_tools", fake_call)
    monkeypatch.setattr(main.session_store, "save_session", lambda *_: None)
    monkeypatch.delenv("CONTEXT_STRATEGY", raising=False)
    monkeypatch.setenv("CONTEXT_TOKEN_THRESHOLD", "10000")

    websocket = FakeWebSocket()
    asyncio.run(main.run_agent_loop(websocket, "context-baseline", session))

    assert captured == [expected_messages]
    usage = next(event for event in websocket.events if event["type"] == "usage")
    assert usage["context_strategy"] == "structured_compaction"
    assert usage["context_compacted"] is False
    assert usage["input_message_count"] == len(expected_messages)
    assert usage["estimated_context_tokens"] > 0
    assert usage["canonical_message_count"] == len(expected_messages)
    assert usage["canonical_estimated_context_tokens"] == usage["estimated_context_tokens"]
    assert usage["model_latency_ms"] >= 0
    assert usage["context_build_latency_ms"] >= 0
