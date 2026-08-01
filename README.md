# Business Trip Execution Agent

> An AI agent that autonomously completes a business trip arrangement by dynamically planning, replanning from raw tool observations, requesting human approval before irreversible or out-of-policy actions, and verifying task completion against an explicit checklist — not a chatbot, not a workflow, not a function-calling demo.

差旅拜访只是这个 agent 承载的一个具体场景；它要证明的是一个更通用的能力：给一句目标，agent 自己规划、自己在失败后重新决策、该停下来问人的时候真的会停下来等你确认。

---

## Demo

*(截图 / GIF 占位 — 建议放三张：推理文字高亮的执行过程、人工审批卡片、最终报告)*

```
[ Screenshot 1: Live reasoning + tool call log ]
[ Screenshot 2: Human approval card ]
[ Screenshot 3: Execution layer / Decision layer final report ]
```

---

## Key Features

- **Dynamic Replanning** — 每次工具调用的失败原因都以原始数据（原始 JSON / 报错文本）交给模型，不做任何预判断；下一步怎么办完全是模型现场推理决定的，代码里没有一行 `if 航班满 then ...` 这种业务分支。
- **Explicit Verifier** — 任务是否完成由一份可勾选的 5 项 Checklist 决定（航班 / 酒店 / 会面时间 / 日历同步 / 确认邮件），而不是"流程跑完就算完成"这种隐式判断。
- **Human-in-the-loop Approval** — 超预算、或即将对客户发出不可撤回的外部动作（发确认邮件）之前，agent 会主动暂停，弹出审批卡片说明理由，等待用户批准或拒绝才继续。
- **Session Continuation** — 当模型因为缺信息而反问用户（而不是任务真正完成）时，会话状态和历史上下文会被正确保留，用户的后续回复会续进同一个任务，而不是丢失上下文重新开始。
- **Execution Layer vs. Decision Layer Reporting** — 任务结束时的总结报告会明确区分"有客观依据、可验证的事实"（订单号、确认号）和"需要人工复核的取舍判断"（为什么选了这个方案），而不是笼统地说"已完成"。

---

## Architecture

```
Goal（一句话，不给步骤）
   ↓
Plan（模型自主拆解任务）
   ↓
Action（调用工具执行）
   ↓
Observation（原始返回结果，成功或失败）
   ↓
Verifier（对照 Checklist 检查完成状态）
   ├─ 未满足 → 原始 Observation 喂回模型 → 模型重新 Plan → 回到 Action
   ├─ 需要人工判断 → 暂停等待 Human Approval → 通过后继续 Action
   └─ 全部满足 → 生成执行层/决策层报告 → Done
```

**Tech stack**: FastAPI + WebSocket backend for real-time streaming · single-page HTML/JS frontend · Qwen (OpenAI-compatible tool-use API) · 10 mock business tools + 1 approval tool, covering CRM lookup, calendar checks, travel policy, flight/hotel search & booking, calendar sync, and email confirmation.

---

## Quick Start

```bash
git clone <this-repo>
cd business_trip_agent
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

在项目根目录创建 `.env`，填入你的 Qwen API Key：

```
QWEN_API_KEY=your_key_here
```

运行：

```bash
python main.py
```

浏览器打开 `http://127.0.0.1:8000`，输入框已预填一个示例目标，点"开始任务"即可看到完整过程。

---

## Design

设计动机、Verifier 的具体判断逻辑、两个刻意设计的失败点（航班满员、酒店超预算）、以及为什么选择一个"成熟"场景而不是标新立异的场景，详见 [`DESIGN.md`](./DESIGN.md)。
