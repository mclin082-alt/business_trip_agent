"""
Phase 3: 补全全部工具 + Checklist 可视化

在 Phase 2 的基础上，这一步做了两件事：

1. 把方案书里完整的工具集补齐（CRM 查询、双方日历查询、差旅政策查询、酒店搜索、
   酒店/机票预订、日历同步、发确认邮件），一共 10 个"假工具"——都是普通 Python
   函数，返回写死的假数据，形式和 Phase 2 的 3 个工具完全一样。
2. 网页侧边栏加了一个 5 项 Checklist（对应方案书 4 节的 Verifier），随着任务推进
   逐项打勾，勾选状态通过 WebSocket 实时推送。

**关于 Checklist 怎么判定"打没打勾"，这里有一个重要的设计原则需要讲清楚**：
打勾这件事本身是**代码做的**，不是模型决定的——但这不违反"决策必须由模型现场
推理"这条红线。原因是：Checklist 检查的是"客观事实有没有发生"（比如"预订工具
是不是返回了 success"），而不是"接下来该怎么办"。前者是 Verifier（验证器）的
职责，后者才是 Agent 的决策职责，这两者在方案书第四节、第五节里是明确分开的两
层。所以下面 `_update_checklist` 函数里没有任何一行是在替模型判断"该订哪个酒店/
该不该重试"，它只是在核对"航班预订成功了吗、酒店预订成功了吗"这些客观结果。

两个刻意设计的失败点仍然保留：
- 航班：09:00 满员（Phase 2 就有）
- 酒店：客户公司在陆家嘴核心区，附近酒店都超预算——但 mock 数据里给了一个稍远
  但在预算内的选项，模型应该能自己发现并选择它（这一步不需要人工审批介入，
  人工审批是 Phase 4 才加的）。
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

app = FastAPI(title="商务拜访执行 Agent - Phase 3")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ------------------------------------------------------------------
# Qwen API 配置（和 Phase 1/2 一样，走 OpenAI 兼容模式）
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
# 和 Phase 2 一样，这里是唯一允许你为了测试而改动的地方：改这里面的值，不改下面
# 任何函数逻辑，重新运行程序，观察模型的推理和下一步决策是不是合理地跟着变了。
# ------------------------------------------------------------------
MOCK_DATA = {
    "get_customer_info": {
        "company_name": "上海蓝湾科技有限公司",
        "address": "上海市浦东新区陆家嘴环路 1000 号",
        "contact_person": "王经理",
        "contact_phone": "138****6677",
        "note": "客户公司位于陆家嘴核心区，是本次拜访的目的地",
    },
    "get_my_calendar": {
        "date": "下周二",
        "my_free_slots": "全天空闲，无冲突会议",
    },
    "get_customer_calendar": {
        "date": "下周二",
        "customer_free_after": "10:00",
        "note": "客户上午 10:00 之前有内部会议不能见面，10:00 之后到下午都有空",
    },
    "get_travel_policy": {
        "flight_budget_per_person": 1500,
        "hotel_budget_per_night": 800,
        "note": (
            "机票经济舱据实报销，单次不超过 1500 元；酒店每晚不超过 800 元。"
            "超出部分需要经理审批（当前 demo 暂未接入在线审批流程，请在合规范围内"
            "自行给出最优方案，例如更换预算内的酒店）。"
        ),
    },
    "search_flight": {
        # 故意设计的失败点 1：09:00 满员
        "status": "failed",
        "reason": "09:00 上海航班已满员",
        "available_alternatives": ["07:30", "11:30", "14:00"],
    },
    "search_hotel": {
        # 故意设计的失败点 2：陆家嘴附近酒店都超预算，但有一个稍远的选项在预算内
        "status": "over_budget",
        "policy_limit": 800,
        "reason": "客户公司位于陆家嘴核心区，周边酒店均价偏高，近距离选项均超出差旅政策预算上限",
        "options": [
            {"hotel_name": "陆家嘴中心大酒店", "distance_to_customer_km": 0.3, "price_per_night": 950},
            {"hotel_name": "浦东国际会展酒店", "distance_to_customer_km": 0.5, "price_per_night": 920},
            {"hotel_name": "银城中路商务酒店", "distance_to_customer_km": 1.8, "price_per_night": 780},
        ],
    },
}


def tool_get_customer_info(company: str = "") -> dict:
    result = dict(MOCK_DATA["get_customer_info"])
    result["queried_company"] = company
    return result


def tool_get_my_calendar(date: str = "") -> dict:
    result = dict(MOCK_DATA["get_my_calendar"])
    result["queried_date"] = date
    return result


def tool_get_customer_calendar(date: str = "") -> dict:
    result = dict(MOCK_DATA["get_customer_calendar"])
    result["queried_date"] = date
    return result


def tool_get_travel_policy() -> dict:
    return dict(MOCK_DATA["get_travel_policy"])


def tool_search_flight(date: str = "", preferred_time: str = "") -> dict:
    result = dict(MOCK_DATA["search_flight"])
    result["queried_date"] = date
    result["queried_time"] = preferred_time
    return result


def tool_search_hotel(date: str = "", near: str = "") -> dict:
    result = dict(MOCK_DATA["search_hotel"])
    result["queried_date"] = date
    result["queried_near"] = near
    return result


# 用模块级列表模拟"已预订记录"，每次开始新任务时会清空（见 run_agent_loop）
_booked_flights: list[str] = []
_booked_hotels: list[str] = []


def tool_book_flight(flight_time: str = "") -> dict:
    order_id = f"FLT-{1000 + len(_booked_flights)}"
    _booked_flights.append(flight_time)
    return {
        "status": "success",
        "flight_time": flight_time,
        "order_id": order_id,
        "message": f"{flight_time} 航班预订成功，订单号 {order_id}",
    }


def tool_book_hotel(hotel_name: str = "", price_per_night: float = 0) -> dict:
    confirmation_id = f"HTL-{2000 + len(_booked_hotels)}"
    _booked_hotels.append(hotel_name)
    return {
        "status": "success",
        "hotel_name": hotel_name,
        "price_per_night": price_per_night,
        "confirmation_id": confirmation_id,
        "message": f"{hotel_name} 预订成功，确认号 {confirmation_id}",
    }


def tool_sync_calendar(event_title: str = "", date: str = "", time: str = "") -> dict:
    event_id = "EVT-8891"
    return {
        "status": "success",
        "event_id": event_id,
        "event_title": event_title,
        "date": date,
        "time": time,
        "message": f"日历事件已创建，event_id={event_id}",
    }


def tool_send_confirmation_email(to: str = "", summary: str = "") -> dict:
    message_id = "MAIL-5521"
    return {
        "status": "success",
        "to": to,
        "message_id": message_id,
        "message": f"确认邮件已发送至 {to}，message_id={message_id}",
    }


TOOL_FUNCTIONS = {
    "get_customer_info": tool_get_customer_info,
    "get_my_calendar": tool_get_my_calendar,
    "get_customer_calendar": tool_get_customer_calendar,
    "get_travel_policy": tool_get_travel_policy,
    "search_flight": tool_search_flight,
    "search_hotel": tool_search_hotel,
    "book_flight": tool_book_flight,
    "book_hotel": tool_book_hotel,
    "sync_calendar": tool_sync_calendar,
    "send_confirmation_email": tool_send_confirmation_email,
}

# 工具的 JSON Schema 定义（OpenAI 兼容模式下的 function calling 格式）
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_customer_info",
            "description": "查询客户的 CRM 档案，包括公司名称、地址、联系人信息。用于确定拜访目的地。",
            "parameters": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "客户公司名称，如果还不知道可以留空"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_my_calendar",
            "description": "查询我自己在指定日期的日程安排，确认自己是否有空。",
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
            "name": "get_travel_policy",
            "description": "查询公司差旅政策，包括机票和酒店的预算上限，用于判断后续方案是否合规。",
            "parameters": {"type": "object", "properties": {}},
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
            "name": "search_hotel",
            "description": "查询客户公司附近的酒店选项及价格，返回是否在预算内、若超预算还有哪些备选（可能距离更远但更便宜）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "入住日期，例如 '下周二'"},
                    "near": {"type": "string", "description": "希望靠近的地点，例如客户公司地址或所在商圈"},
                },
                "required": ["date", "near"],
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
    {
        "type": "function",
        "function": {
            "name": "book_hotel",
            "description": "预订某个具体酒店。只应该在已经确认该酒店符合预算（或明确判断可以接受超预算方案）之后才调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "hotel_name": {"type": "string", "description": "要预订的酒店名称"},
                    "price_per_night": {"type": "number", "description": "该酒店每晚价格"},
                },
                "required": ["hotel_name", "price_per_night"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sync_calendar",
            "description": "把这次拜访的行程同步为一条日历事件，方便双方以及自己后续查看。应该在航班和酒店都确认之后调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_title": {"type": "string", "description": "日历事件标题，例如 '上海客户拜访'"},
                    "date": {"type": "string", "description": "日期"},
                    "time": {"type": "string", "description": "会面时间"},
                },
                "required": ["event_title", "date", "time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_confirmation_email",
            "description": "向客户发送本次拜访的确认邮件/通知。应该在行程全部确认（航班、酒店、会面时间）之后，作为最后一步调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "收件人，例如客户联系人姓名或邮箱"},
                    "summary": {"type": "string", "description": "本次拜访行程的简要总结，会写进邮件正文"},
                },
                "required": ["to", "summary"],
            },
        },
    },
]


SYSTEM_PROMPT = """你是一个商务拜访执行 Agent，负责帮用户把一次出差安排办妥。

