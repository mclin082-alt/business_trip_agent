"""
角色权限矩阵：把两条本来分散的权限轴合并成一张可审计的表。

轴 1（谁能调用哪个 Skill）—— 来自 skills.py 的 Skill.roles_allowed，
    是"静态调用权限"：intern 完全不能碰有副作用的工具。
轴 2（谁能审批哪类事项）—— 来自 policy_store.py 的
    rule_logic.approver_roles，是"动态审批权限"：即使 employee 能调用
    book_hotel 这个 Skill，超预算这一次具体调用仍然要 manager/travel_admin
    审批放行。

两条轴在代码里保持正交（跟 skills.py 顶部注释说的一致），但产品文档上
应该合并展示，否则面试官/审计者看不出"谁最终能拍板"。

用法：
    python role_matrix.py            # 打印 markdown 到 stdout
    python role_matrix.py --write    # 写入 docs/role_permission_matrix.md
"""

from __future__ import annotations

import os
import sys

import policy_store
from skills import ALL_ROLES, ACTIVE_SKILLS

_ROLE_LABELS = {
    "intern": "实习生 (intern)",
    "employee": "员工 (employee)",
    "manager": "直属主管 (manager)",
    "travel_admin": "差旅管理员 (travel_admin)",
}

_TOPIC_LABELS = {
    "hotel_over_budget_approval": "酒店超预算审批",
    "confirmation_email_approval": "客户确认邮件最终发送",
}


def _skill_call_matrix() -> str:
    lines = [
        "## 1. Skill 调用权限（静态）",
        "",
        "谁能直接调用某个 Skill；被拒绝时运行时返回 `permission_denied`，不会尝试执行任何副作用。",
        "",
        "| Skill | " + " | ".join(_ROLE_LABELS[r] for r in ALL_ROLES) + " |",
        "|---|" + "---|" * len(ALL_ROLES),
    ]
    for s in ACTIVE_SKILLS:
        row = [s.name]
        for role in ALL_ROLES:
            mark = "✅" if role in s.roles_allowed else "❌"
            if s.side_effect and mark == "✅":
                mark += "（有副作用）"
            row.append(mark)
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _approval_matrix() -> str:
    matrix = policy_store.role_permission_matrix()
    lines = [
        "",
        "## 2. 审批权限（动态，按当前生效政策）",
        "",
        "同一个角色即使能调用某个 Skill，具体某一次调用仍可能因触发政策而需要"
        "更高角色审批放行。下表标注\"谁是审批人 / 谁需要被别人审批\"。",
        "",
    ]
    for topic, row in matrix.items():
        policy = policy_store.lookup_policy(topic)
        label = _TOPIC_LABELS.get(topic, topic)
        lines.append(f"### {label}（依据 {policy['policy_id']} / {policy['version']}）")
        lines.append("")
        lines.append(f"> {policy['rule_text']}")
        lines.append("")
        lines.append("| 角色 | 权限 |")
        lines.append("|---|---|")
        for role in ("intern", "employee", "manager", "travel_admin"):
            status = row.get(role, "n/a")
            status_label = {
                "approver": "✅ 可审批",
                "requires_approval_from_others": "⏳ 需要上方角色审批后才能执行",
                "n/a": "— 不适用",
            }[status]
            lines.append(f"| {_ROLE_LABELS[role]} | {status_label} |")
        lines.append("")
    return "\n".join(lines)


def build_doc() -> str:
    header = (
        "# 角色权限矩阵\n\n"
        "自动生成，来源：`skills.py`（Skill 调用权限）+ "
        "`policy/travel_policy_v1.json`（审批权限）。\n"
        "重新生成：`python role_matrix.py --write`\n"
    )
    return header + "\n" + _skill_call_matrix() + "\n" + _approval_matrix()


if __name__ == "__main__":
    doc = build_doc()
    if "--write" in sys.argv:
        out_path = os.path.join(os.path.dirname(__file__), "docs", "role_permission_matrix.md")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(doc)
        print(f"已写入 {out_path}")
    else:
        print(doc)
