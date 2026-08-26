# 电商售后 Multi-Agent 决策复盘

当初放弃电商售后 multi-agent，不是因为 task decomposition 没价值，而是因为退款、补偿和资金动作一旦跨 agent 分散执行，责任边界、审批状态和最终账务结果很难形成一条可审计链路。在没有 deterministic policy guardrail、durable HITL 和统一 trace 的前提下，多一个 agent 会增加协调表面积，却不会增加可信度。

Business Trip Agent 的验证改变了可选方案，但没有推翻当初的判断。现在可以在以下边界内重新评估：

- 售后 Triage Agent 只负责问题分类和证据整理；
- Resolution Agent 只提出退款、补偿或换货 proposal，不直接触发资金动作；
- Policy/Risk Agent 根据订单状态、金额阈值和用户风险输出 review；
- deterministic runtime 取模型判断与业务规则中更严格的结果；
- 超阈值资金动作进入 proposal-scoped HITL，由有权限的人审批；
- Payment/Refund Skill 仍是唯一资金执行边界，并承担幂等、权限与审计。

因此新的判断不是“multi-agent 现在更先进，所以应该使用”，而是：只有当 delegation protocol、conflict resolution、approval scope 和 side-effect boundary 都可验证时，multi-agent 才能从额外风险变成有效协作。下一步应先用历史售后 case 做 replay eval，比较 single-agent 与 governed multi-agent 的任务成功率、资金错误率、token cost 和 approval burden，再决定是否迁移。
