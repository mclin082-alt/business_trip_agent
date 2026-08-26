"""Deterministic tests for governed multi-agent delegation and conflict handling."""

import asyncio
import pytest

import main

from multi_agent import (
    check_booking_execution,
    create_booking_proposal,
    create_policy_review,
)


HOTEL_CANDIDATES = [
    {
        "hotel_id": "HTL-A",
        "hotel_name": "浦东商务酒店",
        "price_per_night": 650,
        "within_policy": True,
    },
    {
        "hotel_id": "HTL-C",
        "hotel_name": "浦东香格里拉",
        "price_per_night": 1200,
        "within_policy": False,
    },
]


def _proposal(option_id: str, proposal_id: str = "PRP-1") -> dict:
    return create_booking_proposal(
        raw_output=(
            '{"recommended_option_id":"%s","rationale":"满足地点要求",'
            '"alternatives_considered":["HTL-A","HTL-C"],"risk_flags":[]}' % option_id
        ),
        booking_type="hotel",
        candidates=HOTEL_CANDIDATES,
        objective="选择客户附近酒店",
        constraints="靠近陆家嘴",
        delegation_id="DLG-BOOKING-1",
        proposal_id=proposal_id,
        source_search_seq=1,
    )


def _action_log() -> list[dict]:
    return [
        {
            "name": "search_hotel",
            "args": {"date": "下周二", "near": "陆家嘴"},
            "result": {"status": "success", "options": HOTEL_CANDIDATES},
        }
    ]


def test_runtime_policy_cannot_be_relaxed_by_policy_agent():
    proposal = _proposal("HTL-C")
    review = create_policy_review(
        raw_output=(
            '{"decision":"ALLOW","reason":"我认为客户距离更重要",'
            '"policy_ids":["POL-TRAVEL-2026-001"]}'
        ),
        proposal=proposal,
        delegation_id="DLG-POLICY-1",
        review_id="REV-1",
    )

    assert review["model_decision"] == "ALLOW"
    assert review["runtime_decision"] == "REQUIRE_APPROVAL"
    assert review["effective_decision"] == "REQUIRE_APPROVAL"
    assert review["conflict"] is True


def test_over_budget_proposal_requires_matching_proposal_scoped_approval():
    proposal = _proposal("HTL-C")
    review = create_policy_review(
        raw_output=(
            '{"decision":"REQUIRE_APPROVAL","reason":"超过酒店预算上限",'
            '"policy_ids":["POL-TRAVEL-2026-001","POL-TRAVEL-2026-003"]}'
        ),
        proposal=proposal,
        delegation_id="DLG-POLICY-1",
        review_id="REV-1",
    )
    proposals = {proposal["proposal_id"]: proposal}
    reviews = {proposal["proposal_id"]: review}

    blocked = check_booking_execution(
        booking_type="hotel",
        option_id="HTL-C",
        proposal_id="PRP-1",
        booking_proposals=proposals,
        policy_reviews=reviews,
        action_log=_action_log(),
        approval_state={},
    )
    assert blocked["allowed"] is False
    assert blocked["status"] == "approval_required"

    wrong_approval = check_booking_execution(
        booking_type="hotel",
        option_id="HTL-C",
        proposal_id="PRP-1",
        booking_proposals=proposals,
        policy_reviews=reviews,
        action_log=_action_log(),
        approval_state={"hotel_over_budget_approved_proposal_id": "PRP-OTHER"},
    )
    assert wrong_approval["allowed"] is False

    allowed = check_booking_execution(
        booking_type="hotel",
        option_id="HTL-C",
        proposal_id="PRP-1",
        booking_proposals=proposals,
        policy_reviews=reviews,
        action_log=_action_log(),
        approval_state={"hotel_over_budget_approved_proposal_id": "PRP-1"},
    )
    assert allowed["allowed"] is True
    assert allowed["review_id"] == "REV-1"


def test_new_search_invalidates_old_proposal_and_review():
    proposal = _proposal("HTL-A")
    review = create_policy_review(
        raw_output=(
            '{"decision":"ALLOW","reason":"价格在预算内",'
            '"policy_ids":["POL-TRAVEL-2026-001"]}'
        ),
        proposal=proposal,
        delegation_id="DLG-POLICY-1",
        review_id="REV-1",
    )
    action_log = _action_log() + _action_log()

    result = check_booking_execution(
        booking_type="hotel",
        option_id="HTL-A",
        proposal_id="PRP-1",
        booking_proposals={"PRP-1": proposal},
        policy_reviews={"PRP-1": review},
        action_log=action_log,
        approval_state={},
    )
    assert result["allowed"] is False
    assert result["status"] == "stale_proposal"


def test_policy_agent_can_be_stricter_and_veto_compliant_proposal():
    proposal = _proposal("HTL-A")
    review = create_policy_review(
        raw_output=(
            '{"decision":"DENY","reason":"候选资料存在无法验证的冲突",'
            '"policy_ids":["POL-TRAVEL-2026-001"]}'
        ),
        proposal=proposal,
        delegation_id="DLG-POLICY-1",
        review_id="REV-1",
    )
    assert review["runtime_decision"] == "ALLOW"
    assert review["effective_decision"] == "DENY"


def test_booking_subagent_cannot_invent_candidate_id():
    with pytest.raises(ValueError, match="outside the supplied candidate set"):
        _proposal("HTL-NOT-REAL")


