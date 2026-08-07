"""
商务拜访执行 Agent（Business Trip Execution Agent）

一句话目标 → 模型自主规划 → 调用工具执行 → 读取原始返回结果现场推理下一步，
直到对照 Checklist（Verifier）确认任务真正完成。

核心设计原则：
- **失败之后怎么办，永远由模型现场推理决定，代码里没有任何
  `if 航班满 then ...` 这种业务分支。** 工具返回的原始结果会原样喂给模型，
  代码只负责执行模型选择的下一步动作、以及核对客观结果（Checklist 是否达成）。
- **人工审批（request_human_approval）**：模型判断需要审批时，程序会真正
  暂停，把理由和拟执行动作推给前端展示，等用户点击"批准/拒绝"后，这个结果
  才作为下一个 Observation 喂回模型继续循环。
- **最终报告**：五项 Checklist 全部完成后，再单独调用一次模型，让它基于本次
  任务真实发生的工具调用历史和审批记录，生成"✅ 执行层（有客观依据，如订单号）
  / ⚠️ 决策层（需要人复核的取舍）"二分报告，而不是简单显示"Done"。
- **会话延续**：模型"这一轮没有调用工具"不代表任务结束——它也可能是在用
  文字向用户提问、等待回复（例如审批被拒绝后需要新的方案信息）。只有同时满足
  "没调用工具" + "Checklist 全部完成"才判定任务真正结束。否则会挂起等待用户
  回复，并把回复追加进同一份对话历史继续循环，不会新建任务、不会丢失上下文。

以上四点全部通过同一种机制实现：代码只管理"什么时候该暂停、暂停之后从哪里
继续"这类会话状态，从不替模型做业务判断。

- **预订工具的幂等性**：`book_flight` / `book_hotel` 对同一个 id 的重复调用
  会原样返回第一次的成功结果（同一个订单号），而不是重新走一遍下单逻辑，
  避免了"模型出于确认心理重复调用"被误判为二次下单、造成资损。
- **Prompt Caching**：messages 结构天然满足"稳定前缀（system prompt + tools
  schema）在前、变化内容在后"，自动享受网关的隐式缓存；`PROMPT_CACHE_MODE=
  explicit` 时会额外通过 `extra_body.cache_control` 显式声明缓存粒度。
- **备用模型自动切换**：只识别"额度耗尽"这一类特征明确的错误，一旦命中就
  让当前任务以 `infra_error` 结束（不在同一次运行内部换模型），下一次任务
  会自动使用 `QWEN_BACKUP_MODEL`；`done` 事件里带 `model_used` 字段，方便
  事后区分"模型本身不稳定"和"中途切换了模型版本"。
- **审批的运行时硬约束（安全护栏）**：`classify_approval_purpose`（定义在
  `approval_utils.py`，与 `harness.py` 共用同一份实现）判定一次已批准的审批
  是否属于"酒店超预算"，结果记录在 `approval_state` 里；`tool_book_hotel`
  在真正下单前会检查价格是否超出政策上限、且本次任务是否已获批准，未获批准
  直接拒绝下单，而不是依赖模型自觉遵守 prompt。这条护栏和"失败之后怎么办
  交给模型现场推理"这一设计原则并不冲突——两者划的是"业务判断"（交给模型）
  和"安全底线"（代码硬约束）两类不同的分支，后者是"未经审批不能真的把
  超支订单点下去"这种绝对不能发生的事，不该赌模型一定会听话。
- **最终报告的事实核验**：`generate_final_report` 生成报告后，会用
  `find_fabricated_ids`（同样定义在 `approval_utils.py`）核对 execution_layer
  里提到的订单号/确认号等编号是否在本次任务的 `action_log` 里真实出现过，
  一旦发现编造的编号，就把提示追加进 decision_layer 标红降级展示，而不是
  悄悄相信模型没有幻觉。
- **超时的业务层重试**：`_is_timeout_error` 命中时，在 SDK 层的连接重试
  （`max_retries=1`）之外，再套一层"这一轮模型调用最多重试
  `QWEN_TIMEOUT_MAX_RETRIES` 次、每次退避
  `QWEN_TIMEOUT_RETRY_BACKOFF_SECONDS * 次数` 秒"的业务层重试，只在超时错误
  上生效（配额错误不重试，依旧直接 infra_error + 切换备用模型）；重试用尽
  后仍然正确降级为 `infra_error`，避免"网络抖一下、整个任务就没了"，但也
  不会掩盖真正持续的超时问题。
"""

import asyncio
import json
import os
from copy import deepcopy
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from openai import APITimeoutError, OpenAI

