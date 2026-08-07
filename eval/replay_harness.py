"""
eval/replay_harness.py

离线重放模式：直接读取 eval_logs/*.jsonl（FakeWebSocket 落盘的 event stream），
重跑 harness.py 里的 check_xxx() / check_global_invariants() /
check_final_report_groundedness()，不重新调用 Agent、不消耗任何 LLM 配额。

用途：
- 改了某个 check_xxx() 的判定逻辑之后，想验证"新逻辑对历史轨迹的判断有没有
  变化"，不需要重新烧一遍 Agent 配额。
- 怀疑某次报告里的 pass/fail 是 harness 自己的 bug，而不是 Agent 的问题，
  可以直接对着落盘的原始轨迹反复调试 check 函数本身。
- 想批量跑一次"回归测试"：目录下所有历史日志重放一遍，看有没有 case 从
  pass 变成 fail（或反过来）。

用法：
    python eval/replay_harness.py                       # 重放 eval_logs/ 下全部日志
    python eval/replay_harness.py --dir some_other_dir   # 指定日志目录
    python eval/replay_harness.py --case happy_path      # 只重放某个 case
    python eval/replay_harness.py --judge                # 额外重跑 LLM judge（这个会花钱/花时间，默认关闭）

已知限制：
    check_session_continuation_after_reject(_with_note) 需要 messages_snapshots
    （每轮请求前 messages 的快照）来检查"拒绝后上下文有没有丢失"，但这份数据
    没有被 FakeWebSocket 落盘，.jsonl 里没有。重放时这一项检查会被跳过并在
    结果里标注，而不是拿空列表硬跑出一个错误的 fail。
"""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

# 让脚本既可以放在 eval/ 目录下跟 harness.py 平级运行，
# 也可以从仓库根目录直接 python eval/replay_harness.py 运行。
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness  # noqa: E402  同目录下的 eval/harness.py
import main as main_module  # noqa: E402  仓库根目录下的 main.py（仅用于 judge 模式）


# ------------------------------------------------------------------
# 1. 加载 jsonl
# ------------------------------------------------------------------
def load_events(path: Path) -> list[dict]:
    events = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name} 第 {line_no} 行不是合法 JSON: {exc}") from exc
    return events


# ------------------------------------------------------------------
# 2. 从文件名解析 case_id / run_idx
#
# 兼容两种命名习惯：
#   - "happy_path#0.jsonl"           （FakeWebSocket 默认写法，tag 里用 #）
#   - "happy_path_0.jsonl"           （某些文件系统/上传流程会把 # 换成 _）
# 优先按最后一个分隔符（# 或 _ 后面紧跟数字）切分，取不到就把整个 stem 当
# case_id、run_idx 记为 0（多数是单 run 的 case，比如 tool_abstraction_contract）。
# ------------------------------------------------------------------
_NAME_RE = re.compile(r"^(?P<case_id>.+?)[#_](?P<run_idx>\d+)$")


def parse_filename(path: Path) -> tuple[str, int]:
    stem = path.stem
    m = _NAME_RE.match(stem)
    if m:
        return m.group("case_id"), int(m.group("run_idx"))
    return stem, 0


# ------------------------------------------------------------------
# 3. 找到对应的 case 定义（复用 harness.CASES，保证判据跟真实跑法一致）
# ------------------------------------------------------------------
def find_case(case_id: str) -> dict | None:
    for c in harness.CASES:
        if c["id"] == case_id:
            return c
    return None


# ------------------------------------------------------------------
# 4. 对一份 events 跑一次完整的离线评测
# ------------------------------------------------------------------
async def evaluate_events(events: list[dict], case: dict, run_judge: bool = False) -> dict:
    case_id = case["id"]

    if case_id == "tool_abstraction_contract":
        # 这个 case 的 check 函数吃的是 main_module 本身而不是 events，
        # 跟事件流无关，重放它没有意义，直接跳过。
        return {
            "case_id": case_id,
            "skipped_reason": "tool_abstraction_contract 检查的是 main.py 的工具 schema，"
                               "不依赖 event stream，重放模式下没有意义，请直接跑 harness.py。",
        }

    terminal = harness.terminal_event(events)
    if terminal == "infra_error":
        return {"case_id": case_id, "infra_error": True}

    warnings = []

    if case_id.startswith("session_continuation_after_reject"):
        variant = case["variant"]
        # messages_snapshots 没有被落盘，用空列表重放，然后把因此必然触发的
        # "看不到客户信息，疑似上下文丢失" 这条误报从结果里剔除，改成显式警告，
        # 而不是让它悄悄把这次重放判成 fail。
        det = case["check"](events, [], variant)
        stripped_reason = "拒绝后的模型调用中看不到此前客户信息，疑似上下文丢失"
        if stripped_reason in det["reasons"]:
            det = {
                "pass": len(det["reasons"]) == 1,  # 如果就这一条误报，剔除后重新判 pass
                "reasons": [r for r in det["reasons"] if r != stripped_reason],
            }
            warnings.append(
                "messages_snapshots 在 replay 模式下不可用，"
                "'上下文是否丢失' 这一项本次未被检查（已从 reasons 中剔除）。"
            )
    else:
        det = case["check"](events)

    invariants = harness.check_global_invariants(events)
    report_check = harness.check_final_report_groundedness(events)
    if not report_check.get("not_applicable") and not report_check["pass"]:
        invariants = {
            "pass": False,
            "reasons": invariants["reasons"] + report_check["reasons"],
        }

    judged = None
    if run_judge and case.get("llm_judge_rubric"):
        judged = await harness.run_llm_judge(main_module, events, case["llm_judge_rubric"])

    return {
        "case_id": case_id,
        "infra_error": False,
        "deterministic": det,
        "invariants": invariants,
        "judged": judged,
        "warnings": warnings,
    }


