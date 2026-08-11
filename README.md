# Business Trip Execution Agent

> 一个会自主规划、遇到失败会自己重新决策、该停下来问人时真的会停下、进程重启也能接着跑的商务差旅 Agent —— 同时，这个仓库本身也是一次「AI 产品判断力」的具体呈现，而不只是一段能跑的代码。

**面向对象说明**：这份 README 和它引用的两份姊妹文档，主要写给评估 AI PM 候选人技术判断力的 Leader / 面试官看——目的是在尽量少占用你时间的前提下，把一次完整的产品判断链条摆出来给你核验，而不是让你去读代码猜我是怎么想的。

---

## 三份文档，回答三个不同的问题

这个仓库刻意拆成三份文档，各自独立成文、互不重复，你可以只读你关心的那一份：

| 你想验证什么 | 去哪看 | 预计耗时 |
|---|---|---|
| 这是不是一个真能跑起来的系统，项目里都有什么 | 本文档（README.md） | 3 分钟（见下方摘要） |
| 一次产品级 Agent 从场景选择、能力边界划分，到具体取舍的完整判断链条 | [`DESIGN.md`](./DESIGN.md) | 5 分钟起（文档开头有「如果只有五分钟」） |
| 怎么系统性地定义、验证一个 AI 产品的质量，出问题怎么归因 | [`EVAL_DESIGN.md`](./eval/EVAL_DESIGN.md) | 10 分钟起（文档开头有「如果只有十分钟」） |


### 如果你完全没有时间：5 个最值得看的判断点

1. **场景选择的判断**——为什么特意选一个「成熟」「不猎奇」的场景（商务差旅），把「这个场景成不成立」的说服成本降到最低，让评审精力能集中在设计判断本身上。→ `DESIGN.md` §1.2
2. **「模型决策」与「代码硬约束」的边界**——哪些事永远交给模型现场推理，哪些必须写死成不可绕过的安全护栏，以及这么划线的具体代价。→ `DESIGN.md` §3.5 / §4.2
3. **一次真实的 bug 复盘**——一个最初被当成「模型犯错」的问题，最后定位成系统状态机的设计缺陷；区分这两者本身就是一种需要刻意练习的判断力。→ `DESIGN.md` §7 Evolution 1
4. **评测场景怎么设计才不自欺欺人**——怎么把模型真正推到决策边界上，而不是造一个看起来吓人、实际留了安全出口的假测试。→ `EVAL_DESIGN.md` §4
5. **诚实的失败归因**——「这条风险这次没复现」和「这条风险这次没测到」是两种完全不同的结论，混为一谈会让整套评测结果失去意义。→ `EVAL_DESIGN.md` §5 / §6

---

## 这个项目在做什么（一句话）

给 Agent 一句话目标——「下周二安排去上海拜访客户，把事情办妥」——不给步骤，让它自己规划、查询、预订，在遇到航班超卖、酒店全部超预算这类真实失败时自己重新决策，该停下来等人批准时真的停下来，任务完成时给出一份分层报告。为什么这个看起来很适合写成固定流程的场景，反而是一个更适合展示 Agent 判断力的选择，完整论证见 `DESIGN.md` 第 1–2 章。

## Demo

**推理过程实时展示** —— 不是只显示「重试成功」这类结果性文字：

![Live reasoning](./docs/images/reasoning.png)

**人工审批卡片** —— 超预算或不可逆动作前暂停，等待用户点击批准 / 拒绝：

![Human approval](./docs/images/approval.png)

**执行层 / 决策层分层报告** —— 任务结束后区分「客观可验证的事实」和「需要人工复核的取舍」：

![Final report](./docs/images/report.png)

## 五项核心能力速览

完整的设计动机和取舍在 `DESIGN.md` 第 3、4 章，这里只列清单，避免重复论证：

| 能力 | 一句话 | 详见 |
|---|---|---|
| 自主规划 & 动态重规划 | 下一步做什么由模型现场读取工具原始返回结果推理决定，代码里没有一行业务分支 | §3.1 |
| 显式 Checklist 驱动的完成判定 | 任务是否完成看一份可勾选的清单，而不是「流程跑完就算完成」 | §3.2 |
| 人机协作审批 | 超预算 / 面向客户的不可逆动作前主动暂停，等待人工批准 | §3.3 |
| 可持久化 & 可断点恢复执行 | 暂停是真的落盘，不是靠一个还活着的进程硬撑；跨进程、跨连接、服务器重启都能续上 | §3.4 |
| 生产级安全护栏 | 超预算拦截、报告防幻觉核验写死在代码里，不赌模型自觉遵守 prompt | §3.5 |

## 评测体系速览

