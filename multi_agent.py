"""Governed multi-agent contracts for the business-trip runtime.

The top-level Trip Orchestrator remains the only user-facing agent. Booking
and Policy/Compliance are bounded specialists invoked as tools. Their model
outputs are typed and auditable, while deterministic runtime policy remains
the final authority for side effects.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

import policy_store


ORCHESTRATOR_AGENT = "trip_orchestrator"
BOOKING_AGENT = "booking_subagent"
POLICY_AGENT = "policy_compliance_subagent"

DELEGATION_TOOL_AGENTS = {
    "delegate_booking_plan": BOOKING_AGENT,
    "delegate_policy_review": POLICY_AGENT,
}

Decision = Literal["ALLOW", "REQUIRE_APPROVAL", "DENY"]


class BookingAgentOutput(BaseModel):
    """Structured output produced by the Booking Subagent."""

    recommended_option_id: str
    rationale: str = Field(min_length=1)
    alternatives_considered: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)


class PolicyAgentOutput(BaseModel):
    """Advisory verdict produced by the Policy/Compliance Subagent."""

    decision: Decision
    reason: str = Field(min_length=1)
    policy_ids: list[str] = Field(default_factory=list)


_DECISION_SEVERITY = {"ALLOW": 0, "REQUIRE_APPROVAL": 1, "DENY": 2}


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def extract_json_object(raw_text: str) -> dict:
    """Parse a JSON object, tolerating Markdown fences or surrounding prose."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("specialist output does not contain a JSON object")
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("specialist output must be a JSON object")
    return value


def _candidate_id(booking_type: str, candidate: dict) -> str | None:
    return candidate.get("flight_id" if booking_type == "flight" else "hotel_id")


def _candidate_price(booking_type: str, candidate: dict) -> float | None:
    value = candidate.get("price" if booking_type == "flight" else "price_per_night")
    return float(value) if isinstance(value, (int, float)) else None


def create_booking_proposal(
    *,
    raw_output: str,
    booking_type: Literal["flight", "hotel"],
    candidates: list[dict],
    objective: str,
    constraints: str,
    delegation_id: str,
    proposal_id: str,
    source_search_seq: int,
    model_usage: dict | None = None,
) -> dict:
    """Validate specialist output and bind it to an immutable candidate snapshot."""
    try:
        output = BookingAgentOutput.model_validate(extract_json_object(raw_output))
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Booking Subagent output: {exc}") from exc

    candidate_by_id = {
        option_id: candidate
        for candidate in candidates
        if (option_id := _candidate_id(booking_type, candidate))
    }
    selected = candidate_by_id.get(output.recommended_option_id)
    if selected is None:
        raise ValueError(
            "Booking Subagent recommended an option outside the supplied candidate set: "
            f"{output.recommended_option_id}"
        )

    snapshot = {
        "booking_type": booking_type,
        "candidates": candidates,
        "objective": objective,
        "constraints": constraints,
    }
    return {
        "status": "success",
        "proposal_id": proposal_id,
        "delegation_id": delegation_id,
        "agent": BOOKING_AGENT,
        "booking_type": booking_type,
        "recommended_option_id": output.recommended_option_id,
        "selected_candidate": dict(selected),
        "estimated_cost": _candidate_price(booking_type, selected),
        "rationale": output.rationale,
        "alternatives_considered": output.alternatives_considered,
        "risk_flags": output.risk_flags,
        "source_candidate_ids": sorted(candidate_by_id),
        "source_search_seq": source_search_seq,
        "context_digest": _canonical_digest(snapshot),
        "model_usage": model_usage or {},
    }


