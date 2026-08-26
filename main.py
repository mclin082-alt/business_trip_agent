"""
Travel agent backend: FastAPI + WebSocket + Qwen function-calling loop.

Phase 2: run_agent_loop is now reentrant. It never blocks on a live
asyncio.Queue waiting for human input. At every pause point (awaiting
approval, awaiting a free-text reply) it persists the Session to
session_store and returns. Resuming happens by loading the Session back
from session_store and re-entering run_agent_loop with an explicit
`resume_input`, possibly in a brand new process / websocket connection.
"""

import asyncio
import dataclasses
import inspect
import json
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from approval_utils import (
    check_hotel_approval_with_ref,
    check_policy_boundary_with_ref,
    classify_approval_purpose,
    find_fabricated_ids,
    is_valid_approver,
    is_repeated_readonly_call,
)
import policy_store
import session_store
import memory_store
from multi_agent import (
    BOOKING_AGENT,
    DELEGATION_TOOL_AGENTS,
    ORCHESTRATOR_AGENT,
    POLICY_AGENT,
    booking_agent_messages,
    check_booking_execution,
    create_booking_proposal,
    create_policy_review,
    current_search_sequence,
    policy_agent_messages,
)
from context_manager import build_model_context, estimate_context_tokens, get_context_strategy
from integrations.calendar_adapter import get_my_calendar as get_calendar_from_adapter
from skills import (
    DEFAULT_ROLE,
    SKILL_BY_NAME,
    build_tools_schema,
    wrap_with_permission_check,
)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI()
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# Qwen client setup
# ---------------------------------------------------------------------------
QWEN_API_KEY = os.environ.get("QWEN_API_KEY", "")
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen-plus")
QWEN_BACKUP_MODEL = os.environ.get("QWEN_BACKUP_MODEL", "")
QWEN_TIMEOUT_MAX_RETRIES = int(os.environ.get("QWEN_TIMEOUT_MAX_RETRIES", "2"))
QWEN_TIMEOUT_RETRY_BACKOFF_SECONDS = float(os.environ.get("QWEN_TIMEOUT_RETRY_BACKOFF_SECONDS", "3"))
PROMPT_CACHE_MODE = os.environ.get("PROMPT_CACHE_MODE", "off")  # "off" | "explicit"

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

client = None
if OpenAI is not None and QWEN_API_KEY:
    client = OpenAI(
        api_key=QWEN_API_KEY,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )

_active_model: dict[str, str] = {"name": QWEN_MODEL}


def _is_quota_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("insufficient", "quota", "balance", "limit", "exceeded", "rate_limit"))


def _is_timeout_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("timeout", "timed out", "connection", "read timed out"))


def _build_extra_body_for_cache() -> dict | None:
    if PROMPT_CACHE_MODE != "explicit":
        return None
    return {
        "chat_cache": {
            "system": {"cache_control": {"type": "ephemeral"}},
            "tools": {"cache_control": {"type": "ephemeral"}},
        }
    }


def _safe_json_loads(raw: str | None) -> dict:
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Session: the full state needed to pause and resume run_agent_loop.
#
# NOTE on module layout: Session lives here (in main.py) on purpose.
# session_store.py only ever imports it lazily (inside load_session, under
# TYPE_CHECKING for hints) to avoid a circular import, since main.py imports
# session_store at module load time. Earlier Phase 1 notes referenced
# `from session_store import Session` from main.py, which is backwards and
# would form an import cycle; fixed here.
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class Session:
    messages: list[dict]
    checklist: dict
    approval_state: dict
    action_log: list[dict]
    approval_records: list[dict]
    booked_flights: dict
    booked_hotels: dict
    lifecycle_state: str
    model_calls: int
    seen_readonly_calls: dict
    goal: str = ""
    # Phase 2 additions: state needed to resume mid tool-call-batch after an
    # approval pause (parallel_tool_calls=False mostly keeps batches at size
    # 1, but the multi_tool_call_detected defense path can still produce >1).
    pending_approval: dict | None = None
    pending_tool_calls: list = dataclasses.field(default_factory=list)
    # Skill-level permission role (see skills.py). Defaults to the ordinary
    # staff role so existing behavior/eval cases are unaffected; a caller
    # can pass a different role (e.g. "intern") to exercise the
    # permission_denied path against write/side-effect Skills.
    role: str = DEFAULT_ROLE
    # Non-sensitive connector context only. OAuth/access tokens must remain in
    # the MCP server or its secret store, never in sessions.db.
    actor_id: str = "demo_employee"
    connector_context: dict = dataclasses.field(default_factory=dict)
    # Bounded snapshots of soft preference data; guardrails never read them.
    resolved_subjects: dict = dataclasses.field(default_factory=dict)
    recalled_memories: list[dict] = dataclasses.field(default_factory=list)
    memory_initialized_subjects: list[str] = dataclasses.field(default_factory=list)
    # Governed multi-agent state. Specialists never own the user conversation
    # or execute side effects; they emit typed proposals/reviews persisted here.
    multi_agent_enabled: bool = True
    subagent_model_calls: int = 0
    delegation_records: list[dict] = dataclasses.field(default_factory=list)
    booking_proposals: dict[str, dict] = dataclasses.field(default_factory=dict)
    policy_reviews: dict[str, dict] = dataclasses.field(default_factory=dict)
    # Increment when the persisted Session shape needs a migration.
    schema_version: int = session_store.CURRENT_SESSION_SCHEMA_VERSION

    @classmethod
    def new(
        cls,
        goal: str,
        system_prompt: str,
        role: str = DEFAULT_ROLE,
        actor_id: str = "demo_employee",
        resolved_subjects: dict | None = None,
        multi_agent_enabled: bool | None = None,
    ) -> "Session":
        subjects = dict(resolved_subjects or {})
        subjects["actor"] = actor_id
        return cls(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": goal},
            ],
            checklist=_new_checklist_state(),
            approval_state={},
            action_log=[],
            approval_records=[],
            booked_flights={},
            booked_hotels={},
            lifecycle_state="RUNNING",
            model_calls=0,
            seen_readonly_calls={},
            goal=goal,
            pending_approval=None,
            pending_tool_calls=[],
            role=role,
            actor_id=actor_id,
            connector_context={"calendar_provider": os.environ.get("CALENDAR_PROVIDER", "mock")},
            resolved_subjects=subjects,
            multi_agent_enabled=(
                os.environ.get("MULTI_AGENT_ENABLED", "1").strip().lower()
                not in {"0", "false", "off", "no"}
                if multi_agent_enabled is None
                else multi_agent_enabled
            ),
        )


