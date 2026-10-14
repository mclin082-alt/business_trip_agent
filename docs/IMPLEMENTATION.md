# 商务拜访执行 Agent：实现与开发指南

[返回项目首页](../README.md) · [证据与验证记录](VERIFICATION.md)

本页保留原 README 的功能、架构和安装说明。功能设计与 Eval case 数量不代表完整 live 验收已通过；业务写入仍采用 mock/sandbox。

## 做出了什么

### 1. 自建 Agent Runtime

项目没有把业务写成固定 workflow，也没有直接套用 Agent 编排框架，而是实现了一个可观察、可重入的 runtime：

- 模型根据目标和历史上下文自主规划下一步，并通过 OpenAI-compatible tool calling 调用 Skill。
- 工具返回的原始 Observation 进入下一轮上下文；订票失败、审批拒绝等异常之后的方案由模型现场重规划。
- Checklist 以业务结果而不是调用顺序判定任务是否完成，避免“模型这一轮没调用工具”被误判为结束。
- Runtime 有明确的 `RUNNING`、`AWAITING_APPROVAL`、`AWAITING_REPLY`、`DONE`、`ERROR` 生命周期。
- WebSocket 把 reasoning、tool call、Observation、Checklist、审批和最终报告作为事件实时推送到前端。

这里真正实现的不是“五步差旅流程”，而是一个能承载不确定路径、失败恢复和人工介入的执行循环。

每次模型调用前，`context_manager.py` 从 canonical Session 构建独立 context view。默认的
`structured_compaction` 在估算 token 超过阈值后，注入由 Checklist、action log、审批记录等
runtime state 确定性生成的结构化摘要，并保留最近 4 个完整 interaction units；assistant
tool calls 与对应 tool results 不会被拆开。原始消息和审计轨迹始终完整持久化。

### 2. Durable Execution：真正暂停并恢复

等待审批或用户回复时，`run_agent_loop` 不会把协程挂在内存里，而是保存完整 Session 后返回。恢复时从 SQLite 读回状态，发起一次新的函数调用继续执行。

持久化内容不仅包括消息历史，还包括：

- Checklist 和生命周期状态；
- 已完成和待执行的 tool calls；
- 已产生的订单、审批状态和审批记录；
- 防重复调用所需的运行时状态；
- 当前角色、connector context 和 action log。

因此任务可以跨 WebSocket、跨进程、服务器重启后继续。独立序列化往返测试验证 Session 读回后语义不丢失。

### 3. 跨任务 Long-term Memory

`memory_store.py` 使用独立 SQLite 保存带实体 scope、来源、confidence 和状态的软偏好。Session
启动时按真实 `actor_id` 召回个人偏好；`get_customer_info` 返回稳定 `customer_id/company_id`
后再召回客户惯例；调用方提供 `origin + destination` 时可同时使用 route scope。每个类型最多注入
3 条，`explicit` 排在 `inferred` 前，当前任务指令始终覆盖历史数据。

只从用户明确表达的长期偏好中确定性抽取，不会把一次预订行为自动解释为偏好。任务成功后
运行独立 post-run hook；明确纠正会立即 supersede 旧记录。写入按 run + normalized memory 幂等，
Memory IO 失败只产生 `memory_write_failed`，不会把已完成任务改成失败。查看、删除和按 subject
清空分别通过 `GET /api/memories`、`DELETE /api/memories/{memory_id}` 和
`DELETE /api/memories` 提供。

Memory 以单独的 `<historical_preferences>` 数据块进入 context，不进入原始 Session，也不被 Policy、
Permission 或审批 guardrail 读取。因此即使历史值包含“允许超预算”之类文本，也不能成为授权依据。

### 4. Human-in-the-loop 与 Runtime Guardrails

Agent 可以自主判断何时请求人工审批，但最终副作用仍受 deterministic guardrail 约束：

- 酒店超预算且没有有效审批时，`book_hotel` 在运行时硬拒绝，不依赖模型“自觉”。
- 面向客户的确认邮件发送前必须获得人工最终确认。
- 审批拒绝是正常 Observation；Agent 可以使用拒绝理由调整方案，或进入 `AWAITING_REPLY` 等待补充信息。
- 相同对象的重复预订做幂等处理，避免模型重试造成重复订单。
- 完全相同的只读查询会产生效率警告，暴露潜在循环或浪费。
- 最终报告经过结构化 Schema 校验，并核对其中的订单号、确认号是否真实出现在本次执行中。