from approval_utils import classify_approval_purpose, find_fabricated_ids

load_dotenv()

app = FastAPI(title="商务拜访执行 Agent")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ------------------------------------------------------------------
# Qwen API 配置（走 OpenAI 兼容模式）
# ------------------------------------------------------------------
QWEN_API_KEY = os.environ.get("QWEN_API_KEY")
QWEN_BASE_URL = os.environ.get(
    "QWEN_BASE_URL",
    "https://ws-76amum4g5z46vcjk.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
)
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen3.7-max")

# ------------------------------------------------------------------
# 单次请求超时（对应 eval 报告"慢请求看起来像卡死"问题）
#
# 之前 OpenAI(...) 没有传 timeout，SDK 默认超时时间很长（量级是分钟级别），
# 网关一旦排队/限流/负载高，单次调用可能挂几分钟才返回——期间终端完全没有
# 任何输出，只能凭感觉判断"是慢还是死"。这里显式给一个较短的超时（默认 90
# 秒，可通过 QWEN_REQUEST_TIMEOUT 覆盖），配合下面的 _is_timeout_error 识别，
# 让"慢请求"在可控时间内变成一个明确的异常，而不是无限期挂起。
#
# max_retries 显式设成 1（而不是 SDK 默认的 2 次+指数退避）：重试次数越多，
# "看起来卡死"的总等待时间越长，跟这里想解决的问题背道而驰；保留 1 次重试
# 是为了兼顾单次网络抖动，不是完全不重试。
# ------------------------------------------------------------------
QWEN_REQUEST_TIMEOUT = float(os.environ.get("QWEN_REQUEST_TIMEOUT", "90"))

# ------------------------------------------------------------------
# 超时的"业务层重试"（对应 eval 报告"超时错误可以先重试、而不是直接判
# infra_error 结束整个任务"）：
#
# openai SDK 的 max_retries=1 是连接层面的重试，跟这里要加的重试不是一回事。
# 这里是在 SDK 那层重试都用完、APITimeoutError（或带 timeout 字样的异常）
# 真正抛到 run_agent_loop 之后，再套一层"这一轮模型调用最多重试 N 次"的
# 循环——只在 _is_timeout_error(exc) 为真时重试，配额错误（_is_quota_error）
# 依旧不重试、直接 infra_error+切模型，两者不是同一类错误，不应该用同一套
# 处理逻辑。重试次数用尽后依然正确降级为 infra_error，不会掩盖真正持续的
# 超时问题。
# ------------------------------------------------------------------
QWEN_TIMEOUT_MAX_RETRIES = int(os.environ.get("QWEN_TIMEOUT_MAX_RETRIES", "2"))
QWEN_TIMEOUT_RETRY_BACKOFF_SECONDS = float(os.environ.get("QWEN_TIMEOUT_RETRY_BACKOFF_SECONDS", "2"))

client: OpenAI | None = None
if QWEN_API_KEY:
    client = OpenAI(
        api_key=QWEN_API_KEY,
        base_url=QWEN_BASE_URL,
        timeout=QWEN_REQUEST_TIMEOUT,
        max_retries=1,
    )


# ------------------------------------------------------------------
# Prompt Caching 配置（对应 eval 报告 方案 4 长期）
#
# 已与网关维护方确认：阿里云百炼（DashScope 兼容模式）对该专属部署同时支持
# 隐式缓存与显式缓存，计费上命中部分按 cached_token 折扣价结算：
#   - 隐式缓存（implicit）：不需要任何额外参数，网关自动识别"请求前缀"是否
#     与近期请求重复并命中缓存。唯一的硬约束是：把不变的内容（system prompt、
#     tools schema）放在请求最前面，会变化的内容（本轮新增的 tool 结果/用户
#     输入）放在最后——这一点本来就是 messages 列表自然增长的方式（system→
#     goal→逐轮 assistant/tool），本身已经满足，不需要额外改动。
#     该专属部署触发隐式缓存的最小前缀长度约为 1000 token（qwen-max 系列），
#     所以只有 SYSTEM_PROMPT + TOOLS schema 这类"体量较大且稳定"的前缀才有
#     实际收益，短对话不会命中，这是符合预期的。
#   - 显式缓存（explicit）：通过 OpenAI 兼容接口的 extra_body 传
#     `cache_control` 参数，自定义 cache_key 和过期时间，可以更主动地控制
#     缓存粒度和生命周期，不依赖"最近是否有相同前缀的请求"这种命中概率。
#
# PROMPT_CACHE_MODE 三选一：
#   - "implicit"（默认）：什么都不用做，天然享受隐式缓存，无需 extra_body。
#   - "explicit"：额外传 cache_control，把 SYSTEM_PROMPT 显式标记为长期缓存。
#   - "off"：调试/对比用，显式关闭（目前 SDK 层面即"不传 cache_control"，
#            隐式缓存本身无法从客户端关闭，这里仅用于关闭显式缓存分支）。
# ------------------------------------------------------------------
PROMPT_CACHE_MODE = os.environ.get("PROMPT_CACHE_MODE", "implicit").strip().lower()
PROMPT_CACHE_KEY = os.environ.get("PROMPT_CACHE_KEY", "business-trip-agent-system-prompt-v1")
PROMPT_CACHE_TTL_SECONDS = int(os.environ.get("PROMPT_CACHE_TTL_SECONDS", "600"))