# ---------------------------------------------------------------------------
# AgentDefinition: capability bundle (system prompt + tools schema + tool factory)
# ---------------------------------------------------------------------------
class AgentDefinition:
    def __init__(self, system_prompt: str, tools_schema: list[dict], make_tool_functions):
        self.system_prompt = system_prompt
        self.tools_schema = tools_schema
        self.make_tool_functions = make_tool_functions


# ---------------------------------------------------------------------------
# Mock data
# ---------------------------------------------------------------------------
MOCK_DATA = {
    # NOTE: hotel/flight budget policy no longer lives here — it's sourced
    # from policy_store.py / policy/travel_policy_v1.json (single source of
    # truth shared by runtime enforcement and eval invariant checks).
    "get_customer_info": {
        "status": "success",
        "customer_id": "customer_bluebay_tech",
        "company_id": "company_bluebay_tech",
        "company": "上海蓝湾科技有限公司",
        "address": "上海市浦东新区陆家嘴环路 1088 号",
        "contact": {"name": "王明", "title": "技术总监", "phone": "138-0000-1234"},
        "note": "客户公司位于陆家嘴核心区，是本次拜访的目的地。",
    },
    "get_my_calendar": {
        "status": "success",
        "date": "下周二",
        "events": [
            {"time": "09:00-10:00", "title": "团队周会"},
            {"time": "14:00-15:00", "title": "产品评审"},
        ],
        "free_slots": ["10:00-12:00", "15:00-18:00"],
    },
    "get_customer_calendar": {
        "status": "success",
        "date": "下周二",
        "events": [
            {"time": "10:00-11:30", "title": "客户内部会议"},
        ],
        "free_slots": ["09:00-10:00", "11:30-18:00"],
    },
    "search_flight": {
        "status": "success",
        "flights": [
            {"flight_id": "MU5101", "departure_time": "08:00", "arrival_time": "10:20", "price": 1280, "booking_status": "available"},
            {"flight_id": "CA1234", "departure_time": "10:30", "arrival_time": "12:50", "price": 1580, "booking_status": "available"},
            {"flight_id": "FM9301", "departure_time": "14:00", "arrival_time": "16:30", "price": 980, "booking_status": "available"},
        ],
    },
    "search_hotel": {
        "status": "success",
        "options": [
            {"hotel_id": "HTL-A", "hotel_name": "浦东商务酒店", "price_per_night": 650, "within_policy": True},
            {"hotel_id": "HTL-B", "hotel_name": "陆家嘴精品酒店", "price_per_night": 780, "within_policy": True},
            {"hotel_id": "HTL-C", "hotel_name": "浦东香格里拉", "price_per_night": 1200, "within_policy": False},
        ],
    },
    "sync_calendar": {"status": "success", "event_id": "EVT-001"},
    "send_confirmation_email": {"status": "success", "message_id": "MSG-001"},
}


def _usage_payload(response, model: str) -> dict:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"model": model}
    cached_tokens = None
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached_tokens = getattr(details, "cached_tokens", None)
    return {
        "model": model,
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
        "cached_tokens": cached_tokens,
    }


def _call_specialist(messages: list[dict]) -> tuple[str, dict]:
    """Run one bounded specialist turn without exposing side-effect tools."""
    model = _active_model["name"]
    response = client.chat.completions.create(model=model, messages=messages)
    return response.choices[0].message.content or "", _usage_payload(response, model)