你可以使用的工具覆盖了完成这件事所需要的全部环节：查客户信息（CRM）、查双方日历、
查差旅政策、搜索/预订航班、搜索/预订酒店、同步日历、发送确认邮件给客户。这些工具
之间没有预先写死的调用顺序，具体先查什么、后做什么，由你自己规划。

规则：
1. 工具返回的是原始信息，你需要自己判断这意味着什么、下一步该怎么办——不要假设
   有别的代码会替你处理失败或者超预算的情况。
2. 在每次调用工具之前，务必先用一小段文字说明你的推理过程。这段文字会原样展示
   给用户看，请务必用自然语言写清楚，不要省略。
3. 如果某个查询结果显示失败或者不满足约束（比如航班满员、酒店超预算），不要放弃，
   自己重新想办法（比如换一个时间/换一个酒店选项），直到任务真正完成，或者你判断
   确实无法完成为止。当前 demo 还没有接入人工审批流程，如果遇到超预算的情况，请在
   给定的备选项里自己找到一个合规的方案。
4. 建议的大致顺序是：先了解客户信息和双方日程、查政策，再搜索并确认航班和酒店，
   最后把行程同步到日历、发确认邮件给客户——但如果你判断有更合理的顺序，可以自行
   调整，这不是硬性规定。
