# Phase 1 运行说明

Phase 0 验证的是"网页能不能实时收到后端推送"，Phase 1 在这基础上，把假消息
换成了"网页输入一句话 → 后端转发给千问 API → 拿到真实回复 → 推回网页"。

## 0. 配置你的千问 API Key

项目根目录有个 `.env.example` 文件，复制一份改名叫 `.env`：

```bash
cd /Users/lll/Desktop/business_trip_agent
cp .env.example .env
```

然后用文本编辑器打开 `.env`，把 `QWEN_API_KEY=你的key填在这里` 这一行换成你
自己在阿里云百炼控制台申请到的真实 Key，其它两行（`QWEN_BASE_URL`、
`QWEN_MODEL`）先不用动，保存关闭即可。

**关于你给的两个地址**：`compatible-mode/v1` 是 OpenAI 兼容模式的接口，
`apps/anthropic` 是 Anthropic 兼容模式的接口。Phase 1 代码用的是前者
（`compatible-mode/v1`），因为可以直接用官方 `openai` 这个 Python 包调用，
最省事；后面 Phase 2 要用"工具调用"，这条兼容模式下 Qwen 对应的是
`function calling`，也是成熟支持的，所以先在这条路上打好地基，后面不用换。
如果你后面想换成 Anthropic 兼容模式，把 `.env` 里的 `QWEN_BASE_URL` 换成
`.../apps/anthropic`，并把 `main.py` 里的 `OpenAI` 客户端换成
`anthropic` 官方 SDK 即可，调用方式是类似的，我可以到时候再帮你改。

## 1. 安装依赖

虚拟环境还是老规矩，新开终端窗口都要先激活：

```bash
cd /Users/lll/Desktop/business_trip_agent
source venv/bin/activate
pip install -r requirements.txt
```

这次 `requirements.txt` 比 Phase 0 多了两行：`openai`（调用 Qwen 用的，
Qwen 的 OpenAI 兼容模式可以直接复用这个官方包）、`python-dotenv`（读取
`.env` 文件里的 Key，不用把密钥写死在代码里）。

## 2. 运行

```bash
python main.py
```

看到类似输出说明启动成功：
```
Uvicorn running on http://127.0.0.1:8000
```

## 3. 验收

浏览器打开 http://127.0.0.1:8000

- 顶部圆点变绿、文字变"已连接"（这部分和 Phase 0 一样）
- 页面下方多了一个输入框和"发送"按钮
- 输入框里打"你好"，点发送，几秒内页面上依次出现：
  1. 你刚才说的"你好"（回显，标签是"你"）
  2. 一条"思考中…"的提示（这段时间后端正在等 Qwen 的网络响应）
  3. Qwen 的真实回复文字（标签是"Qwen"）
- 如果 `.env` 里 Key 没填或填错，页面上会直接显示带 ⚠️ 的报错原因，不会白屏卡死

**这一步验证的是**："网页 ↔ 你的代码 ↔ Qwen API" 三方连通，不涉及任何业务
逻辑、不涉及工具调用、也没有循环——这些都是 Phase 2 才加的东西。

## 文件说明

| 文件 | 作用 |
|---|---|
| `main.py` | 后端：`/` 返回首页 HTML，`/ws` 收到用户输入后调用千问 API，把回复推回页面 |
| `static/index.html` | 前端：输入框 + 发送按钮，通过 WebSocket 和后端来回传消息 |
| `requirements.txt` | 依赖清单 |
| `.env.example` | Key 配置模板，复制为 `.env` 后填真实 Key |
| `.env` | 你自己创建，存放真实 Key，**不要分享给别人** |

## 常见问题

- **报错 `Incorrect API key` 或 401**：检查 `.env` 里 `QWEN_API_KEY` 是否复制完整，
  有没有多余空格或引号。
- **报错 `model not found` 或类似 404**：说明 `QWEN_MODEL` 这个型号名在你的账号下
  不可用，去阿里云百炼控制台确认一下你实际能调用的模型名称，改到 `.env` 里的
  `QWEN_MODEL`。
- **一直卡在"思考中…"不动**：大概率是网络问题（比如公司/学校网络屏蔽了这个域名），
  或者 `QWEN_BASE_URL` 填错了，检查后台终端窗口里有没有打印出具体的报错信息。

## 下一步（Phase 2 预告）

现在是"问一句、答一句"的单轮对话。Phase 2 要做的是让 Qwen 具备"工具调用"
能力：定义 `search_flight`、`request_human_approval` 这类假工具，让 Qwen
自己判断"现在该调用哪个工具、传什么参数"，代码只负责执行它决定的动作再把
结果丢回去——这一步是整个方案"Agent 不是 Workflow"这个核心判断标准的第一次
真实验证。
