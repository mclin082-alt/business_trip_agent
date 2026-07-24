"""
Phase 2: 核心循环骨架（2-3 个工具，先跑通"决策链路"）

目标：证明"下一步该干嘛是 Qwen/Claude 自己决定的"，不是代码里写死的 if 分支。

做法：
- 定义 3 个"假工具"（其实是普通 Python 函数，返回写死的假数据）：
    1. get_customer_calendar  查客户日程
    2. search_flight          查航班（故意设计成"09:00 满员"这个失败点）
    3. book_flight            预订航班
- 用模型的 tool use / function calling 能力，让它自己判断"现在该调用哪个工具、
  传什么参数"，代码只负责：把工具定义告诉模型 → 执行模型选的工具 → 把结果原样
  丢回去 → 再问模型 → 循环，直到模型不再调用工具（代表它认为任务完成了）。
- 每一步（推理文字 / 工具调用 / 工具返回结果）都通过 WebSocket 推到网页上。

关键验收点见 README：改 MOCK_DATA 里的假返回值，不改任何别的代码，重新运行，
模型的反应应该合理地跟着变——这才说明决策确实来自它现场的推理。
"""

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

load_dotenv()

app = FastAPI(title="商务拜访执行 Agent - Phase 2")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ------------------------------------------------------------------
# Qwen API 配置（和 Phase 1 一样，走 OpenAI 兼容模式）
# ------------------------------------------------------------------
QWEN_API_KEY = os.environ.get("QWEN_API_KEY")
QWEN_BASE_URL = os.environ.get(
    "QWEN_BASE_URL",
    "https://ws-76amum4g5z46vcjk.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
)
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen3.7-max")

client: OpenAI | None = None
if QWEN_API_KEY:
    client = OpenAI(api_key=QWEN_API_KEY, base_url=QWEN_BASE_URL)


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------
# ============ 假工具的"假数据" ============
#
# 这里是唯一允许你为了测试而改动的地方（对应实施计划 Phase 2 的关键验收点）：
# 改这里面的值，不改下面任何函数逻辑，重新运行程序，观察模型的推理和下一步
# 决策是不是合理地跟着变了。比如把 search_flight 的 status 改成 "success"，
# 模型应该就不会再纠结"改订几点"这件事了。
# ------------------------------------------------------------------
MOCK_DATA = {
    "get_customer_calendar": {
        "date": "下周二",
        "customer_free_after": "10:00",
        "note": "客户上午 10:00 之前有内部会议不能见面，10:00 之后到下午都有空",
    },
    "search_flight": {
        # 故意设计的失败点 1：09:00 满员
        "status": "failed",
        "reason": "09:00 上海航班已满员",
        "available_alternatives": ["07:30", "11:30", "14:00"],
    },
}


def tool_get_customer_calendar(date: str = "") -> dict:
    result = dict(MOCK_DATA["get_customer_calendar"])
    result["queried_date"] = date
    return result


def tool_search_flight(date: str = "", preferred_time: str = "") -> dict:
    result = dict(MOCK_DATA["search_flight"])
    result["queried_date"] = date
    result["queried_time"] = preferred_time
    return result


# 用一个模块级列表模拟"已预订记录"，每次开始新任务时会清空（见 run_agent_loop）
_booked_flights: list[str] = []


def tool_book_flight(flight_time: str = "") -> dict:
    order_id = f"MOCK-ORDER-{1000 + len(_booked_flights)}"
    _booked_flights.append(flight_time)
    return {
        "status": "success",
        "flight_time": flight_time,
        "order_id": order_id,
        "message": f"{flight_time} 航班预订成功，订单号 {order_id}",
    }


TOOL_FUNCTIONS = {
    "get_customer_calendar": tool_get_customer_calendar,
    "search_flight": tool_search_flight,
    "book_flight": tool_book_flight,
}

# 工具的 JSON Schema 定义（OpenAI 兼容模式下的 function calling 格式）
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_customer_calendar",
            "description": "查询客户在指定日期的日程安排，了解客户什么时间方便见面。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "要查询的日期，例如 '下周二'"},
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_flight",
            "description": "查询指定日期、指定期望出发时间附近的航班情况，返回是否可订、若不可订还有哪些备选时间等原始信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "出发日期，例如 '下周二'"},
                    "preferred_time": {"type": "string", "description": "期望的出发时间，例如 '09:00'"},
                },
                "required": ["date", "preferred_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_flight",
            "description": "预订某个具体出发时间的航班。只应该在已经确认该时间段有票、且满足会面时间约束之后才调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "flight_time": {"type": "string", "description": "要预订的出发时间，例如 '07:30'"},
                },
                "required": ["flight_time"],
            },
        },
    },
]


