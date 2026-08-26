"""Run one real, read-only MCP calendar call against a test account.

Usage:
  CALENDAR_PROVIDER=mcp CALENDAR_MCP_COMMAND=python \
  CALENDAR_MCP_ARGS='["/abs/path/to/calendar_server.py"]' \
  python eval/mcp_smoke_test.py 2026-08-26
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integrations.calendar_adapter import get_my_calendar  # noqa: E402


async def main() -> int:
    if os.environ.get("CALENDAR_PROVIDER", "mock").lower() != "mcp":
        print("请设置 CALENDAR_PROVIDER=mcp；smoke test 不会在 mock 模式下伪造真实连接。")
        return 2
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-08-26"
    result = await get_my_calendar(date, "test_calendar_reader", {})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
