"""
Phase 4 + 5 + 6.5

在 Phase 3 的基础上：

1. **人工审批（Phase 4）**：新增一个特殊工具 `request_human_approval`。模型判断
   遇到"预算超出政策上限"或"即将向客户发送最终确认邮件"这两类场景时，必须先
   调用这个工具，网页上会弹出一张卡片展示它给出的理由和打算执行的动作，程序会
   真正暂停，等用户点"批准/拒绝"之后，这个结果才作为下一个 Observation 喂回模型
   继续循环——不是命令行里的 `input()`，也不是自动跳过。

2. **执行层/决策层最终报告（Phase 5）**：五项 Checklist 全部完成、模型这一轮不再
   调用任何工具时，不是直接显示"Done"，而是再调用一次模型，让它基于本次任务
   真实发生的工具调用历史和审批记录，生成一份区分两层的总结：✅ 执行层（订单号、
   确认号这些有客观依据、可验证的事实）和 ⚠️ 决策层（选择的取舍、被批准的例外，
   需要人复核的地方）。

3. **会话延续 + 审批拒绝可选反馈（Phase 6.5，见项目内 Phase6.5 设计记录文档）**：

   之前的版本有一个架构漏洞——"模型这一轮没有调用工具"被无条件当成"任务已经
   完成"，于是不管 Checklist 有没有全部打勾，都会直接 `done` 并销毁本次任务的
   `messages`。但实际上"没调用工具"至少对应两种完全不同的情况：
     a) 任务真的做完了（Checklist 5/5）
     b) 模型缺信息/缺决策依据，用文字反问用户，在等用户回话（Checklist 未满）
   旧代码只认 (a)，导致 (b) 发生时（最典型的触发点是审批被拒绝后模型反问该怎么
   改）程序直接判定任务结束、上下文被销毁，用户的下一句回复只能走 `start_task`
   开一个全新的、上下文为空的任务——表现出来就是模型"失忆"。

   修复方式：**不再单独用"有没有调用工具"判断任务是否结束，而是叠加 Checklist
   完成度**。没调用工具但 Checklist 未满时，不 `return`，而是挂起在一个新的
   `reply_queue.get()` 上等用户在同一个任务里继续说话，收到之后把这句话追加进
   同一份 `messages`，循环继续——这和 Phase 4 里等待审批结果的 `approval_queue`
   是同一种机制，只是这次等的是一段文字而不是布尔值。

   这条修复完全遵守"下一步怎么办必须是模型现场推理决定"的红线：代码没有替模型
   判断"该怎么回答用户""该怎么改方案"，代码只是在管理会话状态——什么时候算真
   结束、什么时候该把新消息续进同一个上下文——这是工程管道层面的事，不是业务
   判断。

   同时顺带做了一个体验优化（非必须）：审批卡片"拒绝"按钮旁边加了一个可选的
   文本框，用户可以直接写一句反馈（比如"改成更便宜的酒店"），减少一轮"模型反问
   →用户在等待回复里再答一次"的往返；不填也完全可以，走普通的等待回复流程。
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

app = FastAPI(title="商务拜访执行 Agent - Phase 6.5")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ------------------------------------------------------------------
# Qwen API 配置（和之前一样，走 OpenAI 兼容模式）
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
# 这里是唯一允许你为了测试而改动的地方：改这里面的值，不改下面任何函数逻辑，
# 重新运行程序，观察模型的推理和下一步决策是不是合理地跟着变了。
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
            "超出部分需要经理审批——遇到超预算的情况，请调用 request_human_approval "
            "工具说明理由并等待批准，不要自己直接下单。"
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
            "description": "预订某个具体酒店。如果该酒店价格在预算内可以直接调用；如果超出差旅政策的每晚预算上限，必须先调用 request_human_approval 获得批准之后才能调用这个工具，不能自己直接下单。",
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
            "description": "向客户发送本次拜访的确认邮件/通知。这是面向客户的外部动作，应该在行程全部确认（航班、酒店、会面时间）之后，并且已经通过 request_human_approval 获得最终发送确认之后，作为最后一步调用。",
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
    {
        "type": "function",
        "function": {
            "name": "request_human_approval",
            "description": (
                "在需要人工审批的场景下调用此工具，暂停当前流程，向用户展示理由和你打算"
                "执行的动作，等待用户点击'批准'或'拒绝'后再继续。以下两种情况必须调用它："
                "① 打算预订的酒店价格超出差旅政策的每晚预算上限；"
                "② 正式向客户发送确认邮件之前的最终确认（无论前面是否超预算都需要走这一步，"
                "因为这是面向客户的外部、不可逆动作）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "为什么需要人工审批，用自然语言说明当前情况"},
                    "proposed_action": {"type": "string", "description": "如果获得批准，你打算执行的具体动作"},
                },
                "required": ["reason", "proposed_action"],
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
3. 如果某个查询结果显示失败或者不满足约束（比如航班满员），不要放弃，自己重新
   想办法（比如换一个时间），直到任务真正完成，或者你判断确实无法完成为止。
4. 以下两种情况，你必须先调用 `request_human_approval` 工具，说明理由和你打算
   执行的动作，等待用户批准之后才能继续对应的下一步，不能自己直接执行：
   a）打算预订的酒店价格超出差旅政策规定的每晚预算上限；
   b）正式调用 send_confirmation_email 向客户发送确认邮件之前，都要先走一次
      最终确认（这是面向客户的外部动作）。
   如果用户拒绝了某次审批请求，你需要重新想办法（比如换一个方案）而不是无视
   拒绝结果继续按原计划执行。如果用户拒绝时附带了具体说明，优先按照说明调整；
   如果没有附带说明、或者你没有足够信息独立决定该怎么调整，可以用文字向用户
   提问澄清，这一轮你可以不调用任何工具——系统会等待用户回复，不会因此把任务
   判定为已结束。
5. 建议的大致顺序是：先了解客户信息和双方日程、查政策，再搜索并确认航班和酒店，
   最后把行程同步到日历、发确认邮件给客户——但如果你判断有更合理的顺序，可以自行
   调整，这不是硬性规定。
6. 当你确认任务已经全部完成（航班、酒店都已预订，日历已同步，确认邮件已发送），
   请输出一段总结文字说明做了什么、结果如何，并且这一轮**不要再调用任何工具**——
   不调用工具、且所有事项都已办妥，才代表任务真正结束。如果任务还没办妥而你这一
   轮没有调用工具（比如你在等用户回答一个问题），系统会把它当作"等待用户回复"，
   而不是任务结束，你之后依然可以正常调用工具继续推进。
"""