def _latest_successful_result(action_log: list[dict], name: str) -> dict | None:
    for item in reversed(action_log):
        result = item.get("result") or {}
        if item.get("name") == name and result.get("status") == "success":
            return result
    return None


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
def _make_booking_tools(session: Session) -> dict[str, Any]:
    def tool_get_customer_info(company: str = "") -> dict:
        return dict(MOCK_DATA["get_customer_info"])

    async def tool_get_my_calendar(date: str = "") -> dict:
        return await get_calendar_from_adapter(
            date=date,
            actor_id=session.actor_id,
            mock_result=MOCK_DATA["get_my_calendar"],
        )

    def tool_get_customer_calendar(date: str = "") -> dict:
        return dict(MOCK_DATA["get_customer_calendar"])

    def tool_get_travel_policy() -> dict:
        flight_limit, _ = policy_store.get_rule_limit("flight_budget")
        hotel_limit, _ = policy_store.get_rule_limit("hotel_budget")
        flight_ref = policy_store.get_policy_ref("flight_budget")
        hotel_ref = policy_store.get_policy_ref("hotel_budget")
        return {
            "status": "success",
            "flight_budget": flight_limit,
            "hotel_budget_per_night": hotel_limit,
            "notes": hotel_ref["rule_text"] if hotel_ref else "",
            "policy_refs": {
                "flight_budget": flight_ref,
                "hotel_budget": hotel_ref,
            },
        }

    def tool_search_flight(date: str = "", destination: str = "") -> dict:
        # NOTE: a flight may carry an internal-only "actual_status" field used to
        # simulate a real-time inventory race condition (search shows it as
        # available, but the booking submission itself fails). That field must
        # never leak into what the model sees at search time, or the "surprise"
        # failure at booking time wouldn't be a surprise at all.
        data = dict(MOCK_DATA["search_flight"])
        data["flights"] = [
            {k: v for k, v in f.items() if k != "actual_status"}
            for f in data["flights"]
        ]
        return data

    def tool_search_hotel(date: str = "", near: str = "") -> dict:
        return dict(MOCK_DATA["search_hotel"])

    async def tool_delegate_booking_plan(
        booking_type: str = "",
        objective: str = "",
        constraints: str = "",
    ) -> dict:
        search_name = {
            "flight": "search_flight",
            "hotel": "search_hotel",
        }.get(booking_type)
        if search_name is None:
            return {
                "status": "invalid_request",
                "reason": "booking_type 必须是 flight 或 hotel。",
            }
        search_result = _latest_successful_result(session.action_log, search_name)
        if search_result is None:
            return {
                "status": "missing_context",
                "reason": f"请先调用 {search_name} 获取候选，再委派 Booking Subagent 比较。",
            }
        candidates = search_result.get("flights" if booking_type == "flight" else "options", [])
        if not candidates:
            return {"status": "no_candidates", "reason": f"{search_name} 没有返回可比较候选。"}

        delegation_id = f"DLG-{uuid.uuid4().hex[:10]}"
        proposal_id = f"PRP-{uuid.uuid4().hex[:10]}"
        record = {
            "delegation_id": delegation_id,
            "from_agent": ORCHESTRATOR_AGENT,
            "to_agent": BOOKING_AGENT,
            "objective": objective or f"比较并推荐 {booking_type} 候选",
            "allowed_capabilities": ["compare_candidates"],
            "status": "running",
        }
        session.delegation_records.append(record)
        supporting_context = {
            name: result
            for name in (
                "get_customer_info",
                "get_my_calendar",
                "get_customer_calendar",
                "get_travel_policy",
            )
            if (result := _latest_successful_result(session.action_log, name)) is not None
        }
        messages = booking_agent_messages(
            goal=session.goal,
            booking_type=booking_type,
            candidates=candidates,
            objective=objective,
            constraints=constraints,
            supporting_context=supporting_context,
        )
        try:
            session.subagent_model_calls += 1
            raw_output, usage = await asyncio.to_thread(_call_specialist, messages)
            proposal = create_booking_proposal(
                raw_output=raw_output,
                booking_type=booking_type,
                candidates=candidates,
                objective=objective,
                constraints=constraints,
                delegation_id=delegation_id,
                proposal_id=proposal_id,
                source_search_seq=current_search_sequence(session.action_log, booking_type),
                model_usage=usage,
            )
        except Exception as exc:  # noqa: BLE001
            record.update({"status": "failed", "error": str(exc)})
            return {
                "status": "delegation_failed",
                "delegation_id": delegation_id,
                "agent": BOOKING_AGENT,
                "reason": str(exc),
            }

        session.booking_proposals[proposal_id] = proposal
        record.update({
            "status": "completed",
            "output_ref": proposal_id,
            "context_digest": proposal["context_digest"],
            "model_usage": usage,
        })
        return dict(proposal)

    async def tool_delegate_policy_review(proposal_id: str = "") -> dict:
        proposal = session.booking_proposals.get(proposal_id)
        if proposal is None:
            return {
                "status": "not_found",
                "reason": f"找不到 Booking proposal {proposal_id}。",
            }
        delegation_id = f"DLG-{uuid.uuid4().hex[:10]}"
        review_id = f"REV-{uuid.uuid4().hex[:10]}"
        record = {
            "delegation_id": delegation_id,
            "from_agent": ORCHESTRATOR_AGENT,
            "to_agent": POLICY_AGENT,
            "objective": f"复核 Booking proposal {proposal_id}",
            "allowed_capabilities": ["read_policy", "issue_verdict"],
            "status": "running",
        }
        session.delegation_records.append(record)
        try:
            session.subagent_model_calls += 1
            raw_output, usage = await asyncio.to_thread(
                _call_specialist, policy_agent_messages(proposal=proposal)
            )
            review = create_policy_review(
                raw_output=raw_output,
                proposal=proposal,
                delegation_id=delegation_id,
                review_id=review_id,
                model_usage=usage,
            )
        except Exception as exc:  # noqa: BLE001
            record.update({"status": "failed", "error": str(exc)})
            return {
                "status": "delegation_failed",
                "delegation_id": delegation_id,
                "agent": POLICY_AGENT,
                "reason": str(exc),
            }

        session.policy_reviews[proposal_id] = review
        record.update({
            "status": "completed",
            "output_ref": review_id,
            "proposal_id": proposal_id,
            "effective_decision": review["effective_decision"],
            "model_usage": usage,
        })
        return dict(review)

    def tool_sync_calendar(event_title: str = "", date: str = "", time: str = "") -> dict:
        return dict(MOCK_DATA["sync_calendar"])

    def tool_send_confirmation_email(to: str = "", summary: str = "") -> dict:
        return dict(MOCK_DATA["send_confirmation_email"])

    def tool_book_flight(flight_id: str = "", proposal_id: str = "") -> dict:
        if flight_id in session.booked_flights:
            prev = dict(session.booked_flights[flight_id])
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

        gate = None
        if session.multi_agent_enabled:
            gate = check_booking_execution(
                booking_type="flight",
                option_id=flight_id,
                proposal_id=proposal_id,
                booking_proposals=session.booking_proposals,
                policy_reviews=session.policy_reviews,
                action_log=session.action_log,
                approval_state=session.approval_state,
            )
            if not gate["allowed"]:
                return {key: value for key, value in gate.items() if key != "allowed"}

        # "actual_status" (if present) represents the real inventory state at
        # booking submission time, which can differ from what search returned
        # (booking_status) to simulate a real-time race condition. Falls back
        # to booking_status when no such override exists.
        actual_status = flight.get("actual_status", flight["booking_status"])
        if actual_status != "available":
            return {
                "status": "failed",
                "reason": f"提交订单时航班 {flight_id}（{flight['departure_time']} 出发）已被其他用户锁定/售罄",
                "flight_id": flight_id,
            }

        order_id = f"FLT-{1000 + len(session.booked_flights)}"
        result = {
            "status": "success",
            "flight_id": flight_id,
            "departure_time": flight["departure_time"],
            "arrival_time": flight["arrival_time"],
            "price": flight["price"],
            "order_id": order_id,
            "message": f"航班 {flight_id}（{flight['departure_time']} 出发）预订成功，订单号 {order_id}",
        }
        if gate:
            result.update({
                "proposal_id": gate["proposal_id"],
                "policy_review_id": gate["review_id"],
            })
        session.booked_flights[flight_id] = result
        return dict(result)

    def tool_book_hotel(hotel_id: str = "", proposal_id: str = "") -> dict:
        if hotel_id in session.booked_hotels:
            prev = dict(session.booked_hotels[hotel_id])
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

        gate = None
        if session.multi_agent_enabled:
            gate = check_booking_execution(
                booking_type="hotel",
                option_id=hotel_id,
                proposal_id=proposal_id,
                booking_proposals=session.booking_proposals,
                policy_reviews=session.policy_reviews,
                action_log=session.action_log,
                approval_state=session.approval_state,
            )
            if not gate["allowed"]:
                return {key: value for key, value in gate.items() if key != "allowed"}

        # Hard guardrail: block over-budget bookings without prior approval.
        # 判断阈值和拒绝消息里的引用都来自 policy_store，跟 get_travel_policy
        # 工具返回给模型看的文本是同一份数据源，不会出现"文档一套/代码一套"。
        within_policy, policy_ref = check_policy_boundary_with_ref(
            hotel["price_per_night"], "hotel_budget"
        )
        has_hotel_approval, approval_ref = check_hotel_approval_with_ref(session.approval_state)
        if not within_policy and not has_hotel_approval:
            ref_text = f"（依据 {policy_ref['policy_id']} / {policy_ref['version']}）" if policy_ref else ""
            return {
                "status": "rejected",
                "policy_id": policy_ref["policy_id"] if policy_ref else None,
                "reason": (
                    f"酒店 {hotel_id} 价格 {hotel['price_per_night']} 超出政策上限{ref_text}，"
                    "且未获得已批准的审批，已被系统拦截。请先调用 request_human_approval 获得批准后再重试。"
                ),
            }

        confirmation_id = f"HTL-{2000 + len(session.booked_hotels)}"
        result = {
            "status": "success",
            "hotel_id": hotel_id,
            "hotel_name": hotel["hotel_name"],
            "price_per_night": hotel["price_per_night"],
            "confirmation_id": confirmation_id,
            "message": f'{hotel["hotel_name"]} 预订成功，确认号 {confirmation_id}',
        }
        if gate:
            result.update({
                "proposal_id": gate["proposal_id"],
                "policy_review_id": gate["review_id"],
            })
        session.booked_hotels[hotel_id] = result
        return dict(result)

    return {
        "get_customer_info": tool_get_customer_info,
        "get_my_calendar": tool_get_my_calendar,
        "get_customer_calendar": tool_get_customer_calendar,
        "get_travel_policy": tool_get_travel_policy,
        "search_flight": tool_search_flight,
        "search_hotel": tool_search_hotel,
        "delegate_booking_plan": tool_delegate_booking_plan,
        "delegate_policy_review": tool_delegate_policy_review,
        "book_flight": tool_book_flight,
        "book_hotel": tool_book_hotel,
        "sync_calendar": tool_sync_calendar,
        "send_confirmation_email": tool_send_confirmation_email,
    }