# ------------------------------------------------------------------
# 备用模型自动切换（对应 eval 报告 方案 5）
#
# 只处理"额度/配额耗尽"这一类特征明确的错误，不笼统捕获所有异常——真正的
# 模型幻觉、网络抖动等问题应该照常暴露出来，不能被这里悄悄吞掉、伪装成
# "切换模型解决了"。
#
# 切换粒度：只在两次"任务"之间切换，不在同一次 run_agent_loop 内部切换。
# 一旦在某次调用里识别到额度错误，这一次任务直接判定为 infra_error 并结束，
# 不在同一个上下文里换模型继续跑（避免同一次运行里推理风格不一致，破坏这次
# 运行的可解释性）。真正的模型切换发生在下一次任务开始、读取
# `_active_model["name"]` 的时候。
# ------------------------------------------------------------------
QWEN_BACKUP_MODEL = os.environ.get("QWEN_BACKUP_MODEL", "").strip() or None

_active_model = {"name": QWEN_MODEL}

_QUOTA_ERROR_KEYWORDS = (
    "quota", "insufficient_quota", "insufficient quota",
    "arrearage", "balance", "rate limit", "429",
    "配额", "余额不足", "欠费", "额度",
)


def _is_quota_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(k.lower() in text for k in _QUOTA_ERROR_KEYWORDS)


# ------------------------------------------------------------------
# 超时错误识别（对应 eval 报告"慢请求看起来像卡死"问题的第二步）
#
# openai SDK 在请求超过 client 级别 timeout 时会抛 openai.APITimeoutError
# （是 APIConnectionError 的子类）；网关/代理层如果在自己那一层掐断连接，
# 也可能表现成别的异常类型但错误信息里带 "timeout"/"timed out" 字样，所以
# 同时用 isinstance 和关键词兜底判断，尽量不漏判。
#
# 这不是"额度耗尽"，不应该触发切换备用模型的逻辑（备用模型解决不了网络慢
# 的问题），只是网关这次响应慢/连接层面出问题——跟额度错误一样，属于
# "不是 Agent 行为问题"，所以复用同一条 infra_error 路径上报，不计入
# 确定性/语义评审统计，但不做模型切换。
# ------------------------------------------------------------------
_TIMEOUT_ERROR_KEYWORDS = ("timeout", "timed out", "read timeout", "connect timeout")


def _is_timeout_error(exc: Exception) -> bool:
    if isinstance(exc, APITimeoutError):
        return True
    text = str(exc).lower()
    return any(k in text for k in _TIMEOUT_ERROR_KEYWORDS)


def _build_extra_body_for_cache() -> dict | None:
    """按 PROMPT_CACHE_MODE 构造 extra_body，只在 explicit 模式下真正传参数。"""
    if PROMPT_CACHE_MODE != "explicit":
        return None
    return {
        "cache_control": {
            "type": "ephemeral",
            "cache_key": PROMPT_CACHE_KEY,
            "cache_age": PROMPT_CACHE_TTL_SECONDS,
        }
    }