FINAL_REPORT_PROMPT = """你是同一个商务拜访执行 Agent，本次任务的所有动作已经执行完毕，现在需要你输出
一份区分两层的总结报告，帮助用户快速判断"哪些是百分百确定做对的，哪些需要人复核"。

请**只输出 JSON**，不要有任何 JSON 之外的文字、不要用 Markdown 代码块包裹，格式如下：
{{"execution_layer": ["...", "..."], "decision_layer": ["...", "..."]}}

- execution_layer（执行层）：只写有客观依据、可以验证的事实，例如订单号/确认号/event_id/
  message_id 确实存在、Checklist 是否全部完成。每条一句话，尽量带上具体编号。
- decision_layer（决策层）：写需要人工复核的权衡取舍，例如为什么选了这个方案而不是别的、
  审批中批准的超预算金额和理由、可能存在的风险（比如可退改灵活度、临时改期怎么办）。
  如果本次任务完全没有需要复核的点，就如实写一条说明"本次未触发需要复核的例外情况"。

务必完全基于下面这份真实执行记录来写，不要编造没有发生过的信息。

目标：{goal}

Checklist 完成状态：
{checklist}

工具调用与返回历史（按发生顺序）：
{tool_history}

人工审批记录：
{approval_records}
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


MAX_TURNS = 16  # 只统计"真正调用了模型"的轮次，不包含等待用户回复/等待审批的时间


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


def _format_tool_history(action_log: list[dict]) -> str:
    if not action_log:
        return "（无）"
    lines = []
    for item in action_log:
        lines.append(
            f"- 调用 {item['name']}，参数：{json.dumps(item['args'], ensure_ascii=False)}，"
            f"返回：{json.dumps(item['result'], ensure_ascii=False)}"
        )
    return "\n".join(lines)


def _format_approval_records(approval_records: list[dict]) -> str:
    if not approval_records:
        return "（本次任务未触发任何人工审批）"
    lines = []
    for r in approval_records:
        outcome = "已批准" if r["approved"] else "被拒绝"
        line = f"- 理由：{r['reason']}；拟执行动作：{r['proposed_action']}；结果：{outcome}"
        if r.get("user_note"):
            line += f"；用户补充说明：{r['user_note']}"
        lines.append(line)
    return "\n".join(lines)


async def generate_final_report(goal: str, checklist: dict, action_log: list[dict], approval_records: list[dict]) -> dict:
    """
    Phase 5：五项 Checklist 全部完成后，再单独调用一次模型（不带 tools），让它基于
    这次任务真实发生的工具调用历史和审批记录，生成执行层/决策层二分报告。

    这次调用刻意不复用主循环的 messages 历史，而是重新组织一份干净的"事实清单"
    喂给模型——这样即使主循环的上下文已经很长，报告依然会紧扣真实发生的事情，
    而不是被前面大段的推理文字带偏。
    """
    checklist_text = "\n".join(
        f"- {CHECKLIST_LABELS[key]}：{'已完成' if done else '未完成'}"
        for key, done in checklist.items()
    )
    prompt = FINAL_REPORT_PROMPT.format(
        goal=goal,
        checklist=checklist_text,
        tool_history=_format_tool_history(action_log),
        approval_records=_format_approval_records(approval_records),
    )

    response = await asyncio.to_thread(
        client.chat.completions.create,
        model=QWEN_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = response.choices[0].message.content or ""

    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
        execution_layer = data.get("execution_layer") or []
        decision_layer = data.get("decision_layer") or []
    except json.JSONDecodeError:
        # 模型没有按要求只输出 JSON，兜底：至少别把内容丢了，原样塞进决策层展示
        execution_layer = []
        decision_layer = [raw_text.strip()] if raw_text.strip() else ["⚠️ 报告生成格式异常，请查看后台日志。"]

    return {"execution_layer": execution_layer, "decision_layer": decision_layer}


async def run_agent_loop(
    websocket: WebSocket,
    goal: str,
    approval_queue: "asyncio.Queue[dict]",
    reply_queue: "asyncio.Queue[str]",
):
    """
    核心循环：调用模型 → 它说要用哪个工具 → 执行工具 → 把结果丢回去 → 重复，
    直到它不再调用工具**并且** Checklist 全部满足（代表它认为完成了）。

    这个循环里依然没有任何 if "航班满员" / "酒店超预算" then ... 这样的业务
    分支，所有"失败之后怎么办"都是模型自己在推理文字里现场决定的，代码只负责
    机械地执行它选的工具，以及在旁边核对客观结果、更新 Checklist。

    Phase 4：如果模型调用的是 `request_human_approval`，代码不会立即返回结果，
    而是把请求推给前端、真正暂停在 `await approval_queue.get()` 上，直到用户在
    网页上点了"批准/拒绝"，这个选择（连同可选的一句补充说明）才会作为下一个
    Observation 喂回模型。

    Phase 5：`action_log` 和 `approval_records` 会记录本次任务真实发生的每一次
    工具调用和每一次审批，任务结束时用来生成执行层/决策层报告。

    Phase 6.5（会话延续）：模型"这一轮没有调用工具"不再被无条件当成"任务结束"。
    只有同时满足"没调用工具" + "Checklist 全部完成"，才真正 done、销毁上下文、
    return。如果没调用工具但 Checklist 还没满，说明模型是在用文字向用户提问、
    等待回复（最典型的触发点是审批被拒绝之后），此时代码会把这一状态推给前端，
    真正暂停在 `await reply_queue.get()` 上，等用户在网页上继续说话，再把这句
    话追加进**同一份** `messages`、`continue` 回到循环顶部——不会新建 messages，
    不会丢失之前查过的客户信息、已订的航班酒店记录。
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
    action_log: list[dict] = []
    approval_records: list[dict] = []
    await send(websocket, "checklist", items=checklist, labels=CHECKLIST_LABELS)

    model_calls = 0
    while model_calls < MAX_TURNS:
        await send(websocket, "thinking")
        try:
            message = await asyncio.to_thread(_call_with_tools, messages)
        except Exception as exc:  # noqa: BLE001
            await send(websocket, "error", text=f"⚠️ 调用模型失败：{exc}")
            return
        model_calls += 1

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
            if all(checklist.values()):
                # 情况 a：Checklist 全满，模型没再调用工具 —— 真的做完了
                await send(websocket, "generating_report")
                try:
                    report = await generate_final_report(goal, checklist, action_log, approval_records)
                    await send(websocket, "final_report", **report)
                except Exception as exc:  # noqa: BLE001
                    await send(websocket, "error", text=f"⚠️ 生成最终报告失败：{exc}")
                await send(websocket, "done")
                return
            else:
                # 情况 b：Checklist 还没满，模型是在用文字等用户回复（比如刚被拒绝
                # 审批、缺信息反问）。不能当成任务结束，真正挂起等用户在同一个
                # 任务里继续说话；这不消耗 MAX_TURNS（model_calls 这一轮已经计过，
                # 但等待期间不会再增加），也不会新建/清空 messages。
                await send(websocket, "awaiting_reply")
                user_text = await reply_queue.get()
                messages.append({"role": "user", "content": user_text})
                await send(websocket, "user_reply", text=user_text)
                continue

        for tc in message.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            await send(websocket, "tool_call", name=name, arguments=args)

            if name == "request_human_approval":
                # Phase 4 的核心：这里真正暂停，等用户在网页上点按钮。代码不判断
                # "该不该批准"，那是用户的决定；代码也不判断"要不要发起审批"，那
                # 是模型刚才自己决定的——代码只是搭一座桥，把两边接起来。
                reason = args.get("reason", "")
                proposed_action = args.get("proposed_action", "")
                await send(websocket, "approval_request", reason=reason, proposed_action=proposed_action)
                approval_payload = await approval_queue.get()
                approved = bool(approval_payload.get("approved"))
                user_note = (approval_payload.get("user_note") or "").strip()

                if approved:
                    result = {"status": "approved", "message": f"用户已批准：{proposed_action}"}
                else:
                    base_msg = (
                        "用户拒绝了该请求，请重新考虑其他合规方案，不要无视拒绝结果继续执行原计划。"
                    )
                    if user_note:
                        base_msg += f" 用户补充说明：{user_note}"
                    else:
                        base_msg += (
                            " 用户没有说明具体原因或期望方案，如果你没有足够信息独立判断该怎么调整，"
                            "可以直接用文字向用户提问澄清。"
                        )
                    result = {"status": "rejected", "message": base_msg}

                approval_records.append({
                    "reason": reason,
                    "proposed_action": proposed_action,
                    "approved": approved,
                    "user_note": user_note,
                })
                await send(websocket, "approval_resolved", approved=approved)
            else:
                func = TOOL_FUNCTIONS.get(name)
                if func is None:
                    result = {"status": "error", "message": f"未知工具：{name}"}
                else:
                    try:
                        result = func(**args)
                    except Exception as exc:  # noqa: BLE001
                        result = {"status": "error", "message": str(exc)}

            await send(websocket, "tool_result", name=name, result=result)
            action_log.append({"name": name, "args": args, "result": result})

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
        text=f"⚠️ 已经调用了 {MAX_TURNS} 次模型还没结束，可能陷入了重复尝试，已强制停止。"
             f"这种情况本身就是 Phase 6 要记录的一类 Bad Case（重复失败/死循环）。",
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    请求路径：
    网页输入一个目标、点"开始任务" → 前端发 {"type": "start_task", "goal": "..."}
    → 这个函数把 run_agent_loop 作为一个后台 task 启动，自己继续留在 while 循环里
      收消息，通过同一个 WebSocket 把每一步（goal / thinking / reasoning /
      tool_call / tool_result / checklist / approval_request / approval_resolved /
      awaiting_reply / user_reply / generating_report / final_report / done /
      error）推给网页，网页收到后实时渲染成日志流 + 更新侧边栏 Checklist。

    这里维护两个 asyncio.Queue，分别对应 run_agent_loop 里两种不同的"暂停等
    用户"的场景：
    - approval_queue：等审批卡片的"批准/拒绝"（Phase 4）
    - reply_queue：等用户在"模型反问、任务未结束"时继续说话（Phase 6.5 新增）
    两者机制完全一样，只是 payload 类型不同（dict vs 纯文本），这样前端在任务
    还没真正结束时，用户的每一句话都能路由回**同一个** run_agent_loop 任务，
    而不是又开一个新任务、丢掉之前的上下文。
    """
    await websocket.accept()
    approval_queue: asyncio.Queue = asyncio.Queue()
    reply_queue: asyncio.Queue = asyncio.Queue()
    current_task: asyncio.Task | None = None
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = payload.get("type")

            if msg_type == "start_task":
                if current_task is not None and not current_task.done():
                    # 上一个任务还没跑完（比如正卡在等审批/等回复），忽略新的开始
                    # 请求，前端按钮此时本来也是禁用状态，这里只是双重保险。
                    continue
                goal = (payload.get("goal") or "").strip()
                if not goal:
                    continue
                # 清空上一轮可能残留的队列内容，避免串到新任务里
                while not approval_queue.empty():
                    approval_queue.get_nowait()
                while not reply_queue.empty():
                    reply_queue.get_nowait()
                current_task = asyncio.create_task(
                    run_agent_loop(websocket, goal, approval_queue, reply_queue)
                )

            elif msg_type == "approval_response":
                await approval_queue.put({
                    "approved": bool(payload.get("approved")),
                    "user_note": (payload.get("user_note") or "").strip(),
                })

            elif msg_type == "user_reply":
                # Phase 6.5 新增：任务还在进行中（模型在等回复）时，用户发来的
                # 后续消息走这里，续进同一个 run_agent_loop，而不是 start_task。
                text = (payload.get("text") or "").strip()
                if text:
                    await reply_queue.put(text)

    except WebSocketDisconnect:
        print("客户端断开连接")
        if current_task is not None and not current_task.done():
            current_task.cancel()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
