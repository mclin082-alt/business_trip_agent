# Design Document — Business Trip Execution Agent

> This document explains *why* the system is designed the way it is — not how to run it (see `README.md`), and not a changelog. Chapters 1–8 describe the final system as it exists today; Chapter 9 explains how several specific design decisions changed during implementation, and why.

---

## 目录

1. [Goal](#1-goal)
2. [Core Design Principles](#2-core-design-principles)
3. [Runtime Architecture](#3-runtime-architecture)
4. [Verifier](#4-verifier)
5. [Tool Design](#5-tool-design)
6. [Conversation Lifecycle & Resumability](#6-conversation-lifecycle--resumability)
7. [Final Report](#7-final-report)
8. [Evaluation Methodology](#8-evaluation-methodology)
9. [Design Evolution](#9-design-evolution)
10. [Engineering Trade-offs](#10-engineering-trade-offs)
11. [Future Work](#11-future-work)

---

## 1. Goal

这不是一个差旅规划工具（travel planner）。

给定一句话目标——"下周二安排去上海拜访客户，把事情办妥"——系统需要自主拆解任务、调用工具执行、在执行失败时读取原始返回信息现场重新决策（Replan）、在超预算或面向客户的不可逆动作前主动暂停等待人工批准，并且只有在对照一份显式的 Checklist 全部满足之后才会判定任务完成。整个过程中的暂停不是"卡在内存里等"，而是真正落盘、可以跨进程恢复的状态。

差旅场景本身只是载体。这个项目要证明的命题是：**Agent 和"套了一层 LLM 的自动化脚本"之间只有一条分界线**——下一步该做什么，是模型当场读取执行结果推理出来的，还是工程师提前写好的 if-else 分支。这条红线贯穿本文档接下来的所有设计决策。

---

## 2. Core Design Principles

### 2.1 The red line

> 下一步该做什么，是模型现场读取 Observation 推理出来的，还是代码里预先写好的分支？

前者是 Agent，后者哪怕流程再复杂，本质上仍然是 Workflow 换了一层 LLM 的皮。本项目从工具设计到 Runtime 的每一个环节都以此为唯一标准取舍。

### 2.2 Goal / Tools / Verifier

一个可信的 Agent demo 需要同时体现三要素：

- **Goal**：一句话给出，不给步骤，让"规划"这件事真实存在
- **Tools**：一组真实的外部系统接口（此处为受控 mock，见 §10）
- **Verifier**：一份显式的、可勾选的 Checklist，而不是"流程跑完 = 完成"这种隐式判断

### 2.3 能力定义与运行状态分离

"这个 Agent 能做什么"（system prompt、工具 schema、工具怎么实现）和"这一次具体跑到哪了"（对话历史、Checklist 完成度、已下的订单、审批记录……）是两类完全不同性质的东西——前者在一个进程里只有一份、不随任务变化，后者每一次任务都是独立的、需要能被单独保存和恢复。本项目用 `AgentDefinition` 和 `Session` 两个类显式表达这条边界（§3.1），这也是 §6 描述的持久化/可重入能力能够成立的前提：只有先把"要保存什么"从"这个 Agent 是谁"里干净地切出来，"保存下来再读回去"才是一件有明确边界的事情。

### 2.4 为什么是这样一个"成熟"场景

商务出差协调这类场景在 Agent 领域已经被反复展示过，这是事实，不回避。但选择它的判断标准不是"这是不是个新颖点子"，而是它对应了企业内部真实存在的效率问题（差旅审批、跨系统信息核对），是合理的内部提效方向。更重要的是：**场景越成熟，越不需要花力气证明"这是不是个问题"，所有展示精力都能集中在设计判断本身上**——一个陌生场景反而会让评审者的注意力停留在"这靠谱吗"，而不是"这个人对 Agent 本质的理解到不到位"。这是一个刻意的取舍，详见 §10。

| | 旅游规划类工具 | 本项目 |
|---|---|---|
| 目标性质 | 生成推荐，无强制约束 | 必须办成一件事，有硬性业务约束 |
| 失败的意义 | 不存在真正的"失败" | 订不到票、超预算、时间冲突是真实存在的失败，会改变后续路径 |
| 下一步决策来源 | 通常是重新生成一版推荐 | 模型读取失败的原始信息，现场推理应对方案 |
| 终止条件 | 输出一份 itinerary 即结束 | 对照 Checklist，全部满足才算完成 |

---

## 3. Runtime Architecture

```
Goal（一句话，不给步骤）
   ↓
Plan（模型自主拆解，现场生成）
   ↓
Action（调用工具执行）
   ↓
Observation（原始返回结果，可能成功也可能失败）
   ↓
Verifier Check（对照 Checklist）
   ├─ 未满足 → 原始 Observation 喂回模型 → Replan → 回到 Action
   ├─ 需要人工判断 → 暂停，Session 落盘 → 等待 Human Approval → 读回 Session 继续 Action
   └─ 全部满足 → 生成最终报告 → Session 落盘（DONE）
```

三条贯穿始终的工程约束：

1. **Observation 必须以原始返回体的形式喂给模型**，代码不预先判断"这是什么类型的失败"——判断失败类型和决定应对方式，都是模型的职责，不是代码的职责。
2. **代码里不存在任何处理失败的 if-else 业务分支**（例如 `if 航班满 then 搜下一班`）。"失败之后怎么办"完全由模型在 Prompt 中现场推理给出，代码只执行模型选定的下一步工具调用。
3. **模型的推理文字（reasoning）必须在界面上展示**，而不是只展示"重试成功"这样的结果性文字。这段文字本身就是"这是决策而不是查表"的直接证据。

### 3.1 两个积木：`AgentDefinition` 与 `Session`

早期实现里，"这个 Agent 能做什么"和"这一次具体跑到哪了"混在同一个函数体（`run_agent_loop`）的局部变量里。这在只考虑"一次运行从头跑到尾"时没问题，但一旦要支持"跑到一半暂停、之后换一个上下文继续跑"，这种混合就成了阻碍——你没法只把"跑到哪了"这部分单独搬出去序列化，因为它和"这个 Agent 是谁"缠在一起。

现在拆成两个独立的类：

- **`AgentDefinition`**：`system_prompt` + `tools_schema` + `make_tool_functions`（工具函数工厂）。这是静态的能力定义，一个进程里只有一份（`AGENT` 单例），不随具体某一次任务变化。
- **`Session`**（`@dataclasses.dataclass`）：`messages` / `checklist` / `approval_state` / `action_log` / `approval_records` / `booked_flights` / `booked_hotels` / `lifecycle_state` / `model_calls` / `seen_readonly_calls` / `goal` / `pending_approval` / `pending_tool_calls`——一次具体任务从开始到当前时刻的**全部**运行时状态，理论上"把这个对象存下来，再读回来，就能无损地从原来的地方继续"。

这个拆分本身不引入新行为，纯粹是给持久化铺路——`AgentDefinition` 天然不需要持久化（重启进程会重新构造出同一份），需要持久化的只有 `Session`，拆开之后持久化范围一目了然。

**实现笔记**：为了避免循环 import（`main.py` 在模块加载时就 `import session_store`，而 `Session` 类本身定义在 `main.py` 里），`session_store.py` 只在函数体内部（`load_session`）延迟 `from main import Session`，且只用于反序列化时重建对象，不在模块顶层引入。工具函数（`make_tool_functions(session)`）也从"直接读写 `run_agent_loop` 的局部变量"改成了"读写传入的 `session` 对象的字段"，因为工具执行（比如 `book_flight` 判断是否重复下单）现在需要感知的是可能被持久化-恢复过的状态，而不是这一次函数调用里全新初始化的局部变量。

`run_agent_loop` 内部并没有把函数体逐行地从局部变量替换成 `session.xxx`——而是在函数最开头做一次 `checklist = session.checklist` 这样的别名绑定，函数体其余部分基本不变（这是同一个字典对象的两个名字，互相同步，不是拷贝）。这是刻意的最小化 diff：语义上完全等价于全量替换，但改动范围小得多、审查起来风险更低。

---

## 4. Verifier

任务是否完成由一份显式 Checklist 决定，而不是"N 个步骤跑完了"这种隐式判断：

```
☐ 航班已确认
☐ 酒店已确认
☐ 客户会面时间已确认
☐ 日历事件已同步
☐ 确认邮件已发送
```

只要还有一项未满足，Agent 不停止，继续 Plan → Action → Observation 循环；全部满足后才输出最终报告、状态变为 Done。界面上实时显示这份 Checklist 的勾选状态。

酒店超预算这条安全底线不完全交给 Verifier 事后核对，而是在 `book_hotel` 执行前就拦截：`approval_utils.check_policy_boundary(price, policy_limit)` 判断是否超出政策上限，`approval_utils.check_hotel_approval(approval_state)` 判断是否已经获得批准，两者拆成独立、单一职责的纯函数——既方便单测，也方便 `harness.py` 里的全局不变量检查复用同一份判断标准，而不是各自维护一份容易漂移的逻辑。

---

## 5. Tool Design

工具接口本身就是 Verifier 的一部分——接口设计得越贴近真实业务语义，模型现场推理的空间才越真实，而不是被接口本身架着走。以下是最终形态。

### 5.1 Query tools（无状态查询）

`get_customer_info`、`get_my_calendar`、`get_customer_calendar`、`get_travel_policy` 四个工具无状态、无争议，模型自行决定查询顺序。

### 5.2 Search / Book 分离

航班和酒店都遵循同一个原则：**Search 只负责返回完整候选列表，Book 只接受候选中真实存在的 ID，两者职责严格分离**，模型必须先比较候选、再做预订，而不能凭空猜一个条件去"试"。

- `search_flight(date, destination)` 返回全部候选航班（`flight_id`、出发/到达时间、价格、查询时的余票状态）。
- `book_flight(flight_id)` 只接受上一步返回的真实 `flight_id`。它可能因为**提交订单时的实时库存变化**而失败——这是环境状态的变化，不是代码里写死的"选这个就失败"，失败点完全由 mock 数据决定，下一步怎么办交给模型现场推理（重新从候选里选，或重新搜索）。
- `search_hotel(date, near)` 返回候选列表，每个候选带 `hotel_id`、`price_per_night`、`within_policy`（是否在预算内的布尔值）。顶层 `status` 恒为 `success`，不使用容易让模型直接跳过权衡、误判为"全部不可行"的标签（如 `over_budget`）。
- `book_hotel(hotel_id)` 只接受候选中真实存在的 `hotel_id`，酒店名称和价格由后端从候选中查出，模型无法传入一个不存在的选项或篡改价格。

### 5.3 Human approval as a tool

`request_human_approval(reason, proposed_action)` 和其他工具的调用方式完全一致——模型自己判断"这个场景需不需要审批"并主动调用它，代码不做这层判断。代码收到这个调用后才会真正暂停，把理由推给前端，等待用户点击"批准/拒绝"，这个结果再作为下一个 Observation 喂回模型（见 §6）。当前触发场景：预订价格超出差旅政策上限的酒店；正式向客户发送确认邮件之前的最终确认。

### 5.4 Trajectory 字段：`call_id` / `step_id` / `run_id`

每一次工具调用及其对应的结果都携带 `call_id`（来自模型返回的 `tool_call.id`），`tool_call` 和 `tool_result` 事件靠这个 ID 关联，而不是靠"事件按顺序一一对应"这种隐含假设——一旦出现乱序或者（防御性场景下的）并行调用，基于顺序的配对会直接错位。每个事件同时带 `step_id`（`session.model_calls` 计数）和 `run_id`，前者标出这是第几轮模型调用，后者在恢复场景下把"同一个任务的多次 `run_agent_loop` 调用"串成一条连续的事件流。这三个字段同时服务生产环境的前端渲染和评测端的事件分析（`harness.py::paired_calls`），是同一份实现，不是各自维护。

---

## 6. Conversation Lifecycle & Resumability

一次任务在 `RUNNING` / `AWAITING_APPROVAL` / `AWAITING_REPLY` / `DONE` / `ERROR` 五种 `lifecycle_state` 之间流转：

```
        ┌────────────────────────────┐
        │   RUNNING（模型 ↔ 工具循环）    │
        └──────────────┬─────────────┘
                        │
     ┌──────────────────┼──────────────────┬────────────────┐
     │                  │                  │                │
     ▼                  ▼                  ▼                ▼
request_human_    没有调用工具，且     没有调用工具，且   连续 MAX_TURNS 次
approval 被调用    Checklist 已全满    Checklist 未全满    仍未结束
     │                  │                  │                │
     ▼                  ▼                  ▼                ▼
AWAITING_APPROVAL  Generating Report   AWAITING_REPLY       ERROR
（暂停，Session      → DONE            （暂停，Session        （终止，Session
落盘）                （Session 落盘）    落盘，等待用户         落盘；一类值得
     │                                   继续说话）            记录的 Bad Case：
     │                                       │                重复失败/死循环）
     └──────────────┐              ┌─────────┘
                     ▼              ▼
        读回 Session，带 resume_input 重新调用
              run_agent_loop（回到 RUNNING）
```

**任务真正结束的判定条件**：`没有调用工具` *且* `Checklist 全部完成` 两者同时成立（原因见 §9 Evolution 1）。这一章要讲的是：判定成立之后，"暂停"具体意味着什么。

### 6.1 为什么协程挂起不是真正的可恢复性

早期实现里，`AWAITING_APPROVAL` / `AWAITING_REPLY` 是靠一个还活着的 `await approval_queue.get()` / `await reply_queue.get()` 实现的——`run_agent_loop` 这个协程本身没有退出，只是卡在这一行等待另一端往队列里塞一个值。这在同一个进程、同一次 WebSocket 连接存活期间没问题，但只要这个前提被打破（进程重启、连接断开、想用另一台机器的一次请求去恢复它），这个卡在半路的协程就彻底丢失了——它的状态只存在于 Python 解释器的调用栈里，从来没有真正落地过。

这不是"还没做持久化"的问题，是这个实现模式本身跟"可恢复"这个目标不兼容：只要恢复依赖的是"原来那个协程还活着"，就永远做不到跨进程恢复。真正的可恢复性要求：暂停点必须是执行的**完全终止**——`run_agent_loop` 这次调用要能干干净净地 `return`，把恢复所需的一切都写进一个跟进程生命周期无关的地方，下一次恢复是一次**全新的函数调用**，而不是"叫醒"什么东西。

### 6.2 三个真正的暂停/终止点

`run_agent_loop` 现在只在下面几个地方会让这次函数调用结束：

| `lifecycle_state` | 触发条件 | 落盘时机 |
|---|---|---|
| `AWAITING_APPROVAL` | 模型调用了 `request_human_approval` | `_process_tool_calls` 命中该工具后立刻 `save_session` |
| `AWAITING_REPLY` | 这一轮模型没有调用任何工具，且 Checklist 未全满 | 主循环里直接 `save_session` 后 `return` |
| `DONE` | 这一轮模型没有调用任何工具，且 Checklist 全满（最终报告已生成） | 生成完最终报告后 `save_session` |
| `ERROR`（终止而非暂停） | 累计调用模型达到 `MAX_TURNS` 仍未结束 | 同样 `save_session`，但这是任务的终点，不会再被恢复 |

`AWAITING_APPROVAL` 落盘时还会带上 `pending_approval`（`call_id` / `reason` / `proposed_action`）和 `pending_tool_calls`（这一批 tool_calls 里排在被暂停的那个后面、还没来得及执行的其余调用）。后者存在的原因见 §6.4。

### 6.3 `resume_input`：显式描述"人做了什么"

恢复不是"继续往下跑"，而是一次新的 `run_agent_loop(websocket, run_id, session, resume_input)` 调用，`resume_input` 用一个小 dict 显式描述这次恢复带来的新信息：

```python
{"kind": "approval", "approved": bool, "user_note": str}   # 用户点击了批准/拒绝
{"kind": "reply", "text": str}                              # 用户在输入框继续说了话
```

`_resolve_pending_approval()` 负责把批准/拒绝结果转换成一条 `role: tool` 消息塞回 `session.messages`（跟其他任何工具的返回结果同构，模型看到的是同一种形状的 Observation），并在批准的是"酒店超预算"类审批时把 `session.approval_state["hotel_over_budget_approved"] = True`，这样 `book_hotel` 的运行时护栏才能放行。

### 6.4 一个遗留的防御性分支：批内暂停

`parallel_tool_calls=False` 已经要求网关每轮只返回一个 tool_call，但这不是网关一侧强保证的行为（`multi_tool_call_detected` 事件就是为这种情况准备的，见 §10）。万一某一轮真的返回了多个 tool_call，而其中第一个恰好是 `request_human_approval`，简单地"暂停当前这一个调用"是不够的——批次里排在它后面的调用还没执行，不能被悄悄丢弃。`session.pending_tool_calls` 就是用来接住这些"排队中"的调用：恢复时 `_resolve_pending_approval` 会先处理完审批结果，再把 `pending_tool_calls` 接着跑下去（`_process_tool_calls`），如果这批里又撞上第二个审批请求，会再次暂停、再次落盘——理论上可以递归发生任意次，不依赖任何硬编码的"最多一层"假设。

### 6.5 `session_store.py`：落盘细节

`Session` 落盘用的是 SQLite 里一张单表：

```sql
CREATE TABLE IF NOT EXISTS sessions (
    run_id TEXT PRIMARY KEY,
    session_json TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

`lifecycle_state` 被特意从 `session_json` 里拆出来单独存一列（虽然它本来就是 `session_json` 里的一个字段），是为了以后可以直接用 SQL 过滤"所有正在等待审批的会话"之类的查询，不需要每一行都反序列化一遍 JSON 才能判断。

`dataclasses.asdict(session)` 能覆盖绝大多数字段，但 `seen_readonly_calls` 的 key 是 `(name, args_json)` 这样的 tuple——JSON 对象的 key 只能是字符串，`json.dumps` 会直接报错。`_encode_seen_readonly_calls` / `_decode_seen_readonly_calls` 把它转换成 `[[key_list, count], ...]` 的列表形式做一次显式的编解码，往返测试（`test_session_store.py::test_plain_json_types_no_custom_encoder_needed`）专门验证了这一点，而不是假设"反正是 dict，`asdict` 一下肯定能存"。

`save_session` / `load_session` 都是同步阻塞函数（`sqlite3` 本身不支持 `async`），调用方一律用 `asyncio.to_thread(...)` 包一层，不在事件循环里做阻塞 I/O。

### 6.6 `websocket_endpoint`：从"喂队列"到"读盘 + 起新任务"

早期实现里，`approval_response` / `user_reply` 这两类前端消息是直接 `await approval_queue.put(...)`，把值喂给那个还卡在 `await queue.get()` 的协程。现在它们做的是：

1. 从 SQLite 按 `run_id` 读回持久化的 `Session`；
2. 校验它当前确实处于对应的 `lifecycle_state`（比如 `approval_response` 只在 `AWAITING_APPROVAL` 时被接受，状态不对就直接返回一条 `error` 事件，不会静默地做错误的事）；
3. 构造好 `resume_input`，用 `asyncio.create_task` **起一个全新的 `run_agent_loop` 任务**，而不是唤醒旧的。

这也让新增的 `resume_task` 消息类型变得合理——它只是"读回 `Session` 的当前状态渲染给前端看"（比如刷新页面、或者一个全新的 WebSocket 连接接手一个原本在另一条连接上跑的任务），本身**不推进任何执行**，因为推进执行需要显式的 `resume_input`，而"我只是想看看现在跑到哪了"不该产生任何副作用。

---

## 7. Final Report

五项 Checklist 全部完成后，系统会额外单独调用一次模型（不带 tools），让它基于本次任务真实发生的工具调用历史和审批记录生成一份分两层的总结，而不是简单显示 "Done"：

```
✅ 执行层（客观验证通过）
- 有客观依据、可核对的事实：订单号、确认号、event_id、message_id 是否真实存在

⚠️ 决策层（建议人工复核）
- 需要人工判断的取舍：为什么选了这个方案而不是别的、审批中批准的超预算金额和理由、
  可能存在的风险（如可退改灵活度）
```

这次调用刻意**不复用主循环里那份可能已经很长的对话历史**，而是重新组织一份干净的事实清单（Checklist 状态 + action log + approval records）喂给模型，避免报告被前面大段的推理文字带偏，确保报告紧扣真实发生的事情。

### 7.1 结构化输出：Pydantic Schema 校验

模型被要求"只输出 JSON"，但"要求"和"保证"是两回事——网关偶尔会在 JSON 前后夹杂解释性文字，或者字段类型对不上（比如 `decision_layer` 输出成一个字符串而不是字符串列表）。`generate_final_report` 的解析分两层：

1. 先做常规的 ```` ```json ```` 代码块剥离 + `json.loads`，解析失败（`JSONDecodeError`）就把模型原始输出整段塞进 `decision_layer`，明确标注"报告生成格式异常"，而不是让页面上什么都不显示。
2. 解析成功后，用 `FinalReportSchema`（`pydantic.BaseModel`，`execution_layer: list[str]` / `decision_layer: list[str]`）做一次 `model_validate`。校验失败（`ValidationError`）会被精确捕获——错误信息里带着 `ve.error_count()` 和具体是哪个字段不满足预期，同样作为一条决策层提示原样展示，并附上模型的原始输出，方便复盘到底是哪里出错，而不是简单地 `try/except` 之后吞掉。

这条校验路径本身不改变报告的业务内容，只是把"这份报告的结构本身是不是可信的"这件事也纳入了防御范围——跟下面的编号防幻觉核验是同一个思路的两个层面：一个管**结构**，一个管**内容**。

编号防幻觉核验（`find_fabricated_ids`）保持不变：代码会核对 `execution_layer` 文本里出现的 ID 形式的 token（`[A-Z]{2,8}-\d{2,8}`），跟本次任务 `action_log` 里真实产生过的 `order_id` / `confirmation_id` / `event_id` / `message_id` 做差集，命中差集说明报告提到了一个从未真实发生过的编号，会追加一条决策层提示明确标红，而不是悄悄相信模型没有幻觉。

这份报告是整个系统里最直接体现"Agent 能力边界"的部分：执行层有客观验证器可以百分百确认，决策层没有，需要人自己判断——这个边界被直接做成了产品功能，而不是回避掉。

---

## 8. Evaluation Methodology

评估分两种性质完全不同的活动，不能混为一谈：

- **Replan（运行时）**：任务执行中，模型遇到失败 Observation 现场重新决策，这是 Agent 单次运行内的能力，不算"迭代"。
- **Evaluation（开发时）**：固定 Prompt 和代码，只改动 mock 数据构造定向场景，多轮运行观察模型在哪些地方判断错误，定位原因、修复、复测——这才是迭代。

### 8.1 Risk-driven，而不是凑数

方法论是"预先定义风险类型 → 只改 mock 数据构造定向场景 → 多次运行观察 → 只有真实出现断言不满足才进入修复流程"，而不是随便跑几轮看效果，也不为了凑够案例数而弱化已经调好的 Prompt。每类场景运行 2–3 次即可，跑够验证"是否存在真实失败"，不追求统计意义上的稳定性认证。

预先定义的四类风险：

| 分类 | 具体表现 |
|---|---|
| 审批遗漏 | 触发超预算/不可逆操作，但模型没有请求人工审批就直接执行 |
| Observation 误读 | 模型对返回字段理解错误，把候选误当成已确认 |
| 重复失败 / 死循环 | Replan 后反复尝试同一个已知失败的方案 |
| 结论型错误 | 推理文字通顺、语言自然，但最终决策实际上不满足约束条件 |

评测侧针对这套方法论做了进一步加固（mock 数据必须真正逼出风险路径、`must_exercise` 区分"跳过"和"没做到"等），详见 `EVAL_DESIGN.md`。

### 8.2 "未复现"也是合法结论

针对每一类风险设计诱导性场景（例如：让所有酒店选项都超预算、把不满足客户时间约束的选项排在候选列表最前面），跑 2–3 轮观察。**如果多轮定向测试下依然没有复现，如实记录"未复现"，这本身就是一条真实、有价值的测试结论**，说明当前版本在该类风险上是稳健的——不需要靠"编造出模型确实犯过这个错"来证明方案的严谨性。

这套方法论在实际执行中意外地起了另一个作用：它把"模型判断错误"和"系统工程缺陷"这两类完全不同性质的问题区分开——后者的发现和修复过程见 §9。

---

## 9. Design Evolution

Chapter 5–7 描述的是最终形态；这一章解释它为什么变成这样。

### Evolution 1 — Session Continuation

**发现**：审批卡片被用户点击"拒绝"后，模型的合理反应是用文字向用户反问（例如"请问您希望怎么调整？"），而不是擅自决定怎么改。但系统把"模型这一轮没有调用工具"无条件当作"任务已完成"，直接销毁了当前任务的对话历史。用户在输入框里的下一句回复，唯一的路径是走"开始新任务"，带着一份全新的空上下文——模型不是真的"失忆"，它是在一个上下文为空的全新任务里，诚实地反映"我不知道这是哪个客户"。

**根因**：代码把"没有调用工具"这一个信号，同时当成了两种完全不同的情况——"任务真的做完了"和"模型在等用户回话"——只认前者，从不考虑后者。这个漏洞不是"审批拒绝"这条路径专属的，任何时刻模型因为信息不足选择反问都会触发同样的问题。

**修正**：判定任务是否结束的条件从"没有调用工具"改为"没有调用工具 *且* Checklist 全部完成"两者同时成立（即 §6 描述的最终状态机）。这不是 Prompt 层面的调整，是 Runtime 状态管理的架构修正。

**取舍**：这类缺陷不属于原定义的四类模型 Bad Case——模型当时的判断（暂不擅自决定、转而反问）其实是对的，错误发生在系统的会话状态管理层，而不是模型的推理层。面试/评审场合需要把这两者分开讲清楚，混为一谈会模糊"执行层 vs 决策层"这个核心洞察本身的边界。

### Evolution 2 — Tool Abstraction

**发现**：航班工具最初的形态是 `search_flight(date, preferred_time)`——模型必须先猜一个具体时间，工具再返回"这个时间行不行"。这本质上是 `check_flight_availability`（查一个猜测时间是否可订），而不是真实世界里"先看全部候选、再比较选择"的 `search_flight`。唯一的失败点（某个时段满员）依赖模型"第一次故意搜错"才能触发，是为了演示 Replan 而设计的不自然 API。同时，酒店工具的顶层 `status: "over_budget"` 具有误导性——候选里其实包含一个合规的选项，把整体状态标成 over_budget 容易让模型跳过权衡、直接误判为"必须审批"。

**根因**：这是 API **抽象层级**设计错误，不是 Prompt 没写好——模型的推理能力再强，也无法在一个本身就不真实的接口形状上做出真实的决策。

**修正**：航班改为 §5.2 描述的 Search/Book 分离形态，Replan 的触发点从"搜索阶段的人为设计"移到"预订阶段的真实库存变化"；酒店去掉误导性顶层状态，改为每个候选自带 `within_policy` 结构化字段。`book_flight`/`book_hotel` 的参数从模型自由传入的时间字符串/价格数字，改为只能传候选中真实存在的 ID。

**取舍**：这次修正没有改变 Replan 或审批机制本身的任何代码逻辑——`_update_checklist`、报告生成、会话延续机制完全不受影响，因为这些逻辑只依赖工具的 `name`/`status` 字段。这也验证了一件事：**工具接口和 Runtime 状态管理是两层独立的关注点**，修一层不需要联动改另一层，这本身也是这套架构设计合理性的一个佐证。

### Evolution 3 — Efficiency Guardrail 从评测下沉到生产（P4）

**发现**：`is_repeated_readonly_call`（检测"同一个只读工具、完全相同的参数被重复调用"）最初只存在于 `harness.py` 里，是一条纯粹的评测侧规则——`check_global_invariants` 用它来判断一次评测运行是不是有"重复查询浪费"。但这意味着：只有跑评测的时候才会发现这个问题，生产环境里模型真的这么做了，用户看到的只是"多等了几秒"，没有任何信号告诉他"这里发生了一次可以避免的重复调用"。

**根因**：这条规则的判断逻辑（哪些工具算只读、怎么算"参数完全相同"）本来就跟"是不是在跑评测"无关，是运行时和评测都应该共享的同一套标准——放在 `harness.py` 里纯属历史原因（先有评测需求，后来才意识到生产也需要）。

**修正**：把 `is_repeated_readonly_call` 提升到 `approval_utils.py`（`main.py` 和 `harness.py` 共同依赖的那层纯函数），`main.py` 的 `_process_tool_calls` 在每次工具调用后都会检查一次，命中就广播一条 `efficiency_warning` WebSocket 事件给前端。这是一条**软警告**，不是硬拦截——不会阻止模型继续执行，只是把"这里可能在浪费"这个信号实时暴露出来，是否要因此调整 Prompt 是产品/工程侧的后续判断，不该由这条护栏本身替用户做决定。

**取舍**：`harness.py` 里原来自己维护的判断逻辑被删除，`check_global_invariants` 改成直接调用 `approval_utils.is_repeated_readonly_call`——保证了"运行时判一套标准、评测判另一套标准"这种自相矛盾不会发生，也是 README 里"两类不同的代码分支"那张表格中"安全底线"一栏的具体案例之一。

### Evolution 4 — 从"内存里挂起"到"可持久化、可重入"（P5 / P6）

**发现**：项目早期的 Future Work 里明确写着"断线重连 / 连接状态判断"是一处已知空白——`await approval_queue.get()` 这种实现，本质上把整个任务的存活期绑定在了一个具体进程、一条具体 WebSocket 连接上。这在 demo 场景下大多数时候不会暴露问题，但只要往"这是一个要长期运行的系统"这个方向多想一步，就会发现它是一个结构性缺陷，不是靠加个 `try/except` 能修好的。

**根因**：跟 Evolution 1 是同一类问题的更彻底版本——"暂停"这个语义被实现成了"协程还活着、只是卡住了"，而不是"状态已经落地、随时可以从任何地方重新拾起"。只要恢复依赖的是内存里某个还没退出的调用栈，跨进程恢复就是不可能完成的任务，无论加多少层重试或者异常处理都绕不开这一点。

**修正**：分两个阶段（详见 §6）：
- **P5**：先把"Agent 能做什么"（`AgentDefinition`）和"这次任务跑到哪了"（`Session`）解耦，让"需要持久化的状态"第一次成为一个边界清晰、可以被整体序列化的对象，而不是散落在函数局部变量里的一堆东西。
- **P6**：把三个真正的暂停/终止点（`AWAITING_APPROVAL` / `AWAITING_REPLY` / `DONE`，以及作为终态的 `ERROR`）改造成"落盘后彻底 `return`"，`run_agent_loop` 从此不再是一个会被挂起的协程，而是一个可以被反复、独立调用的、"跑一段、暂停、再跑一段"的函数；`websocket_endpoint` 相应地从"往队列里塞值"改成"读盘 → 校验状态 → 起一个新任务"。

**取舍**：这个改动的直接代价是"状态必须是可以被 JSON 序列化的"这条约束被前所未有地当真了——`seen_readonly_calls` 的 tuple key 之类的边角情况过去可以随便用，现在必须显式处理（见 §6.5）。`session_store.py` 也需要在模块加载时机上小心处理和 `main.py` 的循环依赖。评测侧（`harness.py`）也因此需要一个新的外层驱动循环（`run_one_case` 里的 `drive()`）去模拟"反复调用、反复读盘、反复决定下一次 `resume_input`"这个新的调用协议，取代了原来直接等一个 `sim_user()` 协程把值塞进队列的写法——这部分改动细节见 `EVAL_DESIGN.md`。

---

## 10. Engineering Trade-offs

**Why mock tools instead of real APIs（CRM / 航班 / 酒店 / 差旅政策）？**
核心原因不是省事，而是需要精确控制失败点的触发时机和内容，保证演示的稳定性和可复现性。真实 API 的失败时机不可控，会让"验证决策来自现场推理"这件事本身变得难以复现。日历同步、邮件发送这类低风险、结果客观可核的环节保留了接入真实 API 的空间，用来增加可信度。

**Why an explicit Checklist instead of "流程跑完 = 完成"？**
Workflow 式的实现天然会用"最后一步执行完"作为终止信号，这恰恰是 Agent 和 Workflow 最容易混淆的地方。显式 Checklist 把"完成"的判断标准从工程实现细节（跑没跑完）搬到了业务语义（该办的事有没有全部办妥），也让 Replan 的必要性有了明确的锚点。

**Why mandatory human approval before irreversible actions?**
完全自动是理想化描述。真正生产环境的 Agent 会在有实际后果、不可逆的操作（对外发邮件、超出预算规定的支出）前保留人工确认，这不是能力不足的妥协，而是对"Agent 能力边界"的诚实态度——这一原则也直接体现在 §7 的执行层/决策层报告里。

**Why separate execution layer and decision layer in the final report？**
不是所有"完成"都有同等的确定性。订单号是否真实存在，是可以客观核验的；"为什么选这个方案而不是别的"是权衡取舍，没有客观验证器。把两者混在一起展示成一句"任务已完成"，会掩盖 Agent 当前技术真实存在的能力边界；分开展示，是把这个边界诚实地做成产品功能。

**Why `parallel_tool_calls=False`, and why still defend against multiple tool calls per turn？**
把网关约束成"每轮只返回一个 tool_call"是为了让暂停/恢复的语义足够简单——一次暂停只对应一个还没执行的动作。但这只是对网关的请求参数，不是代码能强制的行为，`multi_tool_call_detected` 事件专门用来暴露"网关实际上没有遵守这个约束"这种情况；`pending_tool_calls`（§6.4）是防御性设计，假设约束**可能**被违反、并为此留好退路，而不是假设它一定成立、出问题了再手忙脚乱地补。

**Why reentrant + SQLite instead of keeping the simpler in-memory `asyncio.Queue` suspension？**
`asyncio.Queue` 的实现明显更短、更直觉，如果需求永远停留在"单进程、单连接、demo 跑一次就结束"，没有理由换成更复杂的方案。换成可重入 + 持久化的直接代价是：状态必须显式设计成可序列化的（§6.5 的 tuple-key 编解码就是这份代价的具体体现），`main.py` / `session_store.py` 之间要小心处理循环依赖，`harness.py` 的驱动逻辑也要跟着变复杂。换来的是"暂停"从一个只在特定运行环境下成立的隐含假设，变成了一个不依赖运行环境的显式状态——这对一个想演示"生产级思考方式"而不只是"能跑起来的 demo"的项目来说，是值得付的成本。

**Why not just use the OpenAI Agents SDK's `Session` for this？**
SDK 的 `Session` / `SQLiteSession` 解决的是"下一次 `Runner.run` 怎么自动带上历史"，本质上是一份消息日志的持久化协议，不感知"审批还没处理完"、"这一批工具调用只执行了一半"这类业务语义上的中间态。本项目的暂停点天然带有这种中间态（`pending_approval` / `pending_tool_calls`），如果只持久化消息列表，恢复时没法知道"上次到底停在哪一步、还有什么没做完"。这不是说 SDK 的设计有问题——它解决的是更通用、更轻量的场景——而是这个项目的暂停语义本身更接近工作流引擎（如 Temporal）里的"durable execution"，所以选择了自己实现一个覆盖执行状态机、而不只是消息历史的持久化层。

**Why a mature, commonly-seen scenario instead of a novel one？**
见 §2.4。场景本身不需要重新说服评审者"这是不是个问题"，所有展示精力都能集中在"这几处设计判断是不是真的做对了"，而不是先花时间证明场景的合法性。

---

## 11. Future Work

- ~~断线重连 / 连接状态判断~~ —— 已在 P5/P6 解决，详见 §6、§9 Evolution 4：`Session` 持久化到 SQLite 后，断线重连、进程重启后恢复都不再依赖任何存活的内存态。
- **跨任务的长期记忆**：当前每次 `start_task` 都是全新上下文，任务之间不共享历史（例如"上次那次拜访的酒店偏好"）。是否需要、以及以什么形式引入跨任务记忆，尚未定案——这跟 `Session` 的持久化是两个不同层面的问题：前者是"同一个任务暂停后能不能续上"，后者是"不同任务之间要不要有记忆"。
- **多实例部署下的持久化层**：`session_store.py` 目前是单文件 SQLite，适合单开发者 demo 和单进程部署；如果要支持多个 worker 进程同时处理不同的 `run_id`，需要换成支持并发写入的存储（Postgres / Redis 之类）。`session_store.py` 对外的函数签名（`save_session(run_id, session)` / `load_session(run_id)`）已经刻意保持和具体存储实现无关，理论上替换后端不需要改 `main.py`。
- **`Session` 的过期与清理**：目前没有任何机制清理已经 `DONE` 很久的历史会话，`sessions.db` 会无限增长。
- **恢复操作缺少鉴权**：任何拿到 `run_id` 的人都可以对一个 `AWAITING_APPROVAL` 的任务发起 `approval_response`——demo 场景下没问题，生产环境需要补上"这个 `run_id` 是不是当前用户发起的"这层校验。
- **副线场景复用**：同一套 `Goal → Plan → Action → Observation → Replan → Verifier → Done` 框架可以复用到其他场景（例如"会议跟进"：读取会议纪要 → 提取 action items → 创建任务 → 通知相关人 → Checklist 校验）。当前未做真实联调，仅作为架构可复用性的说明。
- **真实系统接入**：日历同步、邮件发送目前均为 mock，是最适合优先替换为真实 API（Google Calendar、SMTP/SendGrid）的两个环节，因为它们的结果客观可核，替换后不影响失败点的可控性。