# ------------------------------------------------------------------
# 5. 批量重放目录下所有日志，输出跟 harness.render_report 风格一致的表格
# ------------------------------------------------------------------
async def replay_dir(log_dir: Path, only_case: str | None, run_judge: bool) -> str:
    files = sorted(log_dir.glob("*.jsonl"))
    if only_case:
        files = [f for f in files if parse_filename(f)[0] == only_case]

    if not files:
        return f"# Replay Run\n\n（在 {log_dir} 下没有找到匹配的 .jsonl 文件）"

    grouped: dict[str, list[tuple[int, dict]]] = {}
    for f in files:
        case_id, run_idx = parse_filename(f)
        try:
            events = load_events(f)
        except ValueError as exc:
            grouped.setdefault(case_id, []).append((run_idx, {"case_id": case_id, "load_error": str(exc)}))
            continue

        case = find_case(case_id)
        if case is None:
            grouped.setdefault(case_id, []).append(
                (run_idx, {"case_id": case_id, "load_error": f"CASES 里找不到 id={case_id}，跳过"})
            )
            continue

        result = await evaluate_events(events, case, run_judge=run_judge)
        grouped.setdefault(case_id, []).append((run_idx, result))

    lines = [
        "# Replay Run（离线重放，未调用任何 LLM）", "",
        "| Case | 确定性层 | 语义层 | 全局不变量 | 跳过/异常 |",
        "|---|---|---|---|---|",
    ]
    detail_lines = []

    for case_id, run_results in grouped.items():
        run_results.sort(key=lambda x: x[0])
        results = [r for _, r in run_results]

        load_errors = [r for r in results if r.get("load_error")]
        skipped = [r for r in results if r.get("skipped_reason")]
        infra = [r for r in results if r.get("infra_error")]
        normal = [r for r in results if not r.get("load_error") and not r.get("skipped_reason") and not r.get("infra_error")]

        det_pass = sum(1 for r in normal if r["deterministic"]["pass"])
        det_total = len(normal)
        det_str = f"{det_pass}/{det_total}" if det_total else "—"

        judged_runs = [r for r in normal if r.get("judged") is not None]
        if judged_runs:
            jp = sum(1 for r in judged_runs if r["judged"].get("pass") is True)
            jf = sum(1 for r in judged_runs if r["judged"].get("pass") is False)
            ju = sum(1 for r in judged_runs if r["judged"].get("pass") is None)
            judge_str = f"{jp}✅/{jf}❌/{ju}❓"
        else:
            judge_str = "—"

        inv_pass = sum(1 for r in normal if r["invariants"]["pass"])
        inv_total = len(normal)
        inv_str = f"{inv_pass}/{inv_total}" if inv_total else "—"

        skip_str = f"{len(load_errors) + len(skipped) + len(infra)}/{len(results)}" if (load_errors or skipped or infra) else "—"

        lines.append(f"| {case_id} | {det_str} | {judge_str} | {inv_str} | {skip_str} |")

        for r in load_errors:
            detail_lines.append(f"- 💥 `{case_id}`: {r['load_error']}")
        for r in skipped:
            detail_lines.append(f"- ⏭️ `{case_id}`: {r['skipped_reason']}")
        for r in infra:
            detail_lines.append(f"- 🔌 `{case_id}`: 原始日志终态为 infra_error，跳过评测。")
        for r in normal:
            for w in r.get("warnings", []):
                detail_lines.append(f"- ℹ️ `{case_id}`: {w}")
            if not r["deterministic"]["pass"]:
                detail_lines.append(f"- ⚠️ `{case_id}` 确定性断言失败: {r['deterministic']['reasons']}")
            if not r["invariants"]["pass"]:
                detail_lines.append(f"- ⚠️ `{case_id}` 全局不变量失败: {r['invariants']['reasons']}")

    return "\n".join(lines + [""] + detail_lines)


def main():
    parser = argparse.ArgumentParser(description="离线重放 eval_logs/*.jsonl，不调用任何 LLM")
    parser.add_argument("--dir", default="eval_logs", help="日志目录，默认 eval_logs")
    parser.add_argument("--case", default=None, help="只重放指定 case_id，默认重放全部")
    parser.add_argument("--judge", action="store_true", help="额外重跑 LLM judge（会真的调用模型，默认关闭）")
    args = parser.parse_args()

    report = asyncio.run(replay_dir(Path(args.dir), args.case, args.judge))
    print(report)


if __name__ == "__main__":
    main()
