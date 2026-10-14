# 商务拜访执行 Agent：贡献与验证记录

[返回项目首页](../README.md) · [开发指南](IMPLEMENTATION.md)

## 本次实际验证：2026-10-14

基准代码：`afe5e0060aca80bf4298b6bbf665d2a1a0688191`。本轮仅修改展示文档，未改动运行代码、测试、依赖声明或政策配置。

在 Linux、Python 3.12.14 的独立虚拟环境中，从仓库根目录运行现有测试。使用独立 SQLite 路径与 pytest cache；实际解释器为 `/workspace/.cloud-setup/business-venv/bin/python`。等价复现命令：

```bash
SESSION_STORE_PATH=/tmp/business-review-sessions.db \
MEMORY_STORE_PATH=/tmp/business-review-memories.db \
python -m pytest tests/ -q -o cache_dir=/tmp/business-review-pytest-cache
```

本次 runner 输出：

```text
35 passed in 0.50s
```

测试不调用真实模型或业务系统。完整 Eval Harness、真实日历 MCP、真实预订/发信和 live 任务链本次均未运行；不据此报告业务任务成功率。

## 成果陈述与可核对证据

| 陈述 | 实现入口 | 现有验证入口 | 证明范围 |
| --- | --- | --- | --- |
| 审批等待时可以保存 Session，恢复所需状态可读回 | [main.py](../main.py)、[session_store.py](../session_store.py) | [Session round-trip tests](../tests/test_session_store.py) | 状态序列化、覆盖、版本兼容；不是一次真实长时任务的录像 |
| multi-agent 不能绕过 deterministic policy，审批绑定 proposal | [multi_agent.py](../multi_agent.py) | [治理测试](../tests/test_multi_agent.py) | 覆盖 stale proposal、伪造候选、未授权审批、预算约束和受控执行；模型调用使用替身 |
| Context 压缩不破坏 tool call/result 配对，Memory 不覆盖政策 | [context_manager.py](../context_manager.py)、[memory_store.py](../memory_store.py) | [Context tests](../tests/test_context_manager.py)、[Memory tests](../tests/test_memory_store.py) | 确定性语义与边界；不是长任务表现或准确率统计 |
| 日历接口区分 mock / MCP，权限失败和超时不能当作有空 | [calendar_adapter.py](../integrations/calendar_adapter.py) | [Calendar tests](../tests/test_calendar_adapter.py) | adapter contract 与受控错误路径；本次未授权真实账号 |
| Eval 区分任务结果、规则约束和语义判断，并支持 replay | [harness.py](../eval/harness.py)、[replay_harness.py](../eval/replay_harness.py) | [Eval 设计](../eval/EVAL_DESIGN.md) | 12 个 case / 默认 26 次是定义与配置；本次未执行这 26 次 live/model runs |

## 历史展示与缺少的材料

已有 [审批](images/approval.png)、[过程](images/reasoning.png)、[结果报告](images/report.png) 三张截图。它们说明 UI、人工介入和报告形态，不含公开 run ID 或版本绑定，不能独立证明最新实现的完整执行链。

原说明中“个人日历可通过测试账号只读接入”对应的是已有适配器与 smoke 入口 [mcp_smoke_test.py](../eval/mcp_smoke_test.py)。本仓库未提供可核对的真实账号响应记录，本次也未进行 live 复验。客户日历、邮件、日历写入及机酒预订仍为 mock/sandbox。

需要补齐：完整 Demo 与版本/run ID 对应关系；经脱敏的 Eval 结果与失败案例轨迹；真实只读日历的来源、权限范围和验收记录。没有这些材料时，不声称生产部署、真实业务写入或线上效果提升。

## 自主实现与复用边界

自建的是业务 runtime、持久化、审批治理、Skill contract、Memory/context 机制与 Eval；模型来自 Qwen，服务与模型客户端复用现有 Python 库。multi-agent 采用 agents-as-tools 思路，但没有直接集成 OpenAI Agents SDK，依据见 [设计说明](MULTI_AGENT_DESIGN.md)。项目使用 AI Coding 辅助原型实现，贡献表描述可在仓库核对的设计与实现范围。