# Tool JSON Schema definitions (OpenAI function calling format).
#
# NOTE: this used to be a hand-maintained list here, kept in sync with
# _make_booking_tools() purely by matching function names by string. It is
# now derived from skills.py's Skill registry, which is the single source
# of truth for schema + permissions + failure semantics. See skills.py.
TOOLS = build_tools_schema()


SYSTEM_PROMPT = """你是一个商务拜访执行 Agent，负责帮用户把一次出差安排办妥。

你是唯一面向用户并对最终任务负责的 Trip Orchestrator。你可以查客户信息（CRM）、
查双方日历、查差旅政策、搜索/预订航班、搜索/预订酒店、同步日历、发送确认邮件，
也可以把有边界的子任务委派给 Booking Subagent 和 Policy/Compliance Subagent。
工具之间没有预先写死的调用顺序，具体先查什么、后做什么，由你自己规划。

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
5. 航班或酒店搜索返回候选后，必须调用 `delegate_booking_plan` 让 Booking Subagent
   比较候选并生成 proposal；再用该 `proposal_id` 调用 `delegate_policy_review`。
   Policy review 的 `effective_decision` 是治理边界：
   - ALLOW：可以用候选 ID + proposal_id 调用对应 book 工具；
   - REQUIRE_APPROVAL：必须先调用 request_human_approval，并把同一个 proposal_id
     一并传入；批准后才能预订；
   - DENY：不能执行，应调整约束、重新搜索或重新 delegation。
   你不能自己充当 Booking 或 Policy agent，也不能用旧 proposal 预订新搜索结果。
6. 建议的大致顺序是：先了解客户信息和双方日程、查政策，再搜索并确认航班和酒店，
   最后把行程同步到日历、发确认邮件给客户——但如果你判断有更合理的顺序，可以自行
   调整，这不是硬性规定。
7. 当你确认任务已经全部完成（航班、酒店都已预订，日历已同步，确认邮件已发送），
   请输出一段总结文字说明做了什么、结果如何，并且这一轮**不要再调用任何工具**——
   不调用工具、且所有事项都已办妥，才代表任务真正结束。如果任务还没办妥而你这一
   轮没有调用工具（比如你在等用户回答一个问题），系统会把它当作"等待用户回复"，
   而不是任务结束，你之后依然可以正常调用工具继续推进。
8. 航班安排需要遵循"先搜索候选、委派比较与合规复核、再预订"的流程：不要凭空猜测航班时间，
   必须使用 search_flight 返回的真实 flight_id 调用 book_flight。预订失败不代表
   任务失败，从之前的候选中重新选择，或重新搜索，都是合理的下一步。
"""

AGENT = AgentDefinition(
    system_prompt=SYSTEM_PROMPT,
    tools_schema=TOOLS,
    make_tool_functions=_make_booking_tools,
)


class FinalReportSchema(BaseModel):
    execution_layer: list[str]
    decision_layer: list[str]


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
    payload = {}
    if "step_id" in data:
        payload["step_id"] = data["step_id"] # 把 step_id 挪到最前面
    payload["type"] = msg_type
    payload.update(data)  # run_id、step_id（值不变，位置不动）、其余字段
    await websocket.send_text(json.dumps(payload, ensure_ascii=False))


def _call_with_tools(messages: list[dict], model: str | None = None):
    """Synchronous blocking call, run in thread pool."""
    kwargs = dict(
        model=model or _active_model["name"],
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
        parallel_tool_calls=False,
    )
    extra_body = _build_extra_body_for_cache()
    if extra_body:
        kwargs["extra_body"] = extra_body

    response = client.chat.completions.create(**kwargs)

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


# Governed delegation adds four manager turns on the normal path (two
# proposals + two reviews). Keep a bounded ceiling while leaving recovery
# room for one failed booking/re-delegation cycle.
MAX_TURNS = 24

CHECKLIST_LABELS = {
    "flight": "航班已确认",
    "hotel": "酒店已确认",
    "meeting": "客户会面时间已确认",
    "calendar_sync": "日历事件已同步",
    "email": "确认邮件已发送",
}


def _new_checklist_state() -> dict:
    return {key: False for key in CHECKLIST_LABELS}