# ------------------------------------------------------------------
# 审批分类 / 报告核验 —— 与 harness.py 共用的纯函数
#
# 实现挪到了 approval_utils.py（见顶部 import），main.py 和 harness.py 都从
# 那里导入同一份 classify_approval_purpose / find_fabricated_ids，避免两边
# 各写一份、以后改一边忘了改另一边。这里不再重复定义。
#
# 用途提醒：
# - classify_approval_purpose 用于运行时的安全护栏（见下方 tool_book_hotel）
#   和 generate_final_report 里的报告核验依据；
# - find_fabricated_ids 用于 generate_final_report 核验最终报告有没有编造
#   不存在的订单号/确认号。
#
# 这不违反"失败之后怎么办永远由模型现场推理决定"的设计原则——这里划的是
# "业务判断"和"安全护栏"两类不同的代码分支：前者（该选哪个酒店、失败了
# 怎么重试）永远交给模型；后者（未经审批不能真的把超支的订单点下去）是绝对
# 不能发生的安全底线，不该赌模型一定会听 prompt 的话。
# ------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------
# ============ Mock 数据 ============
#
# 这是唯一为了测试而改动的地方：改这里面的值、不改下面任何函数逻辑，重新运行，
# 观察模型的推理和下一步决策是否合理地随之改变——这是验证"决策来自现场推理，
# 而不是代码里预埋的分支"的最直接方式。
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
        "flights": [
            {"flight_id": "MU5101", "departure_time": "07:30", "arrival_time": "09:30",
             "price": 1380, "search_status": "available", "booking_status": "sold_out"},
            {"flight_id": "HO1125", "departure_time": "08:30", "arrival_time": "10:30",
             "price": 1280, "search_status": "available", "booking_status": "available"},
            {"flight_id": "CA1835", "departure_time": "09:00", "arrival_time": "11:00",
             "price": 980, "search_status": "available", "booking_status": "available"},
        ],
    },
    "search_hotel": {
        # 刻意设计的失败点 2：陆家嘴附近酒店都超预算，但有一个稍远的选项在预算内
        "status": "success",
        "policy_limit": 800,
        "summary": "客户公司位于陆家嘴核心区，近距离酒店均超出差旅政策预算上限，但存在预算内的稍远选项",
        "options": [
            {"hotel_id": "HTL-A", "hotel_name": "陆家嘴中心大酒店", "distance_to_customer_km": 0.3,
             "price_per_night": 950, "within_policy": False},
            {"hotel_id": "HTL-B", "hotel_name": "浦东国际会展酒店", "distance_to_customer_km": 0.5,
             "price_per_night": 920, "within_policy": False},
            {"hotel_id": "HTL-C", "hotel_name": "银城中路商务酒店", "distance_to_customer_km": 1.8,
             "price_per_night": 780, "within_policy": True},
        ],
    },
}


# ------------------------------------------------------------------
# ============ 工具函数 ============
#
# 无状态的查询类工具直接定义为模块级函数即可；"预订"类工具需要记住本次任务
# 已经订过什么（用来生成递增的订单号），这部分状态不能挂在模块全局变量上——
# 否则两个并发任务会互相污染彼此的预订记录。见下方 _make_booking_tools()。
# ------------------------------------------------------------------
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


def tool_search_flight(date: str = "", destination: str = "") -> dict:
    public_fields = ["flight_id", "departure_time", "arrival_time", "price", "search_status"]
    flights = MOCK_DATA["search_flight"]["flights"]
    return {
        "status": "success",
        "queried_date": date,
        "destination": destination,
        "flights": [{k: f[k] for k in public_fields} for f in flights],
    }


def tool_search_hotel(date: str = "", near: str = "") -> dict:
    result = deepcopy(MOCK_DATA["search_hotel"])  # options 是嵌套列表，浅拷贝不够安全
    result["queried_date"] = date
    result["queried_near"] = near
    return result


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