这套边界把“下一步怎么选”留给模型，把“哪些底线不能越过”留给代码。

### 5. Skill Contract 与角色权限

早期版本里，工具 handler、function-calling Schema 和权限判断分散维护。现在每个 Skill 由一份统一 contract 定义：

- 输入 `parameters` 与成功态输出 `returns` JSON Schema；
- `roles_allowed` 静态调用权限；
- 是否产生外部副作用；
- `risk_level`；
- 明确的 failure semantics；
- 版本、弃用和替代关系。

Runtime 的 tools Schema、权限检查和文档都从同一组 Skill 定义派生，减少接口、实现和说明之间的漂移。目前有 13 个生效 Skill，并保留一个未接入 runtime 的版本演进草案。完整 contract 见自动生成的 [`skills_registry.md`](../skills_registry.md)。

角色权限与审批权限被刻意分成两条轴：例如 employee 可以调用 `book_hotel`，但某次预订一旦超预算，仍必须由政策指定的 manager 或 travel_admin 批准。`role_matrix.py` 可以从 Skill 与 Policy 数据生成统一权限矩阵。

### 6. Governed Multi-Agent Collaboration

single-agent runtime 现在扩展为 manager + bounded specialists：

- Trip Orchestrator 始终拥有用户会话、Checklist、HITL 和最终报告；
- Booking Subagent 只比较已搜索候选，输出绑定 candidate snapshot 的 typed proposal；
- Policy/Compliance Subagent 只做合规复核，输出 `ALLOW`、`REQUIRE_APPROVAL` 或 `DENY`；
- runtime 取 Policy agent 与 deterministic policy 中更严格的结论，模型不能放宽代码底线；
- 超预算审批绑定具体 `proposal_id`，不能被其他候选或后续搜索复用；
- delegation、conflict、escalation、proposal/review ID 和 subagent usage 全部进入持久化 trace。

这采用 OpenAI Agents SDK 所称的 agents-as-tools 语义，而不是让 specialist handoff 后接管用户对话。完整 delegation protocol、冲突矩阵、SDK 借鉴点和 Sandbox 边界见 [`docs/MULTI_AGENT_DESIGN.md`](MULTI_AGENT_DESIGN.md)。

### 7. Policy as Data：规则、依据与审计同源

差旅政策不再以散落在 mock 和判断函数里的裸数字存在，而是存放在结构化的 [`policy/travel_policy_v1.json`](../policy/travel_policy_v1.json) 中：

- 给模型看的 `rule_text`；
- 给 runtime 执行的 `rule_logic`；
- `policy_id`、版本和生效日期；
- 适用角色与审批角色。

查询政策、判断预算边界、生成拒绝原因和记录审批依据都读取同一份 Policy Store。这样可以避免“文档说 800 元、代码实际判断 1000 元”，也让最终报告和审批记录能够引用具体政策版本，而不是只有一个无法追溯的数字。

### 8. 从 Mock 到真实系统的 Integration Boundary

项目默认使用 mock，保证 Demo 和 Eval 可重复；同时已经为 `get_my_calendar` 接入真实只读 MCP 日历：

- Agent 始终调用稳定的 `get_my_calendar` Skill，不感知底层来自 mock 还是 MCP。
- `calendar_adapter.py` 隔离 provider、协议和返回格式差异。
- 超时、权限拒绝、字段缺失和配置错误被归一化成模型可处理的 failure semantics。
- 数据缺失绝不会被解释成“全天有空”。
- 返回包含 `source`、`trace_id`、`data_as_of` 和 `latency_ms` 等可观测字段。
- MCP server 只使用只读日历权限；访问令牌由 connector 自己管理，不进入 Agent Session。

当前真实边界是：个人日历可通过测试账号只读接入；客户日历、日历写入、邮件和机酒预订仍为 mock/sandbox，避免在验证阶段扩大不可逆动作范围。

### 9. Agent Evaluation Framework

项目实现的不是一组 happy-path 测试，而是一套围绕 Agent 失败模式设计的 Eval Harness：

