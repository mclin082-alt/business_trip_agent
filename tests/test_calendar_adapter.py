"""Contract tests for the read-only MCP calendar boundary."""

import asyncio

from integrations import calendar_adapter


def _run(coro):
    return asyncio.run(coro)


def test_mock_mode_preserves_existing_demo_contract(monkeypatch):
    monkeypatch.setattr(calendar_adapter, "CALENDAR_PROVIDER", "mock")
    result = _run(calendar_adapter.get_my_calendar(
        "下周二", "demo_employee", {"status": "success", "date": "下周二", "events": [], "free_slots": ["09:00-18:00"]}
    ))
    assert result["status"] == "success"
    assert result["source"] == "mock"


def test_mcp_missing_fields_is_not_treated_as_free(monkeypatch):
    async def incomplete(*_args):
        return {"date": "2026-08-26", "events": []}

    monkeypatch.setattr(calendar_adapter, "CALENDAR_PROVIDER", "mcp")
    monkeypatch.setattr(calendar_adapter, "_call_mcp", incomplete)
    result = _run(calendar_adapter.get_my_calendar("2026-08-26", "demo", {}))
    assert result["status"] == "data_missing"
    assert "free_slots" not in result


def test_mcp_permission_denied_is_normalised(monkeypatch):
    async def denied(*_args):
        raise PermissionError("forbidden")

    monkeypatch.setattr(calendar_adapter, "CALENDAR_PROVIDER", "mcp")
    monkeypatch.setattr(calendar_adapter, "_call_mcp", denied)
    result = _run(calendar_adapter.get_my_calendar("2026-08-26", "demo", {}))
    assert result["status"] == "permission_denied"
    assert result["source"] == "mcp"


def test_mcp_timeout_is_normalised(monkeypatch):
    async def never_returns(*_args):
        await asyncio.sleep(0.05)

    monkeypatch.setattr(calendar_adapter, "CALENDAR_PROVIDER", "mcp")
    monkeypatch.setattr(calendar_adapter, "CALENDAR_MCP_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(calendar_adapter, "_call_mcp", never_returns)
    result = _run(calendar_adapter.get_my_calendar("2026-08-26", "demo", {}))
    assert result["status"] == "temporarily_unavailable"