def _make_booking_tools(approval_state: dict) -> dict:
    """
    每次任务开始时调用一次，返回一套只属于本次任务的 book_flight / book_hotel
    工具函数。已预订记录保存在闭包变量里，不写模块全局，这样并发跑多个任务时
    彼此的预订状态不会互相污染。

    approval_state 是调用方（run_agent_loop）维护的、和 booked_flights/
    booked_hotels 同级的会话状态（例如 {"hotel_over_budget_approved": bool}），
    这里通过闭包捕获，用于 tool_book_hotel 内部的安全护栏检查（见下方）。

    幂等性说明（对应 eval 报告问题 1 / 方案 1）：
    已预订记录现在是 {id: 完整结果 dict} 而不是单纯的 list——不只是用来生成
    递增订单号，还用于在重复调用时直接原样返回第一次的成功结果（同一个
    order_id/confirmation_id），而不是重新走一遍下单逻辑。这样模型出于确认
    心理重复调用一次（例如审批被拒后重新梳理方案，顺手再确认一次已完成项）
    时不会被一个"陌生的失败"误导，也彻底堵住了"同一个 id 被计成两次成功预订"
    的资损风险——两次调用返回的是完全相同的订单号，而不是两个不同的订单号。
    """
    booked_flights: dict[str, dict] = {}
    booked_hotels: dict[str, dict] = {}

    def tool_book_flight(flight_id: str = "") -> dict:
        if flight_id in booked_flights:
            prev = dict(booked_flights[flight_id])
            prev["duplicate_call"] = True
            prev["message"] = (
                f"航班 {flight_id} 此前已预订成功，订单号 {prev['order_id']}（本次为重复调用，"
                f"未产生新订单，无需重复处理）"
            )
            return prev

        flights = MOCK_DATA["search_flight"]["flights"]
        flight = next((f for f in flights if f["flight_id"] == flight_id), None)

        if flight is None:
            return {"status": "not_found", "reason": f"未找到航班 {flight_id}"}

        if flight["booking_status"] != "available":
            # 搜索时可见，下单时才发现被锁/售罄——这是环境状态变化，不是代码里
            # 写死的"选这个就失败"分支；下一步怎么办完全交给模型现场判断。
            return {
                "status": "failed",
                "reason": f"提交订单时航班 {flight_id}（{flight['departure_time']} 出发）已被其他用户锁定/售罄",
                "flight_id": flight_id,
            }

        order_id = f"FLT-{1000 + len(booked_flights)}"
        result = {
            "status": "success",
            "flight_id": flight_id,
            "departure_time": flight["departure_time"],
            "arrival_time": flight["arrival_time"],
            "price": flight["price"],
            "order_id": order_id,
            "message": f"航班 {flight_id}（{flight['departure_time']} 出发）预订成功，订单号 {order_id}",
        }
        booked_flights[flight_id] = result
        return dict(result)

    def tool_book_hotel(hotel_id: str = "") -> dict:
        if hotel_id in booked_hotels:
            prev = dict(booked_hotels[hotel_id])
            prev["duplicate_call"] = True
            prev["message"] = (
                f"{prev['hotel_name']} 此前已预订成功，确认号 {prev['confirmation_id']}"
                "（本次为重复调用，未产生新订单，无需重复处理）"
            )
            return prev

        options = MOCK_DATA["search_hotel"]["options"]
        hotel = next((h for h in options if h["hotel_id"] == hotel_id), None)

        if hotel is None:
            return {"status": "not_found", "reason": f"未找到酒店 {hotel_id}"}

        # ------------------------------------------------------------
        # 安全护栏（代码层面的硬约束，不依赖模型是否听话）：
        # 价格超出政策上限、且本次任务没有一次已批准的 hotel_over_budget
        # 审批记录时，直接拒绝下单，而不是执行。这条检查原本只存在于
        # harness.py 的事后评测（check_global_invariants）里，现在同时也
        # 在生产运行时生效——评测那条不变量现在验证的是"运行时硬约束确实
        # 生效了"，而不是"模型确实听话了"，两层防御分工更清楚。
        #
        # 这不是一个"该选哪个酒店"这类业务决策分支，而是"未经审批不能真的
        # 把超支订单点下去"这条绝对底线，所以放在代码里、不赌模型会自觉
        # 遵守 system prompt 的规则。
        # ------------------------------------------------------------
        policy_limit = MOCK_DATA["get_travel_policy"]["hotel_budget_per_night"]
        if hotel["price_per_night"] > policy_limit and not approval_state.get("hotel_over_budget_approved"):
            return {
                "status": "rejected",
                "reason": (
                    f"酒店 {hotel_id} 价格 {hotel['price_per_night']} 超出政策上限 {policy_limit}，"
                    "且未获得已批准的审批，已被系统拦截。请先调用 request_human_approval 获得批准后再重试。"
                ),
            }

        confirmation_id = f"HTL-{2000 + len(booked_hotels)}"
        result = {
            "status": "success",
            "hotel_id": hotel_id,
            "hotel_name": hotel["hotel_name"],
            "price_per_night": hotel["price_per_night"],
            "confirmation_id": confirmation_id,
            "message": f'{hotel["hotel_name"]} 预订成功，确认号 {confirmation_id}',
        }
        booked_hotels[hotel_id] = result
        return dict(result)

    return {
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
            "description": (
                "搜索指定日期前往目的地的可选航班，返回完整候选列表（航班编号、出发/到达时间、"
                "价格、查询时的余票状态）。该工具只负责提供候选信息，不会替你选择或预订航班。"
                "你需要结合客户会面时间、出行安排和差旅预算自行比较后，再调用 book_flight。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "出发日期，例如 '下周二'"},
                    "destination": {"type": "string", "description": "到达城市，例如 '上海'"},
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_hotel",
            "description": (
                "查询客户公司附近的酒店候选列表，每个候选都带 hotel_id、价格和 within_policy"
                "（是否在差旅政策预算内）。预订时必须使用候选中真实存在的 hotel_id。"
            ),
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
            "description": (
                "预订 search_flight 返回的某个具体航班，必须传入搜索结果中真实存在的 flight_id。"
                "搜索结果只代表查询时的状态，提交订单时仍可能因实时库存变化而预订失败——遇到失败后，"
                "应根据返回原因从之前的候选中重新选择，或重新搜索，不要放弃。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "flight_id": {"type": "string", "description": "要预订的航班编号，例如 'MU5101'"},
                },
                "required": ["flight_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_hotel",
            "description": (
                "预订某个具体酒店，必须传入 search_hotel 返回的候选中真实存在的 hotel_id。"
                "如果该酒店超出差旅政策的每晚预算上限（候选中 within_policy 为 false），必须先调用"
                " request_human_approval 获得批准之后才能调用这个工具，不能自己直接下单。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hotel_id": {"type": "string", "description": "要预订的酒店编号，例如 'HTL-C'"},
                },
                "required": ["hotel_id"],
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
7. 航班安排需要遵循"先搜索候选、比较约束、再预订"的流程：不要凭空猜测航班时间，
   必须使用 search_flight 返回的真实 flight_id 调用 book_flight。预订失败不代表
   任务失败，从之前的候选中重新选择，或重新搜索，都是合理的下一步。
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


def _call_with_tools(messages: list[dict], model: str | None = None):
    """
    同步阻塞的网络请求，扔到线程池里跑。

    model 参数默认取当前"激活模型"（正常情况下等于 QWEN_MODEL；如果上一次
    任务触发过额度耗尽且配置了备用模型，这里会自动变成 QWEN_BACKUP_MODEL，
    见 _active_model / 方案 5）。

    messages 里 system prompt 固定在最前面、本轮新增内容追加在最后，这个
    结构本身就是隐式缓存需要的"稳定前缀"，不需要额外处理；PROMPT_CACHE_MODE
    为 "explicit" 时才会额外传 cache_control（方案 4 长期）。
    """
    kwargs = dict(
        model=model or _active_model["name"],
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
    )
    extra_body = _build_extra_body_for_cache()
    if extra_body:
        kwargs["extra_body"] = extra_body

    response = client.chat.completions.create(**kwargs)

    # 记录 usage（包含缓存命中情况），供前端/日志观测 token 消耗结构——
    # 这不是必须的业务逻辑，只用于验证方案 4 是否生效，取不到就跳过。
    usage_info = None
    usage = getattr(response, "usage", None)
    if usage is not None:
        cached_tokens = None
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached_tokens = getattr(details, "cached_tokens", None)
        usage_info = {
            "model": kwargs["model"],
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
            "cached_tokens": cached_tokens,
        }

    return response.choices[0].message, usage_info


MAX_TURNS = 16  # 只统计"真正调用了模型"的轮次，不包含等待用户回复/等待审批的时间


# ------------------------------------------------------------------
# Checklist（Verifier）：任务是否完成的判断标准。
#
# 这里只核对客观结果（工具是不是返回了 success），不涉及"接下来该做什么"的
# 决策——那部分完全由模型的推理文字决定。
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
    五项 Checklist 全部完成后调用一次模型（不带 tools），让它基于这次任务真实
    发生的工具调用历史和审批记录，生成执行层/决策层二分报告。

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
        model=_active_model["name"],
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

    # ------------------------------------------------------------------
    # 报告核验（把 harness.py 里 check_final_report_groundedness 的核心逻辑
    # 搬进生产路径）：execution_layer 理应"只写有客观依据的事实"，典型就是
    # 订单号/确认号/event_id/message_id。这里从本次任务真实的 action_log 里
    # 提取所有真实产生过的编号，跟 execution_layer 文本里提到的编号做差集，
    # 凡是文本里提到、但从未真实出现过的编号，都视为报告编造了不存在的事实。
    #
    # 发现幻觉编号后不做"自动重新生成"这种更复杂的重试逻辑，而是直接在
    # decision_layer 里标红降级展示——这符合项目一贯的"暴露问题而不是掩盖
    # 问题"的风格（awaiting_reply / infra_error 都是这个思路）。
    # ------------------------------------------------------------------
    real_ids = {
        item["result"].get(field)
        for item in action_log
        for field in ("order_id", "confirmation_id", "event_id", "message_id")
        if item["result"].get(field)
    }
    fabricated = find_fabricated_ids(real_ids, "\n".join(execution_layer))
    if fabricated:
        decision_layer.append(
            f"⚠️ 报告草稿中提到了未实际产生过的编号 {sorted(fabricated)}，"
            "已自动标记，请人工复核（可能是模型幻觉）。"
        )

    return {"execution_layer": execution_layer, "decision_layer": decision_layer}


async def run_agent_loop(
    websocket: WebSocket,
    goal: str,
    approval_queue: "asyncio.Queue[dict]",
    reply_queue: "asyncio.Queue[str]",
):
    """
    核心循环：调用模型 → 它说要用哪个工具 → 执行工具 → 把结果丢回去 → 重复，
    直到它不再调用工具**并且** Checklist 全部满足（代表任务真正完成）。

    这个循环里没有任何 if "航班满员" / "酒店超预算" then ... 这样的业务分支，
    所有"失败之后怎么办"都是模型自己在推理文字里现场决定的，代码只负责机械地
    执行它选的工具，以及在旁边核对客观结果、更新 Checklist。

    人工审批：如果模型调用的是 `request_human_approval`，代码不会立即返回结果，
    而是把请求推给前端、真正暂停在 `await approval_queue.get()` 上，直到用户在
    网页上点了"批准/拒绝"，这个选择（连同可选的一句补充说明）才会作为下一个
    Observation 喂回模型。

    `action_log` 和 `approval_records` 记录本次任务真实发生的每一次工具调用和
    每一次审批，任务结束时用来生成执行层/决策层报告。

    会话延续：模型"这一轮没有调用工具"不会被无条件当成"任务结束"。只有同时
    满足"没调用工具" + "Checklist 全部完成"，才真正 done、销毁上下文、return。
    如果没调用工具但 Checklist 还没满，说明模型是在用文字向用户提问、等待回复
    （最典型的触发点是审批被拒绝之后），此时代码会把这一状态推给前端，真正
    暂停在 `await reply_queue.get()` 上，等用户在网页上继续说话，再把这句话
    追加进**同一份** `messages`、`continue` 回到循环顶部——不会新建 messages，
    不会丢失之前查过的客户信息、已订的航班酒店记录。approval_queue 和
    reply_queue 是同一种"挂起等用户"机制，只是一个等的是按钮点击、一个等的
    是一段文字。
    """
    if client is None:
        await send(
            websocket, "error",
            text="⚠️ 还没配置 QWEN_API_KEY，请在项目根目录的 .env 文件里填好之后重启程序。",
        )
        return

    # approval_state：和 booked_flights/booked_hotels 同级的会话状态，记录
    # "本次任务里是否已经有一次已批准的酒店超预算审批"，供 tool_book_hotel
    # 内部的安全护栏读取。这里不做"用完就重置"：一次任务里只要批准过一次
    # 超预算，后续同一任务内的重复下单尝试（幂等场景）也应该被认为已获授权，
    # 不需要每次都重新走审批。
    approval_state = {"hotel_over_budget_approved": False}

    # 每个任务拥有独立的工具函数（尤其是 book_flight / book_hotel 的预订记录），
    # 避免并发任务之间互相污染状态。
    tool_functions = _make_booking_tools(approval_state)

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

        # ---- 超时的业务层重试：只在 _is_timeout_error 为真时重试，配额
        # 错误不重试、直接走下面的 infra_error+切模型逻辑。重试用尽后
        # exc 会是最后一次失败的异常，落到下面同一套错误处理分支里，
        # 依然正确降级为 infra_error，不会掩盖真正持续的超时问题。----
        timeout_retries = 0
        exc: Exception | None = None
        message = usage_info = None
        while True:
            try:
                message, usage_info = await asyncio.to_thread(_call_with_tools, messages)
                exc = None
                break
            except Exception as call_exc:  # noqa: BLE001
                exc = call_exc
                if _is_timeout_error(exc) and timeout_retries < QWEN_TIMEOUT_MAX_RETRIES:
                    timeout_retries += 1
                    await send(
                        websocket, "retrying",
                        text=f"⚠️ 模型调用超时，正在进行第 {timeout_retries} 次业务层重试……",
                        attempt=timeout_retries,
                    )
                    await asyncio.sleep(QWEN_TIMEOUT_RETRY_BACKOFF_SECONDS * timeout_retries)
                    continue
                break

        if exc is not None:
            if _is_quota_error(exc):
                # 只吃"额度耗尽"这一类特征明确的错误；其他异常（真正的模型幻觉、
                # 网络抖动等）照常原样报错，不会被这里悄悄吞掉。
                previous_model = _active_model["name"]
                if QWEN_BACKUP_MODEL and _active_model["name"] != QWEN_BACKUP_MODEL:
                    # 主模型额度耗尽、且尚未切换过 —— 切到备用模型。
                    # 切换粒度控制在"任务与任务之间"：这一次任务直接结束，标记为
                    # infra_error；下一次任务开始时 _call_with_tools 会自动读取
                    # 切换后的 _active_model，不在本次运行内部换模型继续跑。
                    # 备用模型的额度不会自己恢复，所以这里是"一次性、永久"的切换，
                    # 之后所有任务都会持续使用备用模型，这是预期行为。
                    _active_model["name"] = QWEN_BACKUP_MODEL
                    await send(
                        websocket, "infra_error",
                        text=(
                            f"⚠️ 模型 {previous_model} 额度耗尽（{exc}），已自动切换到备用模型 "
                            f"{QWEN_BACKUP_MODEL}。本次任务因基础设施问题中止，请重新发起任务，"
                            f"新任务会使用备用模型继续执行。"
                        ),
                        model_used=previous_model,
                        switched_to=QWEN_BACKUP_MODEL,
                    )
                else:
                    # 已经在用备用模型（或压根没配置备用模型），额度问题依旧是
                    # 基础设施问题，不是 Agent 行为问题——必须仍然标记为
                    # infra_error，不能落到下面的 else 分支被当成普通 error，
                    # 否则会被 harness 误计入确定性断言失败，污染报告。
                    reason = (
                        "备用模型额度也已耗尽，且没有更多可切换的模型"
                        if QWEN_BACKUP_MODEL else "未配置备用模型（QWEN_BACKUP_MODEL）"
                    )
                    await send(
                        websocket, "infra_error",
                        text=(
                            f"⚠️ 模型 {previous_model} 额度耗尽（{exc}），{reason}，"
                            f"本次任务因基础设施问题中止。"
                        ),
                        model_used=previous_model,
                        switched_to=None,
                    )
            elif _is_timeout_error(exc):
                # 网关这次响应慢/连接层面出问题，不是 Agent 推理本身的问题，
                # 也不是额度问题（所以不触发备用模型切换）。跟额度错误一样
                # 走 infra_error：这次任务直接中止，前端能明确看到"这是基础
                # 设施问题"而不是误以为死循环，也不会污染 harness 里的确定性/
                # 语义统计。model_used 仍然是当前激活模型（没有发生切换）。
                await send(
                    websocket, "infra_error",
                    text=(
                        f"⚠️ 模型调用超时（{exc}），已重试 {timeout_retries} 次业务层重试仍未成功，"
                        f"网关响应过慢或连接异常，本次任务因基础设施问题中止，请重新发起任务重试。"
                    ),
                    model_used=_active_model["name"],
                    switched_to=None,
                )
            else:
                await send(websocket, "error", text=f"⚠️ 调用模型失败：{exc}")
            return
        model_calls += 1
        if usage_info:
            await send(websocket, "usage", **usage_info)

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
                # model_used：记录本次任务实际完成时使用的 Agent 侧模型，避免日后
                # 回看某些 case 表现波动时，分不清是模型本身不稳定还是中途切换了
                # 备用模型导致的（方案 5）。
                await send(websocket, "done", model_used=_active_model["name"])
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
                # 真正暂停，等用户在网页上点按钮。代码不判断"该不该批准"，那是
                # 用户的决定；代码也不判断"要不要发起审批"，那是模型刚才自己
                # 决定的——代码只是搭一座桥，把两边接起来。
                reason = args.get("reason", "")
                proposed_action = args.get("proposed_action", "")
                await send(websocket, "approval_request", reason=reason, proposed_action=proposed_action)
                approval_payload = await approval_queue.get()
                approved = bool(approval_payload.get("approved"))
                user_note = (approval_payload.get("user_note") or "").strip()

                if approved:
                    result = {"status": "approved", "message": f"用户已批准：{proposed_action}"}
                    if classify_approval_purpose(reason, proposed_action) == "hotel_over_budget":
                        # 记录到会话状态，供 tool_book_hotel 的安全护栏读取。
                        # 不做"用完即重置"，理由见 approval_state 初始化处的注释。
                        approval_state["hotel_over_budget_approved"] = True
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
                func = tool_functions.get(name)
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
             f"这种情况本身就是一类值得记录的 Bad Case（重复失败/死循环）。",
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
    - approval_queue：等审批卡片的"批准/拒绝"
    - reply_queue：等用户在"模型反问、任务未结束"时继续说话
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
                # 任务还在进行中（模型在等回复）时，用户发来的后续消息走这里，
                # 续进同一个 run_agent_loop，而不是 start_task。
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