def deterministic_policy_decision(proposal: dict) -> tuple[Decision, str, list[dict]]:
    """Compute the non-negotiable runtime policy floor for one proposal."""
    booking_type = proposal.get("booking_type")
    selected = proposal.get("selected_candidate") or {}
    price = _candidate_price(booking_type, selected)
    if price is None:
        return "DENY", "候选缺少可验证价格，fail closed。", []

    if booking_type == "hotel":
        budget_ref = policy_store.get_policy_ref("hotel_budget")
        approval_ref = policy_store.get_policy_ref("hotel_over_budget_approval")
        limit, _ = policy_store.get_rule_limit("hotel_budget")
        refs = [ref for ref in (budget_ref, approval_ref) if ref]
        if limit is None:
            return "DENY", "酒店预算政策缺失，不能执行有资金副作用的预订。", refs
        if price <= limit:
            return "ALLOW", f"酒店价格 {price:g} 未超过政策上限 {limit:g}。", refs
        return (
            "REQUIRE_APPROVAL",
            f"酒店价格 {price:g} 超过政策上限 {limit:g}，必须由授权角色审批。",
            refs,
        )

    if booking_type == "flight":
        budget_ref = policy_store.get_policy_ref("flight_budget")
        limit, _ = policy_store.get_rule_limit("flight_budget")
        refs = [budget_ref] if budget_ref else []
        if limit is None:
            return "DENY", "机票预算政策缺失，不能执行有资金副作用的预订。", refs
        if price <= limit:
            return "ALLOW", f"机票价格 {price:g} 未超过政策上限 {limit:g}。", refs
        # The current product has no durable flight-over-budget approval
        # purpose. Fail closed instead of silently reusing the hotel approval.
        return "DENY", "机票超预算，但当前 runtime 尚未配置对应的可审计审批路径。", refs

    return "DENY", f"未知预订类型 {booking_type!r}。", []


def create_policy_review(
    *,
    raw_output: str,
    proposal: dict,
    delegation_id: str,
    review_id: str,
    model_usage: dict | None = None,
) -> dict:
    """Reconcile Policy-agent advice with deterministic runtime policy.

    The model is allowed to be stricter than code, but can never relax the
    deterministic result. This makes a disagreement visible without making a
    stochastic model the sole authority for spending or external side effects.
    """
    try:
        output = PolicyAgentOutput.model_validate(extract_json_object(raw_output))
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Policy Subagent output: {exc}") from exc

    runtime_decision, runtime_reason, policy_refs = deterministic_policy_decision(proposal)
    effective_decision = max(
        (output.decision, runtime_decision),
        key=lambda value: _DECISION_SEVERITY[value],
    )
    if output.decision == "REQUIRE_APPROVAL" and runtime_decision == "ALLOW":
        # A model may discover a risk, but it cannot invent a new approval
        # purpose/approver policy at runtime. Until such a path exists as data,
        # the safe effective result is DENY, not an unauditable approval.
        effective_decision = "DENY"
        effective_reason = (
            "Policy Subagent 要求升级审批，但当前 Policy Store 没有对应的审批用途与"
            "授权角色；为避免模型临时创造授权，按 DENY 处理。"
        )
    elif _DECISION_SEVERITY[output.decision] > _DECISION_SEVERITY[runtime_decision]:
        effective_reason = output.reason
    else:
        effective_reason = runtime_reason
    valid_policy_ids = {ref["policy_id"] for ref in policy_refs}
    cited_policy_ids = sorted(set(output.policy_ids) & valid_policy_ids)
    return {
        "status": "success",
        "review_id": review_id,
        "delegation_id": delegation_id,
        "agent": POLICY_AGENT,
        "proposal_id": proposal["proposal_id"],
        "proposal_context_digest": proposal["context_digest"],
        "model_decision": output.decision,
        "runtime_decision": runtime_decision,
        "effective_decision": effective_decision,
        "conflict": output.decision != runtime_decision,
        "model_reason": output.reason,
        "runtime_reason": runtime_reason,
        "effective_reason": effective_reason,
        "policy_ids": cited_policy_ids or sorted(valid_policy_ids),
        "policy_refs": policy_refs,
        "model_usage": model_usage or {},
    }


def current_search_sequence(action_log: list[dict], booking_type: str) -> int:
    search_name = "search_flight" if booking_type == "flight" else "search_hotel"
    return sum(
        1
        for item in action_log
        if item.get("name") == search_name
        and (item.get("result") or {}).get("status") == "success"
    )


