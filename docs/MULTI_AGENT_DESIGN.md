# Governed Multi-Agent Extension

## 目标

这次扩展验证的不是“同时运行多个 prompt”，而是：在同一套 Skill contract、Policy Store、durable HITL 和 Eval Harness 上，怎样让 specialist agent 成为受治理的协作者。

```text
User
  ↓
Trip Orchestrator（唯一 user-facing owner）
  ├─ search_flight / search_hotel
  ├─ delegate_booking_plan
  │      ↓
  │   Booking Subagent ──→ typed BookingProposal
  ├─ delegate_policy_review(proposal_id)
  │      ↓
  │   Policy/Compliance Subagent ──→ advisory verdict
  │      ↓
  │   deterministic policy reconciliation
  └─ ALLOW → book
     REQUIRE_APPROVAL → durable HITL → scoped approval → book
     DENY → replan / new search / new delegation
```

Trip Orchestrator 一直拥有用户会话、Checklist 和最终报告。两个 subagent 都没有 book、approval、calendar write 或 email 权限，只能产生结构化 decision artifact。

## Delegation protocol

### Booking Subagent

输入不是完整对话历史，而是最小 context projection：目标、已确认约束、候选 snapshot，以及相关的 CRM、calendar 和 policy observation。输出必须通过 `BookingAgentOutput` Schema，并满足：

- `recommended_option_id` 必须真实存在于候选 snapshot；
- proposal 绑定 `source_search_seq` 与 `context_digest`；
- 新的 search 结果出现后，旧 proposal 自动 stale；
- Booking Subagent 只能提出 proposal，不能产生外部副作用或批准自己的例外。

### Policy/Compliance Subagent

输入只包含 Booking proposal 和相关 Policy Store 记录。输出为 `ALLOW | REQUIRE_APPROVAL | DENY`。runtime 同时计算 deterministic verdict，最终采用更严格的结果：

```text
effective_decision = strictest(model_decision, runtime_decision)
```

有一个额外边界：如果模型临时提出 `REQUIRE_APPROVAL`，但 Policy Store 并没有定义对应 approval purpose 与 approver roles，runtime 将其视为 `DENY`。Agent 可以发现风险，但不能现场创造一条授权路径。

## 冲突与升级规则

| 冲突 | 最终决定者 | 结果 |
|---|---|---|
| Booking 推荐预算内酒店，Policy/runtime 均 ALLOW | Trip Orchestrator | 可按同一 proposal 执行 |
| Booking 推荐超预算酒店，Policy agent 误判 ALLOW，runtime 要求审批 | deterministic runtime | `REQUIRE_APPROVAL`，模型不能放宽政策 |
| Booking 推荐预算内酒店，Policy agent 有额外可验证风险并 DENY | Policy/Compliance Subagent | `DENY`，Orchestrator 必须换方案 |
| Policy 要求审批，但没有 Policy Store 定义的审批路径 | deterministic runtime | fail closed 为 `DENY` |
| 酒店超预算且存在明确审批政策 | manager / travel_admin | 只批准对应 `proposal_id`，不能复用到其他 proposal |
| 候选重新搜索、proposal 过期 | runtime freshness check | 必须重新 delegation + review |
| 候选 ID、role permission 或 proposal 不匹配 | deterministic runtime | 永不允许 human approval 覆盖事实或权限错误 |

冲突会产生 `agent_conflict` 事件；需要人工升级时产生 `escalation_required`。Session 持久化 delegation record、proposal、review、model/runtime verdict、policy refs、approver role 与 proposal scope。

## 为什么选择 agents-as-tools，而不是 handoff

OpenAI Agents SDK 将 multi-agent orchestration 主要分为 `Agent.as_tool()` 和 handoff 两类：前者适合 manager 保留控制权、调用 bounded specialist 并综合结果；后者让 specialist 接管本轮对话。这里 Trip Orchestrator 需要持续拥有 Checklist、HITL、最终报告和恢复入口，因此采用 agents-as-tools 语义更合适。

本项目没有直接引入 SDK 依赖，而是把它的设计模式吸收到现有自建 runtime：

- `Agent.as_tool()` → `delegate_booking_plan` / `delegate_policy_review`；
- structured agent input/output → Pydantic delegation contract；
- handoff input filtering → specialist 的最小 context projection；
- tool guardrails → book Skill 前的 deterministic governance gate；
- RunState/HITL → 已有可序列化 Session + proposal-scoped approval；
- tracing spans → delegation/conflict/escalation events + JSONL eval trace；
- usage aggregation → `subagent_model_calls` 与每次 delegation 的 token usage。

这样保留了“single-agent → multi-agent 使用同一套 harness 演进”的项目叙事，也避免为了框架迁移重写已经验证过的 durable runtime。

参考：

- [OpenAI Agents SDK: Agent orchestration](https://openai.github.io/openai-agents-python/multi_agent/)
- [OpenAI Agents SDK: Handoffs](https://openai.github.io/openai-agents-python/handoffs/)
- [OpenAI Agents SDK: Human-in-the-loop](https://openai.github.io/openai-agents-python/human_in_the_loop/)
- [OpenAI Agents SDK: Guardrails](https://openai.github.io/openai-agents-python/guardrails/)
- [OpenAI Agents SDK: Tracing](https://openai.github.io/openai-agents-python/tracing/)

## Sandbox 能不能吸收

可以借鉴，但不建议现在把 Sandbox Agents 接进主路径。

OpenAI SDK 的 Sandbox Agents 主要解决 filesystem、shell、workspace、snapshot 和 resume 隔离，适合 coding、文档处理或长时 workspace agent。商务差旅的关键风险是支付、身份、审批与外部 API 副作用；把代码放进 container 并不会自动解决这些问题。

当前项目真正需要的是 transaction sandbox：mock provider、幂等 key、dry-run、policy gate、HITL 和审计记录。这些能力已经在现有 integration boundary 中实现了一部分。只有未来加入“读取合同/票据、生成报销材料、处理本地文件”这类 workspace 子任务时，才值得把 Sandbox Agent 作为新的 specialist execution environment，并且仍需保留现有 Policy/HITL 边界。

## Eval

新增 `L9 Multi-Agent Governance`，并把下列规则纳入 deterministic checks / global invariants：

- successful booking 必须有同一 `proposal_id` 的 Booking delegation 与 Policy review；
- Policy model 不能放宽 deterministic runtime verdict；
- approval 必须和 proposal scope 匹配；
- stale proposal、invented candidate、DENY 后执行全部 fail closed；
- unauthorized approver 的“批准”记录为 requested approval，但 authoritative result 为 rejected。

