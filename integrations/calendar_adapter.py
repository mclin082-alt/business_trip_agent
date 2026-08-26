"""Read-only calendar adapter with mock and MCP implementations.

The Agent continues to call the stable ``get_my_calendar`` Skill.  This
module is deliberately the only layer that knows whether its data comes from
the demo fixture or a real MCP server, so changing calendar vendors never
changes the model-facing tool contract.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any


CALENDAR_PROVIDER = os.environ.get("CALENDAR_PROVIDER", "mock").lower()
CALENDAR_MCP_TIMEOUT_SECONDS = float(os.environ.get("CALENDAR_MCP_TIMEOUT_SECONDS", "5"))
CALENDAR_MCP_COMMAND = os.environ.get("CALENDAR_MCP_COMMAND", "")
CALENDAR_MCP_ARGS = os.environ.get("CALENDAR_MCP_ARGS", "[]")
CALENDAR_MCP_TOOL = os.environ.get("CALENDAR_MCP_TOOL", "get_my_calendar")


def _failure(status: str, reason: str, *, trace_id: str) -> dict[str, Any]:
    """Return a model-safe, provider-neutral failure observation."""
    return {
        "status": status,
        "source": "mcp",
        "trace_id": trace_id,
        "reason": reason,
    }


def _normalise_success(payload: dict[str, Any], *, date: str, trace_id: str) -> dict[str, Any]:
    """Validate the minimum business contract before exposing MCP data."""
    events = payload.get("events")
    free_slots = payload.get("free_slots")
    if not isinstance(events, list) or not isinstance(free_slots, list):
        return _failure(
            "data_missing",
            "日历连接已成功，但返回缺少 events 或 free_slots，不能据此推断为空闲。",
            trace_id=trace_id,
        )
    return {
        "status": "success",
        "source": "mcp",
        "trace_id": trace_id,
        "date": payload.get("date") or date,
        "events": events,
        "free_slots": free_slots,
        "data_as_of": payload.get("data_as_of"),
    }


def _decode_mcp_result(result: Any) -> dict[str, Any] | None:
    """Extract JSON data from the official MCP Python SDK result object."""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    content = getattr(result, "content", []) or []
    for item in content:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded
    return None


async def _call_mcp(date: str, actor_id: str) -> dict[str, Any]:
    """Call one stdio MCP tool.  Tokens are owned by the MCP server, not Session."""
    try:
        args = json.loads(CALENDAR_MCP_ARGS)
    except json.JSONDecodeError as exc:
        raise ValueError("CALENDAR_MCP_ARGS 必须是 JSON 数组") from exc
    if not CALENDAR_MCP_COMMAND:
        raise ValueError("未配置 CALENDAR_MCP_COMMAND，无法启动日历 MCP server")
    if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
        raise ValueError("CALENDAR_MCP_ARGS 必须是字符串数组")

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=CALENDAR_MCP_COMMAND, args=args)
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as client:
            await client.initialize()
            result = await client.call_tool(
                CALENDAR_MCP_TOOL,
                {"date": date, "actor_id": actor_id},
            )
    if getattr(result, "isError", False):
        raise RuntimeError("MCP server 返回工具错误")
    payload = _decode_mcp_result(result)
    if payload is None:
        raise LookupError("MCP server 未返回可解析的结构化日历数据")
    return payload


async def get_my_calendar(date: str, actor_id: str, mock_result: dict[str, Any]) -> dict[str, Any]:
    """Resolve a calendar read without allowing connector errors to escape runtime."""
    if CALENDAR_PROVIDER == "mock":
        result = dict(mock_result)
        result.setdefault("source", "mock")
        return result

    trace_id = f"cal-{uuid.uuid4().hex[:12]}"
    if CALENDAR_PROVIDER != "mcp":
        return _failure("configuration_error", f"不支持的 CALENDAR_PROVIDER: {CALENDAR_PROVIDER}", trace_id=trace_id)
    if not date.strip():
        return _failure("invalid_input", "缺少要查询的日期，无法读取日历。", trace_id=trace_id)

    started = time.monotonic()
    try:
        payload = await asyncio.wait_for(
            _call_mcp(date, actor_id), timeout=CALENDAR_MCP_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        return _failure("temporarily_unavailable", "日历 MCP 读取超时，请稍后重试或向用户确认时间。", trace_id=trace_id)
    except PermissionError:
        return _failure("permission_denied", "当前身份无权读取测试日历；未执行任何写操作。", trace_id=trace_id)
    except LookupError as exc:
        return _failure("data_missing", str(exc), trace_id=trace_id)
    except (ImportError, ValueError) as exc:
        return _failure("configuration_error", str(exc), trace_id=trace_id)
    except Exception as exc:  # Provider details stay in server logs, not model context.
        return _failure("temporarily_unavailable", f"日历 MCP 暂不可用（{type(exc).__name__}）。", trace_id=trace_id)

    result = _normalise_success(payload, date=date, trace_id=trace_id)
    result["latency_ms"] = round((time.monotonic() - started) * 1000)
    return result