def _customer_calendar_was_queried(action_log: list[dict]) -> bool:
    return any(
        item["name"] == "get_customer_calendar"
        and item["result"].get("status") != "error"
        for item in action_log
    )


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
        if r.get("policy_id"):
            line += f"；依据政策：{r['policy_id']}（{r.get('policy_version', '')}）"
        if r.get("approver_role"):
            line += f"；审批角色：{r['approver_role']}"
        if r.get("user_note"):
            line += f"；用户补充说明：{r['user_note']}"
        lines.append(line)
    return "\n".join(lines)


async def generate_final_report(goal: str, checklist: dict, action_log: list[dict], approval_records: list[dict]) -> dict:
    """Generate execution/decision layer report after all checklist items are done."""
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
    except json.JSONDecodeError:
        execution_layer = []
        decision_layer = [raw_text.strip()] if raw_text.strip() else ["⚠️ 报告生成格式异常，请查看后台日志。"]
    else:
        try:
            validated = FinalReportSchema.model_validate(data)
            execution_layer = validated.execution_layer
            decision_layer = validated.decision_layer
        except ValidationError as ve:
            execution_layer = []
            decision_layer = [f"⚠️ 报告结构校验失败（{ve.error_count()} 处字段不符合预期）：{ve}"]
            if raw_text.strip():
                decision_layer.append(f"模型原始输出：{raw_text.strip()}")

    # Verify execution_layer IDs against real tool results
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