- 8 个产品质量维度：Task Success、Decision Quality、Safety、Recovery、Efficiency、Continuity、Robustness、Collaboration。
- 12 个 Eval Case，覆盖 L1 Planning 到 L9 Multi-Agent Governance；默认共运行 26 次。
- 三层判定：场景专属 deterministic check、跨场景 global invariants、只用于语义问题的 LLM-as-judge。
- `must_exercise` 区分“风险未复现”和“测试根本没触发目标路径”。
- Failure taxonomy 把审批绕过、幻觉、场景设置错误、循环浪费、上下文丢失等失败做统一归因。
- 每次运行保存完整 JSONL 事件流，可离线 replay，避免每次分析都重新调用被测模型。
- Capability 覆盖率由 case metadata 自动生成，而不是维护一份容易过时的手工表格。

评测方法、case matrix 和明确未覆盖项见 [`eval/EVAL_DESIGN.md`](../eval/EVAL_DESIGN.md)。

---

## Architecture

```text
Goal（一句话目标）
  ↓
Trip Orchestrator ──→ Search Skills
  ↑                       ↓
  │             Booking Subagent → typed Proposal
  │                       ↓
  │             Policy Subagent → reconciled Review
  │                       ↓
  └──── Observation ← Runtime Governance Gate ← Adapter / Mock / MCP
  ↓
Checklist Verifier
  ├─ 未完成：继续规划
  ├─ 需要审批或补充信息：Session 落盘，runtime 返回
  │                         ↓
  │                    Human Response
  │                         ↓
  │                    读回 Session，重新进入 runtime
  └─ 全部完成：生成分层报告 → Schema/事实核验 → DONE
```

**Tech stack**：FastAPI + WebSocket · Qwen OpenAI-compatible API · SQLite Session Store · Pydantic · MCP Python SDK · pytest。

## Repo Map

```text
business_trip_agent/
├── main.py                       # Agent runtime、Session、WebSocket、Skill dispatch
├── context_manager.py            # Full History / Sliding Window / Structured Compaction
├── session_store.py              # SQLite 持久化与 Session 序列化
├── memory_store.py               # 跨任务 Memory 的写入、召回、冲突与遗忘
├── multi_agent.py                # Delegation contract、proposal/review 与冲突治理
├── skills.py                     # Skill contract、Schema、权限与版本
├── policy_store.py               # 结构化政策查询和可执行规则
├── approval_utils.py             # Runtime/Eval 共用的审批、核验和归因纯函数
├── role_matrix.py                # 从 Skill + Policy 生成角色权限矩阵
├── policy/
│   └── travel_policy_v1.json     # 政策文本、规则、版本和审批角色
├── integrations/
│   ├── calendar_adapter.py       # mock/MCP 日历适配与 failure normalization
│   └── feishu_calendar_mcp_server.py
├── eval/
│   ├── harness.py                # Eval cases、checks、invariants、judge、报告
│   ├── replay_harness.py         # 对 JSONL 事件流离线重评
│   ├── mcp_smoke_test.py         # 真实只读 MCP 单次连通性验证
│   └── EVAL_DESIGN.md
├── tests/
│   ├── test_session_store.py     # Durable Session 往返测试
│   ├── test_memory_store.py      # Memory scope、覆盖、治理与安全测试
│   ├── test_context_manager.py   # Context 策略、压缩与协议安全测试
│   ├── test_calendar_adapter.py  # Calendar integration contract tests
│   └── test_multi_agent.py       # Delegation、conflict、approval scope 与 wiring tests
├── generate_skill_registry_doc.py
├── skills_registry.md            # 从 skills.py 自动生成的接口参考
├── static/index.html             # Demo 前端
└── docs/
    ├── MULTI_AGENT_DESIGN.md     # multi-agent 协议、SDK 借鉴与 Sandbox 边界
    ├── ECOMMERCE_MULTI_AGENT_REVISIT.md
    └── images/                   # 文档截图
```

`eval_logs/`、`sessions.db` 和本地虚拟环境属于运行产物，不是系统设计的一部分。

---

## Quick Start

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

在项目根目录创建 `.env`：

```text
QWEN_API_KEY=your_key_here
```

启动：

```bash
python -m uvicorn main:app --env-file .env --host 127.0.0.1 --port 8000
```

本机启动后，前端路径为 `/static/index.html`。`main.py` 本身不加载 `.env`；上面的 Uvicorn 命令显式加载它，也可直接向进程注入 `QWEN_API_KEY`。

### 主要环境变量