5. 当你确认任务已经全部完成（航班、酒店都已预订，日历已同步，确认邮件已发送），
   请输出一段总结文字说明做了什么、结果如何，并且这一轮**不要再调用任何工具**——
   不调用工具就代表你认为任务结束了，循环会就此停止。
"""


async def send(websocket: WebSocket, msg_type: str, **data):
    await websocket.send_text(json.dumps({"type": msg_type, **data}, ensure_ascii=False))


def _call_with_tools(messages: list[dict]):
    """同步阻塞的网络请求，扔到线程池里跑。"""
    response = client.chat.completions.create(
        model=QWEN_MODEL,
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
    )
    return response.choices[0].message


MAX_TURNS = 14  # 工具变多了，适当放宽轮次上限，防止模型陷入死循环时强制停止


# ------------------------------------------------------------------
# Checklist（Verifier）：对照方案书第四节的 5 项完成标准。
#
# 注意这里判断"打没打勾"的逻辑，只是在核对客观结果（工具是不是返回了 success），
# 不涉及"接下来该做什么"的决策——那部分完全由模型的推理文字决定。
# ------------------------------------------------------------------
CHECKLIST_LABELS = {
    "flight": "航班已确认",
    "hotel": "酒店已确认",
    "meeting": "客户会面时间已确认",
    "calendar_sync": "日历事件已同步",
    "email": "确认邮件已发送",
}


def _new_checklist_state() -> dict:
    return {key: False for key in CHECKLIST_LABELS}


def _update_checklist(checklist: dict, customer_calendar_queried: bool, name: str, result: dict) -> None:
    status = result.get("status")
    if name == "book_flight" and status == "success":
        checklist["flight"] = True
    if name == "book_hotel" and status == "success":
        checklist["hotel"] = True
    if name == "sync_calendar" and status == "success":
        checklist["calendar_sync"] = True
    if name == "send_confirmation_email" and status == "success":
        checklist["email"] = True
    # "客户会面时间已确认"：需要同时满足"查过客户日历"和"航班已确认"这两个客观
    # 事实——即已经了解客户的可见面时间窗口，并且已经把能满足这个窗口的航班订下来。
    if customer_calendar_queried and checklist["flight"]:
        checklist["meeting"] = True


async def run_agent_loop(websocket: WebSocket, goal: str):
    """
    核心循环：调用模型 → 它说要用哪个工具 → 执行工具 → 把结果丢回去 → 重复，
    直到它不再调用工具（代表它认为完成了）。

    这个 for 循环里依然没有任何 if "航班满员" / "酒店超预算" then ... 这样的业务
    分支，所有"失败之后怎么办"都是模型自己在推理文字里现场决定的，代码只负责
    机械地执行它选的工具，以及在旁边核对客观结果、更新 Checklist。
    """
    global _booked_flights, _booked_hotels
    _booked_flights = []
    _booked_hotels = []  # 每次新任务开始，清空模拟的预订记录

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

    checklist = _new_checklist_state()
    customer_calendar_queried = False
    await send(websocket, "checklist", items=checklist, labels=CHECKLIST_LABELS)

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

            if name == "get_customer_calendar" and result.get("status") != "error":
                customer_calendar_queried = True

            _update_checklist(checklist, customer_calendar_queried, name, result)
            await send(websocket, "checklist", items=checklist, labels=CHECKLIST_LABELS)

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
    网页输入一个目标、点"开始任务" → 前端发 {"type": "start_task", "goal": "..."}
    → 这个函数调用 run_agent_loop，在循环里通过同一个 WebSocket 把每一步
      （goal / thinking / reasoning / tool_call / tool_result / checklist / done / error）
      推给网页，网页收到后实时渲染成日志流 + 更新侧边栏 Checklist。
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