def test_unauthorized_approver_cannot_unlock_proposal(monkeypatch):
    session = main.Session.new("预订超预算酒店", main.SYSTEM_PROMPT)
    session.pending_approval = {
        "call_id": "call-approval-1",
        "reason": "酒店价格超出预算",
        "proposed_action": "预订 HTL-C",
        "proposal_id": "PRP-1",
    }
    events = []

    async def send_event(event_type, **data):
        events.append({"type": event_type, **data})

    monkeypatch.setattr(main.session_store, "save_session", lambda *_: None)
    asyncio.run(
        main._resolve_pending_approval(
            session,
            {
                "kind": "approval",
                "approved": True,
                "approver_role": "employee",
                "user_note": "同意",
            },
            {},
            send_event,
            "run-1",
        )
    )

    assert "hotel_over_budget_approved_proposal_id" not in session.approval_state
    assert session.approval_records[-1]["requested_approved"] is True
    assert session.approval_records[-1]["approved"] is False
    assert "无权批准" in session.approval_records[-1]["resolution_error"]
    assert events[-1]["type"] == "approval_resolved"
    assert events[-1]["approved"] is False


def test_runtime_wiring_delegates_reviews_and_books(monkeypatch):
    session = main.Session.new("预订预算内酒店", main.SYSTEM_PROMPT)
    session.action_log.append(
        {
            "name": "search_hotel",
            "args": {"date": "下周二", "near": "陆家嘴"},
            "result": {"status": "success", "options": HOTEL_CANDIDATES},
        }
    )
    specialist_outputs = iter(
        [
            (
                '{"recommended_option_id":"HTL-A","rationale":"预算内且靠近客户",'
                '"alternatives_considered":["HTL-C"],"risk_flags":[]}',
                {"model": "fake", "total_tokens": 100},
            ),
            (
                '{"decision":"ALLOW","reason":"价格未超过政策上限",'
                '"policy_ids":["POL-TRAVEL-2026-001"]}',
                {"model": "fake", "total_tokens": 80},
            ),
        ]
    )
    monkeypatch.setattr(main, "_call_specialist", lambda _messages: next(specialist_outputs))
    tools = main._make_booking_tools(session)

    proposal = asyncio.run(
        tools["delegate_booking_plan"](
            booking_type="hotel",
            objective="选择客户附近酒店",
            constraints="预算优先",
        )
    )
    review = asyncio.run(tools["delegate_policy_review"](proposal_id=proposal["proposal_id"]))
    booking = tools["book_hotel"](
        hotel_id="HTL-A",
        proposal_id=proposal["proposal_id"],
    )

    assert proposal["status"] == "success"
    assert review["effective_decision"] == "ALLOW"
    assert booking["status"] == "success"
    assert booking["proposal_id"] == proposal["proposal_id"]
    assert len(session.delegation_records) == 2
    assert session.subagent_model_calls == 2


def test_authorized_scoped_approval_completes_over_budget_path(monkeypatch):
    session = main.Session.new("预订唯一可用的超预算酒店", main.SYSTEM_PROMPT)
    session.action_log.append(
        {
            "name": "search_hotel",
            "args": {"date": "下周二", "near": "陆家嘴"},
            "result": {"status": "success", "options": [HOTEL_CANDIDATES[1]]},
        }
    )
    specialist_outputs = iter(
        [
            (
                '{"recommended_option_id":"HTL-C","rationale":"唯一可用候选",'
                '"alternatives_considered":[],"risk_flags":["over_budget"]}',
                {"model": "fake", "total_tokens": 90},
            ),
            (
                '{"decision":"REQUIRE_APPROVAL","reason":"酒店超出预算",'
                '"policy_ids":["POL-TRAVEL-2026-001","POL-TRAVEL-2026-003"]}',
                {"model": "fake", "total_tokens": 70},
            ),
        ]
    )
    monkeypatch.setattr(main, "_call_specialist", lambda _messages: next(specialist_outputs))
    tools = main._make_booking_tools(session)
    proposal = asyncio.run(
        tools["delegate_booking_plan"](
            booking_type="hotel",
            objective="选择唯一可用酒店",
            constraints="无预算内候选",
        )
    )
    review = asyncio.run(tools["delegate_policy_review"](proposal_id=proposal["proposal_id"]))
    assert review["effective_decision"] == "REQUIRE_APPROVAL"

    before = tools["book_hotel"](hotel_id="HTL-C", proposal_id=proposal["proposal_id"])
    assert before["status"] == "approval_required"

    session.pending_approval = {
        "call_id": "call-approval-2",
        "reason": "酒店价格超出预算",
        "proposed_action": "预订 HTL-C",
        "proposal_id": proposal["proposal_id"],
    }

    async def ignore_event(_event_type, **_data):
        return None

    asyncio.run(
        main._resolve_pending_approval(
            session,
            {
                "kind": "approval",
                "approved": True,
                "approver_role": "manager",
                "user_note": "客户指定酒店",
            },
            tools,
            ignore_event,
            "run-2",
        )
    )
    after = tools["book_hotel"](hotel_id="HTL-C", proposal_id=proposal["proposal_id"])

    assert after["status"] == "success"
    assert session.approval_state["hotel_over_budget_approved_proposal_id"] == proposal["proposal_id"]
    assert session.approval_records[-1]["approver_role"] == "manager"
