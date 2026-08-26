"""A deliberately read-only MCP server backed by the locally authenticated Lark CLI.

Run only after `lark-cli config init --new` and user OAuth login.  The server
never receives App Secret or user tokens; lark-cli keeps those in its keychain.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date as date_type, datetime, time, timedelta
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Business Trip Agent — Feishu Calendar (read-only)")
TZ = ZoneInfo("Asia/Shanghai")


def _day(value: str) -> date_type:
    # A connector must receive an absolute date; natural-language conversion
    # remains the Agent's responsibility and must be made explicit upstream.
    return date_type.fromisoformat(value)


def _event_items(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "events", "data"):
            nested = payload.get(key)
            if nested is not None:
                return _event_items(nested)
    return []


@mcp.tool()
async def get_my_calendar(date: str, actor_id: str = "") -> dict:
    """Read the authenticated user's Feishu calendar for one ISO date, with no writes."""
    day = _day(date)
    start = datetime.combine(day, time.min, TZ)
    end = start + timedelta(days=1)
    proc = await asyncio.create_subprocess_exec(
        "lark-cli", "calendar", "+agenda", "--as", "user",
        "--start", start.isoformat(), "--end", end.isoformat(), "--format", "json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        message = stderr.decode("utf-8", errors="replace")[-500:]
        if "permission" in message.lower() or "auth" in message.lower():
            raise PermissionError("飞书用户授权或日历读取权限不足")
        raise RuntimeError("飞书日历读取失败")
    raw = json.loads(stdout.decode("utf-8"))
    events = []
    for item in _event_items(raw):
        if not isinstance(item, dict):
            continue
        events.append({
            "title": item.get("summary") or item.get("title") or "（无标题日程）",
            "time": item.get("time") or item.get("start_time") or item.get("start") or "",
        })
    # Do not calculate free_slots from a partially understood CLI response.
    # Returning no slots makes the adapter surface data_missing rather than
    # falsely presenting a user as free. A validated real response is added
    # after the first smoke test establishes the agenda JSON shape.
    return {"date": date, "events": events, "free_slots": [], "data_as_of": datetime.now(TZ).isoformat()}


if __name__ == "__main__":
    mcp.run(transport="stdio")
