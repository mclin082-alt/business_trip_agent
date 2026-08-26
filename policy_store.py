"""
政策检索层（结构化 JSON + 关键词/字段匹配，故意不接向量库）。

对应"对齐路径"第二阶段的要求：
  - 事实可引用：每次查询返回 policy_id + version + rule_text 原文，而不是
    裸的数字，模型和最终报告都可以引用这个 ID 作为依据。
  - 规则可执行：rule_logic 是结构化字段（如 max_amount/limit），审批相关
    代码（approval_utils.py / main.py）直接读这个字段做判断，跟 rule_text
    保证同源，不会出现"文档一套、代码判断另一套"的漂移。

main.py 里的 get_travel_policy 工具、book_hotel 的超预算判断，都应该改成
调用这个模块，而不是继续读 MOCK_DATA 里的裸常量。
"""

from __future__ import annotations

import json
import os
from typing import Any

_POLICY_FILE = os.path.join(os.path.dirname(__file__), "policy", "travel_policy_v1.json")

_cache: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _cache
    if _cache is None:
        with open(_POLICY_FILE, encoding="utf-8") as f:
            _cache = json.load(f)
    return _cache


def reload_policies() -> None:
    """测试/热更新用：清空缓存，下次调用重新读文件。"""
    global _cache
    _cache = None


def all_policies() -> list[dict]:
    return list(_load()["policies"])


def lookup_policy(topic: str) -> dict | None:
    """按 topic 精确查找一条政策（当前版本的生效政策）。

    topic 例如："hotel_budget" / "flight_budget" /
    "hotel_over_budget_approval" / "confirmation_email_approval"
    """
    for p in all_policies():
        if p["topic"] == topic:
            return p
    return None


def search_policies(keyword: str) -> list[dict]:
    """简单关键词检索：在 title/rule_text/topic 里做子串匹配。
    用于"知识库"里模型可能用自然语言问政策的场景（比如"酒店预算是多少"）。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return all_policies()
    hits = []
    for p in all_policies():
        haystack = f"{p['title']} {p['rule_text']} {p['topic']}"
        if keyword in haystack:
            hits.append(p)
    return hits


def get_policy_ref(topic: str) -> dict | None:
    """给"可引用"用的精简结构：policy_id + version + effective_date + rule_text。
    这是要拼进模型可见结果、以及审批记录里的那部分。
    """
    p = lookup_policy(topic)
    if p is None:
        return None
    return {
        "policy_id": p["policy_id"],
        "version": p["version"],
        "effective_date": p["effective_date"],
        "rule_text": p["rule_text"],
    }


def get_rule_limit(topic: str) -> tuple[float | None, str | None]:
    """取某条政策 rule_logic 里的 (limit, policy_id)，供运行时判断直接使用，
    保证判断阈值和政策文本永远读同一份数据源。
    """
    p = lookup_policy(topic)
    if p is None:
        return None, None
    limit = p.get("rule_logic", {}).get("limit")
    return limit, p.get("policy_id")


def get_approver_roles(topic: str) -> tuple[str, ...]:
    """取某条审批类政策允许的审批角色，供角色权限矩阵和审批流程复用。"""
    p = lookup_policy(topic)
    if p is None:
        return ()
    roles = p.get("rule_logic", {}).get("approver_roles", [])
    return tuple(roles)


def is_valid_policy_id(policy_id: str) -> bool:
    """某个 policy_id 是否真实存在——供"报告引用了不存在的政策"这类幻觉检测复用，
    用法和 approval_utils.find_fabricated_ids 对订单号做的事情是同一个模式。
    """
    return any(p["policy_id"] == policy_id for p in all_policies())


def role_permission_matrix() -> dict[str, dict[str, str]]:
    """生成"角色 × 审批类政策"的权限矩阵，供 role_matrix.py 渲染文档用。

    返回 {topic: {role: "approver" | "requires_approval_from_others" | "n/a"}}
    这里只覆盖审批类（approval_required）政策，预算类政策不涉及角色维度。
    """
    matrix: dict[str, dict[str, str]] = {}
    for p in all_policies():
        logic = p.get("rule_logic", {})
        if logic.get("type") != "approval_required":
            continue
        approver_roles = set(logic.get("approver_roles", []))
        row: dict[str, str] = {}
        for role in ("intern", "employee", "manager", "travel_admin"):
            if role not in p.get("applies_to", []):
                row[role] = "n/a"
            elif role in approver_roles:
                row[role] = "approver"
            else:
                row[role] = "requires_approval_from_others"
        matrix[p["topic"]] = row
    return matrix
