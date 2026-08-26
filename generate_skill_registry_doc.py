"""生成 skills_registry.md —— Skill Registry 交付物。

Run: python3 generate_skill_registry_doc.py
"""

import json

from skills import ALL_SKILL_VERSIONS, ACTIVE_SKILLS


def _bool_cn(b: bool) -> str:
    return "是" if b else "否"


def _output_fields_summary(returns: dict) -> str:
    """把 returns schema 的 properties 收成一句 `字段: 类型` 摘要，塞进表格单元格。"""
    props = returns.get("properties", {})
    parts = []
    for key, spec in props.items():
        t = spec.get("type", "any")
        if isinstance(t, list):
            t = "/".join(t)
        parts.append(f"{key}: {t}")
    return "; ".join(parts) if parts else "(无)"


def render() -> str:
    lines = [
        "# Skill Registry",
        "",
        f"当前生效 Skill 数：{len(ACTIVE_SKILLS)}（另有 {len(ALL_SKILL_VERSIONS) - len(ACTIVE_SKILLS)} 个未接入运行时的版本演进草案，见文末）",
        "",
        "| Skill | 版本 | 允许角色 | 外部副作用 | 风险等级 | 输出字段（成功态） | 失败语义 |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in ACTIVE_SKILLS:
        lines.append(
            f"| `{s.name}` | {s.version} | {', '.join(s.roles_allowed)} | "
            f"{_bool_cn(s.side_effect)} | {s.risk_level} | {_output_fields_summary(s.returns)} | {s.on_failure} |"
        )

    lines += ["", "## 版本演进示例", "",
              "以下 Skill 存在草案版本，用于展示这套体系如何支持迭代/收紧权限，"
              "尚未接入运行时（不影响任何现有 Eval Case 的行为基线）：", ""]
    for s in ALL_SKILL_VERSIONS:
        if s not in ACTIVE_SKILLS:
            lines.append(f"- `{s.name}` {s.version}（取代 `{s.supersedes}`）：{s.description}")

    lines += ["", "## 输入/输出 Schema 详情", "",
              "以下按 Skill 逐个列出完整的输入 (`parameters`) 与成功态输出 (`returns`) JSON Schema，"
              "供接口对齐/契约测试直接引用：", ""]
    for s in ACTIVE_SKILLS:
        lines.append(f"### `{s.name}` ({s.version})")
        lines.append("")
        lines.append("输入 Schema：")
        lines.append("```json")
        lines.append(json.dumps(s.parameters, ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")
        lines.append("输出 Schema（成功态）：")
        lines.append("```json")
        lines.append(json.dumps(s.returns, ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    doc = render()
    with open("skills_registry.md", "w", encoding="utf-8") as f:
        f.write(doc)
    print(doc)