async def _resolve_pending_approval(
    session: Session,
    resume_input: dict,
    tool_functions: dict[str, Any],
    send_event,
    run_id: str,
) -> bool:
    """Apply an approval decision to session.pending_approval, then keep
    processing any tool calls that were queued behind it in the same batch.

    Returns True if execution paused again (e.g. a second approval was hit
    among the pending tool calls), False if it's safe to re-enter the model
    loop.
    """
    pending = session.pending_approval
    if pending is None:
        # Stray/duplicate resume with nothing to resolve - ignore.
        return False

    call_id = pending["call_id"]
    reason = pending["reason"]
    proposed_action = pending["proposed_action"]
    proposal_id = pending.get("proposal_id", "")
    requested_approved = bool(resume_input.get("approved"))
    user_note = (resume_input.get("user_note") or "").strip()
    purpose = classify_approval_purpose(reason, proposed_action)
    approver_role = resume_input.get("approver_role") or (
        "manager" if purpose == "hotel_over_budget" else session.role
    )
    resolution_error = ""
    if requested_approved and not is_valid_approver(purpose, approver_role):
        resolution_error = (
            f"角色 {approver_role!r} 无权批准 purpose={purpose}，审批按拒绝处理。"
        )
    if (
        requested_approved
        and purpose == "hotel_over_budget"
        and session.multi_agent_enabled
        and not proposal_id
    ):
        resolution_error = "酒店超预算审批缺少 proposal_id，不能形成可审计授权。"
    approved = requested_approved and not resolution_error

    if approved:
        result = {"status": "approved", "message": f"用户已批准：{proposed_action}"}
        if purpose == "hotel_over_budget":
            session.approval_state["hotel_over_budget_approved"] = True
            session.approval_state["hotel_over_budget_approved_proposal_id"] = proposal_id
            session.approval_state["hotel_over_budget_policy_ref"] = policy_store.get_policy_ref(
                "hotel_budget"
            )
    else:
        base_msg = resolution_error or (
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

    approval_topic_ref = {
        "hotel_over_budget": policy_store.get_policy_ref("hotel_over_budget_approval"),
        "confirmation_email": policy_store.get_policy_ref("confirmation_email_approval"),
    }.get(purpose)
    session.approval_records.append({
        "reason": reason,
        "proposed_action": proposed_action,
        "approved": approved,
        "requested_approved": requested_approved,
        "resolution_error": resolution_error or None,
        "user_note": user_note,
        "purpose": purpose,
        "policy_id": approval_topic_ref["policy_id"] if approval_topic_ref else None,
        "policy_version": approval_topic_ref["version"] if approval_topic_ref else None,
        "approver_role": approver_role,
        "proposal_id": proposal_id or None,
    })
    await send_event("approval_resolved", approved=approved, call_id=call_id)

    session.lifecycle_state = "RUNNING"
    session.messages.append({
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(result, ensure_ascii=False),
    })

    pending_calls = session.pending_tool_calls
    session.pending_approval = None
    session.pending_tool_calls = []

    if pending_calls:
        return await _process_tool_calls(session, tool_functions, pending_calls, send_event, run_id)
    return False


def _memory_subject_pairs(session: Session) -> list[dict]:
    subjects = [{"subject_type": "actor", "subject_id": session.actor_id}]
    customer_id = session.resolved_subjects.get("customer")
    route_id = session.resolved_subjects.get("route")
    if customer_id:
        subjects.append({"subject_type": "customer", "subject_id": customer_id})
    if route_id:
        subjects.append({"subject_type": "route", "subject_id": route_id})
    return subjects


def _refresh_session_memories(session: Session) -> int:
    """Recall newly resolved scopes and keep a bounded, serializable snapshot."""
    subjects = _memory_subject_pairs(session)
    subject_keys = {f"{item['subject_type']}:{item['subject_id']}" for item in subjects}
    if subject_keys.issubset(set(session.memory_initialized_subjects)):
        return 0
    recalled = memory_store.retrieve_memories(subjects, task_context={"per_type_limit": 3}, limit=9)
    by_id = {item["memory_id"]: item for item in session.recalled_memories}
    for item in recalled:
        by_id[item["memory_id"]] = item
    session.recalled_memories = list(by_id.values())
    session.memory_initialized_subjects = sorted(
        set(session.memory_initialized_subjects) | subject_keys
    )
    return len(recalled)


def _memory_extraction_subjects(session: Session) -> dict:
    return {
        "actor": session.actor_id,
        "customer": session.resolved_subjects.get("customer"),
        "company": session.resolved_subjects.get("company"),
        "route": session.resolved_subjects.get("route"),
    }


async def _process_tool_calls(
    session: Session,
    tool_functions: dict[str, Any],
    tool_calls: list[dict],
    send_event,
    run_id: str,
) -> bool:
    """Execute a batch of normalized tool calls in order.

    Each tool_call is {"id", "name", "arguments"}. If `request_human_approval`
    is hit, persists session (lifecycle=AWAITING_APPROVAL, pending_approval,
    pending_tool_calls = remaining unexecuted calls) and returns True so the
    caller can return out of run_agent_loop without blocking on anything.
    Returns False once every call in the batch has been executed.
    """
    for i, tc in enumerate(tool_calls):
        name = tc["name"]
        args = tc["arguments"]
        call_id = tc["id"]
        await send_event("tool_call", name=name, arguments=args, call_id=call_id)
        delegated_to = DELEGATION_TOOL_AGENTS.get(name)
        if delegated_to:
            await send_event(
                "delegation_started",
                from_agent=ORCHESTRATOR_AGENT,
                to_agent=delegated_to,
                tool_name=name,
                call_id=call_id,
            )

        if name == "request_human_approval":
            skill = SKILL_BY_NAME.get(name)
            if skill is not None and session.role not in skill.roles_allowed:
                result = {
                    "status": "permission_denied",
                    "reason": (
                        f"角色 '{session.role}' 无权调用 Skill '{name}'"
                        f"（允许的角色：{list(skill.roles_allowed)}）。"
                    ),
                }
                await send_event("tool_result", name=name, result=result, call_id=call_id)
                session.action_log.append({"name": name, "args": args, "result": result})
                session.messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
                continue

            reason = args.get("reason", "")
            proposed_action = args.get("proposed_action", "")
            proposal_id = args.get("proposal_id", "")
            await send_event(
                "approval_request",
                reason=reason,
                proposed_action=proposed_action,
                proposal_id=proposal_id or None,
                call_id=call_id,
            )
            session.lifecycle_state = "AWAITING_APPROVAL"
            session.pending_approval = {
                "call_id": call_id,
                "reason": reason,
                "proposed_action": proposed_action,
                "proposal_id": proposal_id,
            }
            session.pending_tool_calls = tool_calls[i + 1:]
            await asyncio.to_thread(session_store.save_session, run_id, session)
            return True

        func = tool_functions.get(name)
        if func is None:
            result = {"status": "error", "message": f"未知工具：{name}"}
        else:
            try:
                # Skill-level permission gate: same check used by harness.py's
                # eval cases, so runtime and eval can never silently diverge
                # on who is allowed to call a given Skill. Denied calls never
                # reach the underlying func — no side effect is attempted.
                result = wrap_with_permission_check(name, session.role, func, **args)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:  # noqa: BLE001
                result = {"status": "error", "message": str(exc)}

        await send_event("tool_result", name=name, result=result, call_id=call_id)
        if delegated_to:
            delegation_status = (
                "completed" if result.get("status") == "success" else "failed"
            )
            await send_event(
                "delegation_completed",
                from_agent=ORCHESTRATOR_AGENT,
                to_agent=delegated_to,
                delegation_status=delegation_status,
                delegation_id=result.get("delegation_id"),
                proposal_id=result.get("proposal_id"),
                review_id=result.get("review_id"),
                effective_decision=result.get("effective_decision"),
                model_usage=result.get("model_usage", {}),
                call_id=call_id,
            )
            if name == "delegate_policy_review" and result.get("status") == "success":
                if result.get("conflict"):
                    await send_event(
                        "agent_conflict",
                        proposal_id=result.get("proposal_id"),
                        model_decision=result.get("model_decision"),
                        runtime_decision=result.get("runtime_decision"),
                        effective_decision=result.get("effective_decision"),
                    )
                if result.get("effective_decision") == "REQUIRE_APPROVAL":
                    await send_event(
                        "escalation_required",
                        proposal_id=result.get("proposal_id"),
                        review_id=result.get("review_id"),
                        approver_roles=list(
                            policy_store.get_approver_roles("hotel_over_budget_approval")
                        ),
                    )
        session.action_log.append({"name": name, "args": args, "result": result})

        # Two-stage recall: actor preferences are loaded when a Session starts;
        # customer preferences are loaded only after CRM returns a stable ID.
        if name == "get_customer_info" and result.get("status") == "success":
            customer_id = result.get("customer_id") or result.get("company_id")
            if customer_id:
                session.resolved_subjects.update({
                    "customer": str(customer_id),
                    "company": result.get("company", ""),
                })
                try:
                    recalled_count = await asyncio.to_thread(_refresh_session_memories, session)
                    if recalled_count:
                        await send_event(
                            "memory_recalled", subject_type="customer",
                            subject_id=str(customer_id), count=recalled_count,
                        )
                except Exception as exc:  # noqa: BLE001
                    await send_event("memory_recall_failed", text=str(exc))

        # Soft warning for repeated readonly calls with identical args
        if is_repeated_readonly_call(name, args, session.seen_readonly_calls):
            await send_event(
                "efficiency_warning",
                text=f"⚠️ 只读查询工具 {name} 被完全相同的参数重复调用，疑似规划效率浪费。",
                name=name,
                arguments=args,
            )

        _update_checklist(session.checklist, _customer_calendar_was_queried(session.action_log), name, result)
        await send_event("checklist", items=session.checklist, labels=CHECKLIST_LABELS)

        session.messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(result, ensure_ascii=False),
        })

    return False