SYSTEM_PROMPT = """你是一个商务拜访执行 Agent，负责帮用户把一次出差安排办妥。

规则：
1. 你可以调用给定的工具去查询信息或者执行预订，工具返回的是原始信息，你需要自己
   判断这意味着什么、下一步该怎么办——不要假设有别的代码会替你处理失败情况。
2. 在每次调用工具之前，务必先用一小段文字说明你的推理过程（你为什么要这么做，或者
   拿到上一步结果后你是怎么分析、怎么决定下一步的）。这段文字会原样展示给用户看，
   请务必用自然语言写清楚，不要省略。
3. 如果某个查询结果显示失败或者不满足约束（比如航班满员、时间冲突），不要放弃，
   自己重新想办法（比如换一个时间再查/再订），直到任务真正完成，或者你判断确实
   无法完成为止。
4. 当你确认任务已经完成（比如航班已经成功预订，并且满足了客户的时间约束），请输出
   一段总结文字说明做了什么、结果如何，并且这一轮**不要再调用任何工具**——不调用
   工具就代表你认为任务结束了，循环会就此停止。
"""


async def send(websocket: WebSocket, msg_type: str, **data):
    await websocket.send_text(json.dumps({"type": msg_type, **data}, ensure_ascii=False))


def _call_with_tools(messages: list[dict]):
    """同步阻塞的网络请求，扔到线程池里跑，见 call_qwen 的注释。"""
    response = client.chat.completions.create(
        model=QWEN_MODEL,
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
    )
    return response.choices[0].message


MAX_TURNS = 8  # 防止模型陷入死循环，超过这个轮次就强制停止并提示


async def run_agent_loop(websocket: WebSocket, goal: str):
    """
    核心循环：调用模型 → 它说要用哪个工具 → 执行工具 → 把结果丢回去 → 重复，
    直到它不再调用工具（代表它认为完成了）。

    这是整个方案"Agent 不是 Workflow"的核心验证：下面这个 for 循环里，完全没有
    任何 if "航班满员" then ... 这样的业务分支，所有"失败之后怎么办"都是模型
    自己在推理文字里现场决定的，代码只是机械地执行它选的工具。
    """
    global _booked_flights
    _booked_flights = []  # 每次新任务开始，清空模拟的预订记录

    if client is None:
        await send(
            websocket, "error",
            text="⚠️ 还没配置 QWEN_API_KEY，请在项目根目录的 .env 文件里填好之后重启程序。",
        )
        return

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": goal},
    ]
    await send(websocket, "goal", text=goal)

    for turn in range(MAX_TURNS):
        await send(websocket, "thinking")
        try:
            message = await asyncio.to_thread(_call_with_tools, messages)
        except Exception as exc:  # noqa: BLE001
            await send(websocket, "error", text=f"⚠️ 调用模型失败：{exc}")
            return

        # 把模型这一轮的原始回复存回历史，下一轮请求要带上，否则模型没有记忆
        assistant_msg: dict = {"role": "assistant", "content": message.content or ""}
        if message.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in message.tool_calls
            ]
        messages.append(assistant_msg)

        # 推理文字：这是"证明决策来自现场推理"的最直接证据，必须展示出来
        if message.content:
            await send(websocket, "reasoning", text=message.content)

        if not message.tool_calls:
            # 模型这一轮没有选任何工具，代表它认为任务已经完成
            await send(websocket, "done")
            return

        for tc in message.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            await send(websocket, "tool_call", name=name, arguments=args)

            func = TOOL_FUNCTIONS.get(name)
            if func is None:
                result = {"status": "error", "message": f"未知工具：{name}"}
            else:
                try:
                    result = func(**args)
                except Exception as exc:  # noqa: BLE001
                    result = {"status": "error", "message": str(exc)}

            await send(websocket, "tool_result", name=name, result=result)

            # 把工具的原始返回结果原样喂回去（不做任何"这是什么类型失败"的预判断）
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })

    await send(
        websocket, "error",
        text=f"⚠️ 已经跑了 {MAX_TURNS} 轮还没结束，可能陷入了重复尝试，已强制停止。"
             f"这种情况本身就是 Phase 6 要记录的一类 Bad Case（重复失败/死循环）。",
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    请求路径：
    网页输入一个目标（比如"帮我订一张下周二去上海的机票，要赶上客户上午10点的会面"）、
    点"开始任务" → 前端发 {"type": "start_task", "goal": "..."}
    → 这个函数调用 run_agent_loop，在循环里通过同一个 WebSocket 把每一步
      （goal / thinking / reasoning / tool_call / tool_result / done / error）
      推给网页，网页收到后实时渲染成日志流。
    """
    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if payload.get("type") != "start_task":
                continue

            goal = (payload.get("goal") or "").strip()
            if not goal:
                continue

            await run_agent_loop(websocket, goal)
    except WebSocketDisconnect:
        print("客户端断开连接")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
