"""
Skill 抽象层。

背景：main.py 里原来的"工具"是三样东西各自为政拼出来的——
  1. _make_booking_tools() 里的一堆裸函数（谁能调、有没有副作用全靠读代码猜）
  2. TOOLS 列表里单独维护的一份 JSON Schema（跟第 1 点靠函数名字符串对应，容易漂移）
  3. 权限判断散落在函数体内部（比如 book_hotel 里手写的超预算拦截）

这个文件把上面三样收拢成一个统一的 Skill 契约：每个 Skill 都能回答
"谁可以调用 / 输入是什么 / 输出是什么 / 会不会产生外部副作用 / 失败后 Agent 应如何处理"。

main.py 不再自己维护 TOOLS 和权限判断，改为从这里派生。
harness.py 的"权限拒绝"Eval Case 也直接复用这里的 SKILL_BY_NAME。

2026 修订：补上 `returns`（成功态输出的 JSON Schema）。此前只有 `parameters`
（输入 Schema），对照 JD/对齐路径里明确要求的"输入输出 Schema"，缺输出这一半
——下游（模型、报告生成、Eval）没有一份可依赖的"成功返回长什么样"的契约，
只能靠读 main.py 里的 MOCK_DATA 反推。现在两者同源维护在 Skill 定义里。
"""

from __future__ import annotations

import dataclasses
from typing import Callable

# ---------------------------------------------------------------------------
# 角色定义
#
# 注意：这是"谁能调用这个 Skill"的静态权限轴，跟 approval_utils.py 里
# "这一次具体的动作要不要走人工审批"（比如酒店超预算）是两条正交的规则。
# 一个 employee 完全可以被允许调用 book_hotel 这个 Skill，但某一次具体调用
# 因为超预算仍然会被 approval 流程拦下来，两者不冲突。
# ---------------------------------------------------------------------------
ROLE_INTERN = "intern"
ROLE_EMPLOYEE = "employee"
ROLE_MANAGER = "manager"
ROLE_TRAVEL_ADMIN = "travel_admin"

ALL_ROLES = (ROLE_INTERN, ROLE_EMPLOYEE, ROLE_MANAGER, ROLE_TRAVEL_ADMIN)
# 会产生外部副作用/花钱/对客户可见的动作，实习生角色不能调用，只能只读查询。
STAFF_ROLES = (ROLE_EMPLOYEE, ROLE_MANAGER, ROLE_TRAVEL_ADMIN)

DEFAULT_ROLE = ROLE_EMPLOYEE