async def run_agent_loop(
    websocket: WebSocket,
    run_id: str,
    session: Session,
    resume_input: dict | None = None,
):
    """
    Core loop: call model -> execute tool -> feed result back -> repeat until
    no tool calls AND checklist is fully satisfied.

    Reentrant (Phase 2): never awaits a live queue for human input. At every
    pause point it persists `session` via session_store and returns. The
    caller is responsible for resuming later by loading the session back and
    calling this function again with a `resume_input` describing the human's
    decision/reply.

    `resume_input` shapes:
      {"kind": "approval", "approved": bool, "user_note": str}
      {"kind": "reply", "text": str}
    """
    checklist = session.checklist

    async def send_event(msg_type: str, **data):
        await send(websocket, msg_type, run_id=run_id, step_id=session.model_calls, **data)

    if client is None:
        await send_event(
            "error",
            text="⚠️ 还没配置 QWEN_API_KEY，请在项目根目录的 .env 文件里填好之后重启程序。",
        )
        return

    tool_functions = AGENT.make_tool_functions(session)
    # ``session.messages`` is the canonical history. Context strategies only
    # create a per-model-call view and never replace or truncate it.
    messages = session.messages

    await send_event("goal", text=session.goal)
    await send_event("checklist", items=checklist, labels=CHECKLIST_LABELS)

    try:
        recalled_count = await asyncio.to_thread(_refresh_session_memories, session)
        if recalled_count:
            await send_event(
                "memory_recalled", subject_type="actor",
                subject_id=session.actor_id, count=recalled_count,
            )
    except Exception as exc:  # noqa: BLE001
        # Recall is an optional context enhancement, never a task dependency.
        await send_event("memory_recall_failed", text=str(exc))

    if resume_input is not None:
        kind = resume_input.get("kind")
        if kind == "approval":
            paused = await _resolve_pending_approval(session, resume_input, tool_functions, send_event, run_id)
            if paused:
                return
        elif kind == "reply":
            text = resume_input.get("text", "")
            session.lifecycle_state = "RUNNING"
            messages.append({"role": "user", "content": text})
            await send_event("user_reply", text=text)
            # An explicit correction should take effect cross-task immediately,
            # even if the current task later pauses or fails.
            try:
                extracted = memory_store.extract_explicit_memories(
                    [text], _memory_extraction_subjects(session), run_id
                )
                if any(item.get("is_correction") for item in extracted):
                    written = await asyncio.to_thread(
                        memory_store.persist_explicit_memories,
                        [text], _memory_extraction_subjects(session), run_id,
                    )
                    if written:
                        session.memory_initialized_subjects = []
                        session.recalled_memories = []
                        await asyncio.to_thread(_refresh_session_memories, session)
                        await send_event("memory_written", count=len(written), timing="correction")
            except Exception as exc:  # noqa: BLE001
                await send_event("memory_write_failed", text=str(exc), timing="correction")

    while session.model_calls < MAX_TURNS:
        await send_event("thinking")

        context_strategy = get_context_strategy()
        context_build_started = time.perf_counter()
        model_context = build_model_context(session, context_strategy)
        context_build_latency_ms = (time.perf_counter() - context_build_started) * 1000
        input_message_count = len(model_context)
        estimated_context_tokens = estimate_context_tokens(model_context)
        canonical_message_count = len(session.messages)
        canonical_estimated_context_tokens = estimate_context_tokens(session.messages)
        context_compacted = model_context != session.messages

        # Timeout retry: only for timeout errors, not quota errors
        timeout_retries = 0
        model_latency_ms = 0.0
        exc: Exception | None = None
        message = usage_info = None
        while True:
            try:
                model_call_started = time.perf_counter()
                try:
                    message, usage_info = await asyncio.to_thread(_call_with_tools, model_context)
                finally:
                    model_latency_ms += (time.perf_counter() - model_call_started) * 1000
                exc = None
                break
            except Exception as call_exc:  # noqa: BLE001
                exc = call_exc
                if _is_timeout_error(exc) and timeout_retries < QWEN_TIMEOUT_MAX_RETRIES:
                    timeout_retries += 1
                    await send_event(
                        "retrying",
                        text=f"⚠️ 模型调用超时，正在进行第 {timeout_retries} 次业务层重试……",
                        attempt=timeout_retries,
                    )
                    await asyncio.sleep(QWEN_TIMEOUT_RETRY_BACKOFF_SECONDS * timeout_retries)
                    continue
                break

        if exc is not None:
            if _is_quota_error(exc):
                previous_model = _active_model["name"]
                if QWEN_BACKUP_MODEL and _active_model["name"] != QWEN_BACKUP_MODEL:
                    _active_model["name"] = QWEN_BACKUP_MODEL
                    await send_event(
                        "infra_error",
                        text=(
                            f"⚠️ 模型 {previous_model} 额度耗尽（{exc}），已自动切换到备用模型 "
                            f"{QWEN_BACKUP_MODEL}。本次任务因基础设施问题中止，请重新发起任务，"
                            f"新任务会使用备用模型继续执行。"
                        ),
                        model_used=previous_model,
                        switched_to=QWEN_BACKUP_MODEL,
                    )
                else:
                    reason = (
                        "备用模型额度也已耗尽，且没有更多可切换的模型"
                        if QWEN_BACKUP_MODEL else "未配置备用模型（QWEN_BACKUP_MODEL）"
                    )
                    await send_event(
                        "infra_error",
                        text=(
                            f"⚠️ 模型 {previous_model} 额度耗尽（{exc}），{reason}，"
                            f"本次任务因基础设施问题中止。"
                        ),
                        model_used=previous_model,
                        switched_to=None,
                    )
            elif _is_timeout_error(exc):
                await send_event(
                    "infra_error",
                    text=(
                        f"⚠️ 模型调用超时（{exc}），已重试 {timeout_retries} 次业务层重试仍未成功，"
                        f"网关响应过慢或连接异常，本次任务因基础设施问题中止，请重新发起任务重试。"
                    ),
                    model_used=_active_model["name"],
                    switched_to=None,
                )
            else:
                await send_event("error", text=f"⚠️ 调用模型失败：{exc}")
            return
        session.model_calls += 1
        await send_event(
            "usage",
            **(usage_info or {}),
            context_strategy=context_strategy.value,
            context_compacted=context_compacted,
            input_message_count=input_message_count,
            estimated_context_tokens=estimated_context_tokens,
            canonical_message_count=canonical_message_count,
            canonical_estimated_context_tokens=canonical_estimated_context_tokens,
            model_latency_ms=round(model_latency_ms, 3),
            context_build_latency_ms=round(context_build_latency_ms, 3),
        )

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

        if message.content:
            await send_event("reasoning", text=message.content)

        if not message.tool_calls:
            if all(checklist.values()):
                await send_event("generating_report")
                try:
                    report = await generate_final_report(session.goal, checklist, session.action_log, session.approval_records)
                    await send_event("final_report", **report)
                except Exception as exc:  # noqa: BLE001
                    await send_event("error", text=f"⚠️ 生成最终报告失败：{exc}")
                session.lifecycle_state = "DONE"
                await asyncio.to_thread(session_store.save_session, run_id, session)
                try:
                    user_texts = [
                        item.get("content", "") for item in session.messages
                        if item.get("role") == "user" and isinstance(item.get("content"), str)
                    ]
                    written = await asyncio.to_thread(
                        memory_store.persist_explicit_memories,
                        user_texts, _memory_extraction_subjects(session), run_id,
                    )
                    if written:
                        await send_event("memory_written", count=len(written), timing="post_run")
                except Exception as exc:  # noqa: BLE001
                    # A completed booking remains completed even when Memory IO fails.
                    await send_event("memory_write_failed", text=str(exc), timing="post_run")
                await send_event("done", model_used=_active_model["name"])
                return
            else:
                await send_event("awaiting_reply")
                session.lifecycle_state = "AWAITING_REPLY"
                await asyncio.to_thread(session_store.save_session, run_id, session)
                return

        tool_calls_norm = [
            {"id": tc.id, "name": tc.function.name, "arguments": _safe_json_loads(tc.function.arguments)}
            for tc in message.tool_calls
        ]

        # Guard: warn if gateway returns multiple tool calls despite parallel_tool_calls=False
        if len(tool_calls_norm) > 1:
            await send_event(
                "multi_tool_call_detected",
                text=(
                    f"⚠️ 检测到一轮返回了 {len(tool_calls_norm)} 个 tool_call，"
                    "parallel_tool_calls=False 未能阻止网关返回多个并行调用。"
                    "这是已知风险点（批内进度序列化，Phase 2 处理），本轮仍会"
                    "按原有顺序全部执行。"
                ),
                count=len(tool_calls_norm),
            )

        paused = await _process_tool_calls(session, tool_functions, tool_calls_norm, send_event, run_id)
        if paused:
            return

    session.lifecycle_state = "ERROR"
    await asyncio.to_thread(session_store.save_session, run_id, session)
    await send_event(
        "error",
        text=f"⚠️ 已经调用了 {MAX_TURNS} 次模型还没结束，可能陷入了重复尝试，已强制停止。"
             f"这种情况本身就是一类值得记录的 Bad Case（重复失败/死循环）。",
    )