def check_booking_execution(
    *,
    booking_type: Literal["flight", "hotel"],
    option_id: str,
    proposal_id: str,
    booking_proposals: dict[str, dict],
    policy_reviews: dict[str, dict],
    action_log: list[dict],
    approval_state: dict,
) -> dict:
    """Validate delegation, freshness, policy review and scoped approval."""
    proposal = booking_proposals.get(proposal_id)
    if proposal is None:
        return {
            "allowed": False,
            "status": "delegation_required",
            "reason": "预订前必须先取得 Booking Subagent 的有效 proposal_id。",
        }
    if proposal.get("booking_type") != booking_type:
        return {
            "allowed": False,
            "status": "proposal_mismatch",
            "reason": f"proposal {proposal_id} 不属于 {booking_type} 预订。",
        }
    if proposal.get("recommended_option_id") != option_id:
        return {
            "allowed": False,
            "status": "proposal_mismatch",
            "reason": (
                f"拟执行选项 {option_id} 与 proposal 推荐的 "
                f"{proposal.get('recommended_option_id')} 不一致；请重新 delegation。"
            ),
        }

    latest_search_seq = current_search_sequence(action_log, booking_type)
    if proposal.get("source_search_seq") != latest_search_seq:
        return {
            "allowed": False,
            "status": "stale_proposal",
            "reason": "proposal 生成后候选搜索结果已更新，必须重新比较并复核。",
        }

    review = policy_reviews.get(proposal_id)
    if review is None or review.get("proposal_context_digest") != proposal.get("context_digest"):
        return {
            "allowed": False,
            "status": "policy_review_required",
            "reason": "执行前必须由 Policy/Compliance Subagent 复核同一版本的 proposal。",
        }

    decision = review.get("effective_decision")
    if decision == "DENY":
        return {
            "allowed": False,
            "status": "policy_denied",
            "reason": review.get("effective_reason") or review.get("runtime_reason"),
            "review_id": review.get("review_id"),
        }
    if decision == "REQUIRE_APPROVAL":
        approved_proposal_id = approval_state.get("hotel_over_budget_approved_proposal_id")
        if booking_type != "hotel" or approved_proposal_id != proposal_id:
            return {
                "allowed": False,
                "status": "approval_required",
                "reason": "Policy review 要求对这个 proposal 进行 proposal-scoped 人工审批。",
                "review_id": review.get("review_id"),
            }

    return {
        "allowed": True,
        "status": "success",
        "proposal_id": proposal_id,
        "review_id": review.get("review_id"),
        "effective_decision": decision,
    }


def booking_agent_messages(
    *,
    goal: str,
    booking_type: str,
    candidates: list[dict],
    objective: str,
    constraints: str,
    supporting_context: dict,
) -> list[dict]:
    """Build a minimal context projection for the Booking Subagent."""
    payload = {
        "goal": goal,
        "booking_type": booking_type,
        "objective": objective,
        "constraints": constraints,
        "candidates": candidates,
        "supporting_context": supporting_context,
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 Booking Subagent。只比较给定候选并提出一个可审计 proposal；"
                "你没有预订、审批或修改外部系统的权限。只能推荐候选列表中真实存在的 ID。"
                "优先满足时间、可用性和明确约束；酒店存在合规候选时优先合规候选。"
                "只输出 JSON object，字段为 recommended_option_id、rationale、"
                "alternatives_considered、risk_flags。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def policy_agent_messages(*, proposal: dict) -> list[dict]:
    """Build a policy-only context projection for the Policy Subagent."""
    policy_records = [
        policy
        for policy in policy_store.all_policies()
        if policy.get("topic") in {
            "hotel_budget",
            "hotel_over_budget_approval",
            "flight_budget",
        }
    ]
    compact_proposal = {
        key: value
        for key, value in proposal.items()
        if key not in {"model_usage", "alternatives_considered"}
    }
    payload = {"proposal": compact_proposal, "policy_records": policy_records}
    return [
        {
            "role": "system",
            "content": (
                "你是 Policy/Compliance Subagent。你只做合规复核，不选择旅行体验偏好，"
                "也不能执行预订或批准自己的请求。依据给定 policy_records 输出 ALLOW、"
                "REQUIRE_APPROVAL 或 DENY。酒店超预算应为 REQUIRE_APPROVAL；政策缺失、"
                "候选不可验证或不存在可审计审批路径时应 DENY。只输出 JSON object，"
                "字段为 decision、reason、policy_ids。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