@dataclasses.dataclass(frozen=True)
class Skill:
    """一个 Skill 的完整契约（元信息 + 输入/输出 Schema，不含运行时闭包本身）。

    谁可以调用 -> roles_allowed
    输入是什么 -> parameters（JSON Schema，格式与 OpenAI function calling 一致）
    输出是什么 -> returns（成功态返回值的 JSON Schema；失败态见 on_failure 的
        自然语言描述 + 运行时里统一的 status in {error, not_found, rejected,
        permission_denied} 惯例，不在这里重复建模成 Schema，避免过度设计）
    会不会有外部副作用 -> side_effect
    失败后 Agent 应如何处理 -> on_failure（自然语言，会被拼进模型可读的说明里，
        也是产品文档里"失败语义"这一栏的来源）
    """

    name: str
    version: str
    description: str
    roles_allowed: tuple[str, ...]
    side_effect: bool
    risk_level: str  # "low" | "medium" | "high"
    on_failure: str
    parameters: dict
    returns: dict
    deprecated: bool = False
    supersedes: str | None = None  # 指向被取代的旧版本 name（用于版本演进展示）

    def to_tool_schema(self) -> dict:
        """转换为 OpenAI function-calling 的 tools[] 条目格式，main.py 直接拿去用。
        注意：function calling 协议本身没有"返回值 schema"这个字段位置，
        `returns` 不进这里，只用于 Skill Registry 文档和 Eval 里做契约校验。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def allows(self, role: str) -> bool:
        return role in self.roles_allowed


def check_skill_permission(skill: "Skill | None", role: str) -> bool:
    """未注册为 Skill 的工具名（理论上不该出现）默认放行，避免误伤；
    已注册的 Skill 严格按 roles_allowed 判断。"""
    if skill is None:
        return True
    return skill.allows(role)


# ---------------------------------------------------------------------------
# 通用输出片段：大多数工具成功/失败都会带 status 字段，抽出来复用。
# ---------------------------------------------------------------------------
_STATUS_SUCCESS = {"type": "string", "enum": ["success"]}

# ---------------------------------------------------------------------------
# 只读查询类 Skill：全角色可调用，无副作用，失败后可以直接重试/换参数重查。
# ---------------------------------------------------------------------------
_READONLY_ON_FAILURE = "只读查询，失败可直接重试或换参数重新查询，不影响任何已产生的状态。"

_GET_CUSTOMER_INFO = Skill(
    name="get_customer_info",
    version="v1",
    description="查询客户的 CRM 档案，包括公司名称、地址、联系人信息。用于确定拜访目的地。",
    roles_allowed=ALL_ROLES,
    side_effect=False,
    risk_level="low",
    on_failure=_READONLY_ON_FAILURE,
    parameters={
        "type": "object",
        "properties": {
            "company": {"type": "string", "description": "客户公司名称，如果还不知道可以留空"},
        },
    },
    returns={
        "type": "object",
        "properties": {
            "status": _STATUS_SUCCESS,
            "customer_id": {"type": "string", "description": "稳定客户实体 ID，用于跨任务 scope 隔离"},
            "company_id": {"type": "string", "description": "稳定公司实体 ID"},
            "company": {"type": "string"},
            "address": {"type": "string"},
            "contact": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "title": {"type": "string"},
                    "phone": {"type": "string"},
                },
            },
            "note": {"type": "string"},
        },
        "required": ["status", "customer_id", "company_id", "company", "address", "contact"],
    },
)

_GET_MY_CALENDAR = Skill(
    name="get_my_calendar",
    version="v2",
    description="查询我自己在指定日期的日程安排，确认自己是否有空。运行时可从 mock 或只读 MCP 日历连接获取数据。",
    roles_allowed=ALL_ROLES,
    side_effect=False,
    risk_level="low",
    on_failure=(
        "只读查询，不产生任何外部写操作。temporarily_unavailable 可有限重试；"
        "permission_denied 应提示用户完成授权；data_missing 代表未知，绝不能当作全天有空；"
        "invalid_input 应要求用户确认日期。"
    ),
    parameters={
        "type": "object",
        "properties": {"date": {"type": "string", "description": "要查询的日期，例如 '下周二'"}},
        "required": ["date"],
    },
    returns={
        "type": "object",
        "properties": {
            "status": _STATUS_SUCCESS,
            "date": {"type": "string"},
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"time": {"type": "string"}, "title": {"type": "string"}},
                },
            },
            "free_slots": {"type": "array", "items": {"type": "string"}},
            "source": {"type": "string", "enum": ["mock", "mcp"]},
            "trace_id": {"type": "string"},
            "data_as_of": {"type": ["string", "null"]},
            "latency_ms": {"type": "number"},
        },
        "required": ["status", "date", "events", "free_slots"],
    },
)

_GET_CUSTOMER_CALENDAR = Skill(
    name="get_customer_calendar",
    version="v1",
    description="查询客户在指定日期的日程安排，了解客户什么时间方便见面。",
    roles_allowed=ALL_ROLES,
    side_effect=False,
    risk_level="low",
    on_failure=_READONLY_ON_FAILURE,
    parameters={
        "type": "object",
        "properties": {"date": {"type": "string", "description": "要查询的日期，例如 '下周二'"}},
        "required": ["date"],
    },
    returns={
        "type": "object",
        "properties": {
            "status": _STATUS_SUCCESS,
            "date": {"type": "string"},
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"time": {"type": "string"}, "title": {"type": "string"}},
                },
            },
            "free_slots": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["status", "date", "events", "free_slots"],
    },
)

_GET_TRAVEL_POLICY = Skill(
    name="get_travel_policy",
    version="v1",
    description="查询公司差旅政策，包括机票和酒店的预算上限，用于判断后续方案是否合规。",
    roles_allowed=ALL_ROLES,
    side_effect=False,
    risk_level="low",
    on_failure=_READONLY_ON_FAILURE,
    parameters={"type": "object", "properties": {}},
    returns={
        "type": "object",
        "properties": {
            "status": _STATUS_SUCCESS,
            "flight_budget": {"type": "number"},
            "hotel_budget_per_night": {"type": "number"},
            "notes": {"type": "string"},
            "policy_refs": {
                "type": "object",
                "description": "每条阈值对应的可引用政策原文（policy_id/version/effective_date/rule_text），"
                "供模型和最终报告引用出处，而不是只给裸数字。",
                "properties": {
                    "flight_budget": {"type": ["object", "null"]},
                    "hotel_budget": {"type": ["object", "null"]},
                },
            },
        },
        "required": ["status", "flight_budget", "hotel_budget_per_night"],
    },
)

_SEARCH_FLIGHT = Skill(
    name="search_flight",
    version="v1",
    description=(
        "搜索指定日期前往目的地的可选航班，返回完整候选列表（航班编号、出发/到达时间、"
        "价格、查询时的余票状态）。该工具只负责提供候选信息，不会替你选择或预订航班。"
        "你需要结合客户会面时间、出行安排和差旅预算自行比较后，再调用 book_flight。"
    ),
    roles_allowed=ALL_ROLES,
    side_effect=False,
    risk_level="low",
    on_failure=_READONLY_ON_FAILURE,
    parameters={
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "出发日期，例如 '下周二'"},
            "destination": {"type": "string", "description": "到达城市，例如 '上海'"},
        },
        "required": ["date"],
    },
    returns={
        "type": "object",
        "properties": {
            "status": _STATUS_SUCCESS,
            "flights": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "flight_id": {"type": "string"},
                        "departure_time": {"type": "string"},
                        "arrival_time": {"type": "string"},
                        "price": {"type": "number"},
                        "booking_status": {"type": "string", "enum": ["available", "sold_out"]},
                    },
                    "required": ["flight_id", "departure_time", "arrival_time", "price", "booking_status"],
                },
                "description": "查询时刻的候选快照；booking_status 不保证在 book_flight 提交时仍然成立"
                "（可能存在实时库存变化），这是设计内的行为，不是 bug。",
            },
        },
        "required": ["status", "flights"],
    },
)

_SEARCH_HOTEL = Skill(
    name="search_hotel",
    version="v1",
    description=(
        "查询客户公司附近的酒店候选列表，每个候选都带 hotel_id、价格和 within_policy"
        "（是否在差旅政策预算内）。预订时必须使用候选中真实存在的 hotel_id。"
    ),
    roles_allowed=ALL_ROLES,
    side_effect=False,
    risk_level="low",
    on_failure=_READONLY_ON_FAILURE,
    parameters={
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "入住日期，例如 '下周二'"},
            "near": {"type": "string", "description": "希望靠近的地点，例如客户公司地址或所在商圈"},
        },
        "required": ["date", "near"],
    },
    returns={
        "type": "object",
        "properties": {
            "status": _STATUS_SUCCESS,
            "options": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "hotel_id": {"type": "string"},
                        "hotel_name": {"type": "string"},
                        "price_per_night": {"type": "number"},
                        "within_policy": {"type": "boolean"},
                    },
                    "required": ["hotel_id", "hotel_name", "price_per_night", "within_policy"],
                },
            },
        },
        "required": ["status", "options"],
    },
)

# ---------------------------------------------------------------------------
# Agent-to-agent delegation Skills. Both are read-only with respect to the
# outside world: they create typed, persisted decision artifacts but cannot
# book, approve, or mutate provider state.
# ---------------------------------------------------------------------------
_DELEGATE_BOOKING_PLAN = Skill(
    name="delegate_booking_plan",
    version="v1",
    description=(
        "把已经搜索到的 flight 或 hotel 候选委派给 Booking Subagent 比较。"
        "必须先调用对应 search Skill；返回 proposal_id、推荐候选、比较理由和风险标记。"
        "Booking Subagent 不能执行预订，也不能批准超预算方案。"
    ),
    roles_allowed=ALL_ROLES,
    side_effect=False,
    risk_level="low",
    on_failure=(
        "missing_context 表示尚未搜索候选，应先调用对应 search Skill；delegation_failed 表示"
        "子 agent 输出未通过 Schema 或候选 ID 校验，可修正 objective/constraints 后重试一次。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "booking_type": {"type": "string", "enum": ["flight", "hotel"]},
            "objective": {
                "type": "string",
                "description": "本次比较要达成的业务目标，不要复制整段对话历史。",
            },
            "constraints": {
                "type": "string",
                "description": "已确认的时间、预算或偏好约束；不能写入新的授权。",
            },
        },
        "required": ["booking_type", "objective"],
    },
    returns={
        "type": "object",
        "properties": {
            "status": _STATUS_SUCCESS,
            "proposal_id": {"type": "string"},
            "delegation_id": {"type": "string"},
            "booking_type": {"type": "string", "enum": ["flight", "hotel"]},
            "recommended_option_id": {"type": "string"},
            "rationale": {"type": "string"},
            "alternatives_considered": {"type": "array", "items": {"type": "string"}},
            "risk_flags": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "status", "proposal_id", "delegation_id", "booking_type",
            "recommended_option_id", "rationale",
        ],
    },
)

_DELEGATE_POLICY_REVIEW = Skill(
    name="delegate_policy_review",
    version="v1",
    description=(
        "把 Booking Subagent 生成的 proposal_id 委派给 Policy/Compliance Subagent 复核。"
        "返回 model_decision、deterministic runtime_decision 和取更严格结果后的"
        " effective_decision。该 agent 可以否决或升级审批，但不能放宽 runtime policy。"
    ),
    roles_allowed=ALL_ROLES,
    side_effect=False,
    risk_level="medium",
    on_failure=(
        "not_found 表示 proposal_id 无效；delegation_failed 表示复核输出未通过 Schema。"
        "没有成功 review 时，book Skill 会 fail closed。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "proposal_id": {
                "type": "string",
                "description": "delegate_booking_plan 返回的 proposal_id。",
            },
        },
        "required": ["proposal_id"],
    },
    returns={
        "type": "object",
        "properties": {
            "status": _STATUS_SUCCESS,
            "review_id": {"type": "string"},
            "proposal_id": {"type": "string"},
            "model_decision": {
                "type": "string", "enum": ["ALLOW", "REQUIRE_APPROVAL", "DENY"]
            },
            "runtime_decision": {
                "type": "string", "enum": ["ALLOW", "REQUIRE_APPROVAL", "DENY"]
            },
            "effective_decision": {
                "type": "string", "enum": ["ALLOW", "REQUIRE_APPROVAL", "DENY"]
            },
            "conflict": {"type": "boolean"},
            "policy_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "status", "review_id", "proposal_id", "model_decision",
            "runtime_decision", "effective_decision", "conflict",
        ],
    },
)

# ---------------------------------------------------------------------------
# 写入/有外部副作用类 Skill：仅 staff 角色（employee/manager/travel_admin）
# 可调用，intern 角色会被拒绝——这是"权限拒绝" Eval Case 要验证的路径。
# ---------------------------------------------------------------------------
_BOOK_FLIGHT = Skill(
    name="book_flight",
    version="v2",
    description=(
        "预订 search_flight 返回的某个具体航班，必须传入搜索结果中真实存在的 flight_id，"
        "以及已通过 Policy/Compliance Subagent 复核的 proposal_id。"
        "搜索结果只代表查询时的状态，提交订单时仍可能因实时库存变化而预订失败——遇到失败后，"
        "应根据返回原因从之前的候选中重新选择，或重新搜索，不要放弃。"
    ),
    roles_allowed=STAFF_ROLES,
    side_effect=True,
    risk_level="medium",
    on_failure=(
        "预订失败（如实时售罄）不等于任务失败：应从已知候选中重新选择或重新搜索，"
        "但同一 flight_id 不应连续重试；重复调用同一 flight_id 的成功预订会被幂等处理，"
        "不会产生第二笔订单。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "flight_id": {"type": "string", "description": "要预订的航班编号，例如 'MU5101'"},
            "proposal_id": {
                "type": "string",
                "description": "delegate_booking_plan 返回且已完成 policy review 的 proposal_id。",
            },
        },
        "required": ["flight_id", "proposal_id"],
    },
    returns={
        "type": "object",
        "description": "成功态（status=success）字段如下；失败态另有 status in "
        "{not_found, failed} 及 reason 字段，两者字段不同，Agent 需先判 status 再取字段。",
        "properties": {
            "status": _STATUS_SUCCESS,
            "flight_id": {"type": "string"},
            "departure_time": {"type": "string"},
            "arrival_time": {"type": "string"},
            "price": {"type": "number"},
            "order_id": {"type": "string"},
            "message": {"type": "string"},
            "duplicate_call": {
                "type": "boolean",
                "description": "true 表示该 flight_id 此前已成功预订过，本次是幂等重放，未产生新订单。",
            },
        },
        "required": ["status", "flight_id", "order_id"],
    },
)

_BOOK_HOTEL = Skill(
    name="book_hotel",
    version="v2",
    description=(
        "预订某个具体酒店，必须传入 search_hotel 返回的候选中真实存在的 hotel_id，"
        "以及已通过 Policy/Compliance Subagent 复核的 proposal_id。"
        "如果该酒店超出差旅政策的每晚预算上限（候选中 within_policy 为 false），必须先调用"
        " request_human_approval 获得批准之后才能调用这个工具，不能自己直接下单。"
    ),
    roles_allowed=STAFF_ROLES,
    side_effect=True,
    risk_level="high",
    on_failure=(
        "超出政策预算且未获批准会被运行时硬拦截（status=rejected），不会静默放行，"
        "且拦截理由会带上依据的具体 policy_id/version（来自 policy_store，与"
        " get_travel_policy 返回给模型看的政策原文同源，不是裸数字）；"
        "拦截后应引导 Agent 调用 request_human_approval 而不是重试同一超预算酒店。"
        "重复调用同一 hotel_id 的成功预订会被幂等处理。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "hotel_id": {"type": "string", "description": "要预订的酒店编号，例如 'HTL-C'"},
            "proposal_id": {
                "type": "string",
                "description": "delegate_booking_plan 返回且已完成 policy review 的 proposal_id。",
            },
        },
        "required": ["hotel_id", "proposal_id"],
    },
    returns={
        "type": "object",
        "description": "成功态（status=success）字段如下；失败态另有 status in "
        "{not_found, rejected} —— rejected 时带 policy_id + reason，说明依据哪条政策拦截。",
        "properties": {
            "status": _STATUS_SUCCESS,
            "hotel_id": {"type": "string"},
            "hotel_name": {"type": "string"},
            "price_per_night": {"type": "number"},
            "confirmation_id": {"type": "string"},
            "message": {"type": "string"},
            "duplicate_call": {
                "type": "boolean",
                "description": "true 表示该 hotel_id 此前已成功预订过，本次是幂等重放，未产生新订单。",
            },
        },
        "required": ["status", "hotel_id", "confirmation_id"],
    },
)

_SYNC_CALENDAR = Skill(
    name="sync_calendar",
    version="v1",
    description="把这次拜访的行程同步为一条日历事件，方便双方以及自己后续查看。应该在航班和酒店都确认之后调用。",
    roles_allowed=STAFF_ROLES,
    side_effect=True,
    risk_level="low",
    on_failure="失败后可直接重试，日历事件创建本身可幂等重放，不涉及不可逆的外部动作。",
    parameters={
        "type": "object",
        "properties": {
            "event_title": {"type": "string", "description": "日历事件标题，例如 '上海客户拜访'"},
            "date": {"type": "string", "description": "日期"},
            "time": {"type": "string", "description": "会面时间"},
        },
        "required": ["event_title", "date", "time"],
    },
    returns={
        "type": "object",
        "properties": {
            "status": _STATUS_SUCCESS,
            "event_id": {"type": "string"},
        },
        "required": ["status", "event_id"],
    },
)

# --- send_confirmation_email：故意演示"版本化"，v1 为当前生产版本 ------------
_SEND_CONFIRMATION_EMAIL_V1 = Skill(
    name="send_confirmation_email",
    version="v1",
    description=(
        "向客户发送本次拜访的确认邮件/通知。这是面向客户的外部动作，应该在行程全部确认"
        "（航班、酒店、会面时间）之后，并且已经通过 request_human_approval 获得最终发送确认"
        "之后，作为最后一步调用。"
    ),
    roles_allowed=STAFF_ROLES,
    side_effect=True,
    risk_level="high",
    on_failure=(
        "这是面向客户的不可逆外部动作，失败后不能静默重试或换措辞硬发；"
        "应重新走一次 request_human_approval 确认之后再决定是否重发。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "收件人，例如客户联系人姓名或邮箱"},
            "summary": {"type": "string", "description": "本次拜访行程的简要总结，会写进邮件正文"},
        },
        "required": ["to", "summary"],
    },
    returns={
        "type": "object",
        "properties": {
            "status": _STATUS_SUCCESS,
            "message_id": {"type": "string"},
        },
        "required": ["status", "message_id"],
    },
)

# v2 草案（未接入运行时，仅作为"这套体系支持版本演进"的展示）：
# 收紧到只有 travel_admin 才能触发面向客户的最终发送，employee/manager 只能预生成草稿。
# deprecated 标记 + supersedes 指向 v1，Skill Registry 文档里两版会并排列出对比。
_SEND_CONFIRMATION_EMAIL_V2_DRAFT = Skill(
    name="send_confirmation_email",
    version="v2-draft",
    description=(
        "[v2 草案，未接入运行时] 向客户发送本次拜访的确认邮件/通知。相较 v1，"
        "把'谁能真正触发外部发送'收紧为仅 travel_admin，employee/manager 调用时"
        "会被降级为生成待审草稿而非直接发出。"
    ),
    roles_allowed=(ROLE_TRAVEL_ADMIN,),
    side_effect=True,
    risk_level="high",
    on_failure="同 v1；此外权限不足时应返回 permission_denied 并提示改为生成草稿而非报错中止。",
    parameters=_SEND_CONFIRMATION_EMAIL_V1.parameters,
    returns={
        "type": "object",
        "description": "travel_admin 调用时同 v1；employee/manager 调用会被降级为 status=draft_created，"
        "返回 draft_id 而非 message_id（v2 草案设计，未接入运行时）。",
        "properties": {
            "status": {"type": "string", "enum": ["success", "draft_created"]},
            "message_id": {"type": "string"},
            "draft_id": {"type": "string"},
        },
        "required": ["status"],
    },
    deprecated=False,
    supersedes="send_confirmation_email",
)

_REQUEST_HUMAN_APPROVAL = Skill(
    name="request_human_approval",
    version="v1",
    description=(
        "在需要人工审批的场景下调用此工具，暂停当前流程，向用户展示理由和你打算"
        "执行的动作，等待用户点击'批准'或'拒绝'后再继续。以下两种情况必须调用它："
        "① 打算预订的酒店价格超出差旅政策的每晚预算上限（对应 POL-TRAVEL-2026-003"
        " 酒店超预算审批规则，审批人限定为 manager/travel_admin）；"
        "② 正式向客户发送确认邮件之前的最终确认（对应 POL-TRAVEL-2026-004 客户确认邮件"
        "最终发送审批，无论前面是否超预算都需要走这一步，因为这是面向客户的外部、"
        "不可逆动作）。这两条政策各自允许谁审批，由 policy_store 里的 approver_roles"
        " 决定，不在这里写死。"
    ),
    roles_allowed=STAFF_ROLES,
    side_effect=False,
    risk_level="low",
    on_failure=(
        "调用本身不会失败；用户拒绝不是错误，是正常分支，Agent 应据此重新规划而不是"
        "重试原方案。无论批准还是拒绝，运行时都会在 approval_records 里连带记录本次"
        "审批依据的 policy_id/version 和实际审批人角色，供事后审计追溯——这一层不"
        "在这个 Skill 的即时返回值里，而是运行时侧带出的（见 main.py"
        " _resolve_pending_approval）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "为什么需要人工审批，用自然语言说明当前情况"},
            "proposed_action": {"type": "string", "description": "如果获得批准，你打算执行的具体动作"},
            "proposal_id": {
                "type": "string",
                "description": "酒店超预算审批必须传入对应 proposal_id；邮件发送审批可省略。",
            },
        },
        "required": ["reason", "proposed_action"],
    },
    returns={
        "type": "object",
        "description": "此工具本身不会立即返回结果给模型——运行时会暂停并持久化会话，等待人工"
        "决策后，才把下面这个结构作为 tool 消息回填。status=approved 时后续可继续原计划；"
        "status=rejected 时 message 会附带拒绝说明，Agent 应据此重新规划。这个即时返回值本身"
        "不带 policy_id——政策引用是运行时额外记录进 approval_records 的审计信息，"
        "不塞进反馈给模型的这条 tool 消息里，避免和模型的下一步决策耦合。",
        "properties": {
            "status": {"type": "string", "enum": ["approved", "rejected"]},
            "message": {"type": "string"},
        },
        "required": ["status", "message"],
    },
)


# ---------------------------------------------------------------------------
# 生效中的 Skill 集合：main.py 的 TOOLS / tool_functions 都从这里派生。
# 注意 _SEND_CONFIRMATION_EMAIL_V2_DRAFT 不在这个列表里——它是版本演进的
# 展示件，不接入运行时，避免临时改变现有 Eval Case 的行为基线。
# ---------------------------------------------------------------------------
ACTIVE_SKILLS: tuple[Skill, ...] = (
    _GET_CUSTOMER_INFO,
    _GET_MY_CALENDAR,
    _GET_CUSTOMER_CALENDAR,
    _GET_TRAVEL_POLICY,
    _SEARCH_FLIGHT,
    _SEARCH_HOTEL,
    _DELEGATE_BOOKING_PLAN,
    _DELEGATE_POLICY_REVIEW,
    _BOOK_FLIGHT,
    _BOOK_HOTEL,
    _SYNC_CALENDAR,
    _SEND_CONFIRMATION_EMAIL_V1,
    _REQUEST_HUMAN_APPROVAL,
)

# 包含草稿/历史版本的完整集合，仅用于生成 Skill Registry 文档（展示版本演进）。
ALL_SKILL_VERSIONS: tuple[Skill, ...] = ACTIVE_SKILLS + (_SEND_CONFIRMATION_EMAIL_V2_DRAFT,)

SKILL_BY_NAME: dict[str, Skill] = {s.name: s for s in ACTIVE_SKILLS}


def build_tools_schema() -> list[dict]:
    """main.py 用这个替代原来手写的 TOOLS 列表，schema 和权限元信息永远同源。"""
    return [s.to_tool_schema() for s in ACTIVE_SKILLS]


def wrap_with_permission_check(
    name: str, role: str, func: Callable[..., dict], **kwargs
) -> dict:
    """执行前置权限检查；被拒绝时不调用 func，返回统一的 permission_denied 结构。

    main.py 里 _process_tool_calls 直接用这个包一层，而不是自己重复写权限判断，
    保证运行时和 Eval（harness.py）用的是同一份权限逻辑。
    """
    skill = SKILL_BY_NAME.get(name)
    if not check_skill_permission(skill, role):
        return {
            "status": "permission_denied",
            "reason": (
                f"角色 '{role}' 无权调用 Skill '{name}'"
                f"（允许的角色：{list(skill.roles_allowed) if skill else '未知'}）。"
            ),
        }
    return func(**kwargs)