`harness.py` 目前覆盖 7 个质量维度（Task Success / Decision Quality / Safety / Recovery / Efficiency / Continuity / Robustness）、10 个测试场景，每次运行做三层互相独立的检查：确定性断言、全局不变量、LLM-as-judge。完整方法论——怎么设计场景才能把风险真正逼出来、怎么做失败归因、怎么区分「未复现」和「没测到」——见 `EVAL_DESIGN.md`。

## Architecture（一句话流程图）

```
Goal（一句话，不给步骤）
   ↓
Plan（模型自主拆解任务）→ Action（调用工具）→ Observation（原始返回）
   ↓
Verifier（对照 Checklist）
   ├─ 未满足 → 原始 Observation 喂回模型，回到 Plan
   ├─ 需要人工判断 → 落盘，暂停，等待 Human Approval → 读回状态继续
   └─ 全部满足 → 生成分层报告（含防幻觉核验）→ 落盘 → Done
```

**Tech stack**：FastAPI + WebSocket · 可重入 `run_agent_loop` + SQLite `session_store` 的持久化/断点续跑 · Qwen（OpenAI-compatible tool-use API） · 10 个 mock 业务工具 + 1 个审批工具 · `approval_utils.py` 承载运行时与评测共用的纯函数。

## Repo Map

```
business_trip_agent/
├── main.py                    # 核心 Agent（FastAPI + WebSocket + 可重入 run_agent_loop）
├── session_store.py           # Session 序列化 / SQLite 持久化
├── approval_utils.py          # 审批分类 / 报告核验 / 效率护栏 —— main.py 与 harness.py 共用的纯函数
├── test_session_store.py      # session_store 的序列化往返单测
├── requirements.txt
├── README.md                  # 本文档：项目导航 + 怎么跑起来
├── DESIGN.md                  # Agent 产品与系统设计：判断链条
├── .gitignore
├── eval/
│   ├── EVAL_DESIGN.md         # 评测体系设计：怎么定义与验证质量
│   ├── harness.py             # 评测框架：确定性断言 / 全局不变量 / LLM-as-judge
│   └── replay_harness.py      # 回放评估工具
├── static/
│   └── index.html             # 前端单页面
└── docs/
    └── images/                # 文档配图
```

> 评测运行产物（`eval_logs/`）、运行时会话存档（`sessions.db`）不纳入版本管理，属于本地调试/运行产物。

## Quick Start

```bash
git clone <this-repo>
cd business_trip_agent
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

在项目根目录创建 `.env`：

```
QWEN_API_KEY=your_key_here
```

运行：

```bash
python main.py
```

浏览器打开 `http://127.0.0.1:8000`，输入框已预填一个示例目标，点「开始任务」即可看到完整过程——包括推理过程实时展示、审批卡片、以及最终的分层报告。

### 可选环境变量

| 变量 | 默认值 | 作用 |
|---|---|---|
| `QWEN_MODEL` | `qwen-plus` | 主模型 |
| `QWEN_BACKUP_MODEL` | （空） | 主模型额度耗尽时自动切换的备用模型 |
| `QWEN_TIMEOUT_MAX_RETRIES` | `2` | 网关超时的业务层重试次数 |
| `QWEN_TIMEOUT_RETRY_BACKOFF_SECONDS` | `3` | 超时重试退避基数（秒） |
| `PROMPT_CACHE_MODE` | `off` | 设为 `explicit` 开启针对 system/tools 的显式 Prompt Caching |
| `SESSION_STORE_PATH` | `sessions.db` | Session 持久化的 SQLite 文件路径 |

### 跑评测

```bash
python harness.py
```

跑 `harness.py` 里定义的全部测试场景，生成一份包含确定性层 / 语义层 / 全局不变量 / Failure Taxonomy / Capability 覆盖率的 Markdown 报告。评测使用与生产环境完全隔离的 SQLite 文件（默认 `eval_logs/eval_sessions.db`），每次运行前清空重建。报告怎么解读、每个维度背后的方法论，见 `EVAL_DESIGN.md`。

支持的环境变量：`EVAL_CASE_FILTER`（只重跑指定 case id，逗号分隔）、`EVAL_LOG_DIR`（落盘每次运行的完整事件流，默认 `eval_logs`）、`EVAL_VERBOSE`（是否实时打印事件）、`EVAL_MAX_RESUME_ITERATIONS`（默认 40，防止暂停/恢复死循环）、`EVAL_BOUNDED_MAX_TURNS`（默认 6，仅用于死循环边界场景）。

### 跑持久化单测

```bash
python -m pytest test_session_store.py -v
```

验证 `Session` 序列化 / 反序列化往返一致性，包括 `seen_readonly_calls` 这类 JSON 原生不支持的 dict-tuple-key 字段。

---

如果你只想读设计判断本身、不需要跑代码：直接去 [`DESIGN.md`](./DESIGN.md) 和 [`EVAL_DESIGN.md`](./eval/EVAL_DESIGN.md)。