@app.get("/api/memories")
async def get_saved_memories(subject_type: str, subject_id: str, include_inactive: bool = False):
    """List Memory records for one explicit entity scope."""
    try:
        records = await asyncio.to_thread(
            memory_store.list_memories, subject_type, subject_id, include_inactive
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"memories": records}


@app.delete("/api/memories/{memory_id}")
async def delete_saved_memory(memory_id: str):
    deleted = await asyncio.to_thread(memory_store.forget_memory, memory_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory not found or already deleted")
    return {"deleted": True, "memory_id": memory_id}


@app.delete("/api/memories")
async def clear_saved_memories(subject_type: str, subject_id: str):
    try:
        count = await asyncio.to_thread(memory_store.clear_memories, subject_id, subject_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"deleted_count": count, "subject_type": subject_type, "subject_id": subject_id}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint: manages agent loop lifecycle.

    Phase 2: no more live asyncio.Queue handoff to a blocked run_agent_loop.
    Every human input (approval_response / user_reply) loads the persisted
    Session back from session_store and spins up a *new* run_agent_loop task
    with an explicit resume_input. This also means a fresh websocket
    connection (e.g. after a server restart) can resume a run via `run_id`
    without needing the original connection/task to still be alive.
    """
    await websocket.accept()
    current_task: asyncio.Task | None = None
    active_run_id: str | None = None
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
                    continue
                goal = (payload.get("goal") or "").strip()
                if not goal:
                    continue
                run_id = str(uuid.uuid4())
                active_run_id = run_id
                actor_id = (payload.get("actor_id") or "demo_employee").strip()
                resolved_subjects = {}
                origin = (payload.get("origin") or "").strip()
                destination = (payload.get("destination") or "").strip()
                if origin and destination:
                    resolved_subjects["route"] = f"{origin.casefold()}->{destination.casefold()}"
                session = Session.new(
                    goal, AGENT.system_prompt,
                    actor_id=actor_id,
                    resolved_subjects=resolved_subjects,
                )
                current_task = asyncio.create_task(run_agent_loop(websocket, run_id, session))

            elif msg_type == "resume_task":
                # Informational only: lets a (possibly brand new) connection
                # re-render state for a run_id without replaying the full
                # event stream. Does NOT itself advance execution.
                run_id = payload.get("run_id")
                if not run_id:
                    continue
                loaded = await asyncio.to_thread(session_store.load_session, run_id)
                if loaded is None:
                    await send(websocket, "error", text=f"⚠️ 找不到 run_id={run_id} 对应的会话记录。")
                    continue
                active_run_id = run_id
                await send(
                    websocket,
                    "session_resumed",
                    run_id=run_id,
                    lifecycle_state=loaded.lifecycle_state,
                    checklist=loaded.checklist,
                    labels=CHECKLIST_LABELS,
                    action_log=loaded.action_log,
                )

            elif msg_type == "approval_response":
                run_id = payload.get("run_id") or active_run_id
                if not run_id:
                    continue
                if current_task is not None and not current_task.done():
                    # A run is already actively executing on this connection.
                    continue
                loaded = await asyncio.to_thread(session_store.load_session, run_id)
                if loaded is None or loaded.lifecycle_state != "AWAITING_APPROVAL":
                    await send(
                        websocket, "error",
                        text=f"⚠️ run_id={run_id} 当前不处于等待审批状态，忽略此次响应。",
                    )
                    continue
                active_run_id = run_id
                resume_input = {
                    "kind": "approval",
                    "approved": bool(payload.get("approved")),
                    "user_note": (payload.get("user_note") or "").strip(),
                    "approver_role": payload.get("approver_role"),
                }
                current_task = asyncio.create_task(
                    run_agent_loop(websocket, run_id, loaded, resume_input)
                )

            elif msg_type == "user_reply":
                text = (payload.get("text") or "").strip()
                if not text:
                    continue
                run_id = payload.get("run_id") or active_run_id
                if not run_id:
                    continue
                if current_task is not None and not current_task.done():
                    continue
                loaded = await asyncio.to_thread(session_store.load_session, run_id)
                if loaded is None or loaded.lifecycle_state != "AWAITING_REPLY":
                    await send(
                        websocket, "error",
                        text=f"⚠️ run_id={run_id} 当前不处于等待用户回复状态，忽略此次响应。",
                    )
                    continue
                active_run_id = run_id
                resume_input = {"kind": "reply", "text": text}
                current_task = asyncio.create_task(
                    run_agent_loop(websocket, run_id, loaded, resume_input)
                )

    except WebSocketDisconnect:
        print("客户端断开连接")
        if current_task is not None and not current_task.done():
            current_task.cancel()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