| 变量 | 默认值 | 作用 |
|---|---|---|
| `QWEN_MODEL` | `qwen-plus` | 主模型 |
| `QWEN_BACKUP_MODEL` | 空 | 主模型不可用时的备用模型 |
| `QWEN_TIMEOUT_MAX_RETRIES` | `2` | 网关超时业务层重试次数 |
| `PROMPT_CACHE_MODE` | `off` | 设为 `explicit` 开启显式 Prompt Caching |
| `CONTEXT_STRATEGY` | `structured_compaction` | Context view 策略：`full_history`、`sliding_window` 或 `structured_compaction` |
| `CONTEXT_TOKEN_THRESHOLD` | `6000` | Structured Compaction 的估算 token 触发阈值 |
| `STRUCTURED_COMPACTION_RECENT_UNITS` | `4` | Compaction 后保留的最近完整 interaction units 数 |
| `SLIDING_WINDOW_UNITS` | `6` | Sliding Window 保留的最近完整 interaction units 数 |
| `SESSION_STORE_PATH` | `sessions.db` | Session SQLite 路径 |
| `MEMORY_STORE_PATH` | `memories.db` | Long-term Memory SQLite 路径 |
| `MULTI_AGENT_ENABLED` | `1` | 设为 `0` 仅用于 legacy/debug；默认启用 governed multi-agent gate |
| `CALENDAR_PROVIDER` | `mock` | 设为 `mcp` 启用真实只读日历 |
| `CALENDAR_MCP_COMMAND` | 空 | MCP stdio server 启动命令 |
| `CALENDAR_MCP_ARGS` | `[]` | MCP server 参数 JSON 数组 |
| `CALENDAR_MCP_TOOL` | `get_my_calendar` | 日历工具名 |
| `CALENDAR_MCP_TIMEOUT_SECONDS` | `5` | 单次日历读取超时 |

### 可选：真实只读 Feishu Calendar MCP

```bash
CALENDAR_PROVIDER=mcp
CALENDAR_MCP_COMMAND=python
CALENDAR_MCP_ARGS='["/absolute/path/to/integrations/feishu_calendar_mcp_server.py"]'
CALENDAR_MCP_TOOL=get_my_calendar
```

先通过 `lark-cli` 为测试账号配置 `calendar:calendar:readonly` 权限。该 server 只读取日程，不创建、更新或删除事件。

---

## 如何验证

运行本地 contract/unit tests：

```bash
python -m pytest tests/ -v
```

运行完整 Eval：

```bash
python eval/harness.py
```

常用 Eval 环境变量包括 `EVAL_CASE_FILTER`、`EVAL_LOG_DIR`、`EVAL_VERBOSE`、`EVAL_MAX_RESUME_ITERATIONS` 和 `EVAL_BOUNDED_MAX_TURNS`。

对已经保存的事件流离线重评：

```bash
python eval/replay_harness.py
# 或指定日志目录：python eval/replay_harness.py --dir <log-directory>
```

生成最新 Skill Registry：

```bash
python generate_skill_registry_doc.py
```

真实 MCP 连通性 smoke test：

```bash
CALENDAR_PROVIDER=mcp \
CALENDAR_MCP_COMMAND=python \
CALENDAR_MCP_ARGS='["/absolute/path/to/integrations/feishu_calendar_mcp_server.py"]' \
python eval/mcp_smoke_test.py 2026-08-26
```

---

## 当前边界

仓库实现了以下原型能力；离线检查、历史展示与未复验的 live 路径应分别阅读，当前验证范围见 [证据与验证记录](VERIFICATION.md)。它仍是面向能力验证的原型：

- 真实写日历、发邮件和机酒预订尚未接入生产 API。
- SQLite 适合单机演示，不覆盖多 worker 并发部署。
- 通过任务 ID 恢复 Session 尚未加入用户身份鉴权。
- `actor_id` 目前由调用方随 `start_task` 提供（缺省为 `demo_employee`），尚未与生产身份系统做可信绑定。
- approval role 已做 policy 校验，但仍由请求携带/演示默认值提供；生产环境必须绑定可信 IAM 身份，不能让客户端自报 `manager`。
- Token、成本和 latency 已有局部数据，但尚未聚合为产品级运营视图。
- Eval 尚未完整覆盖 Partial Success、Tool Precision、Ambiguous Goal 和 Long Context 退化。

这些边界是刻意保留并明确说明的：项目当前证明的是一套可信 Agent 的核心执行与评测方法，而不是宣称已经完成全部生产化工作。
