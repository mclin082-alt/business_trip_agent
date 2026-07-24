# Phase 0 运行说明

## 0. 前置步骤

正常,Mac 上系统 Python 是"受保护"的,不让你直接装包,用虚拟环境就行,三行命令解决:

```bash
cd /Users/lll/Desktop/business_trip_agent
python3 -m venv venv
source venv/bin/activate
```

执行完最后一句,命令行提示符前面会多出一个 `(venv)`,这表示你现在用的是一个"隔离出来的干净 Python",装什么都不影响系统。然后正常安装和运行:

```bash
pip install -r requirements.txt
python main.py
```

**注意**:以后每次要跑这个项目,新开一个终端窗口后,都要先 `cd` 到项目文件夹再执行 `source venv/bin/activate`,不然会提示 `command not found: pip`(因为虚拟环境只在当前终端窗口生效,关掉窗口就退出了)。

跑起来后浏览器打开 `http://127.0.0.1:8000` 应该就能看到消息了。如果这几步还有报错,把报错信息贴给我。

## 1. 安装依赖
在项目文件夹下打开终端：

```bash
pip install -r requirements.txt
```

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

- 应该看到页面顶部有个小圆点从灰色变绿色，文字变成"已连接"
- 下方日志区域每隔一秒自动出现一条"步骤 N 完成"，不需要手动刷新页面

## 文件说明

| 文件 | 作用 |
|---|---|
| `main.py` | 后端：启动 FastAPI 服务，`/` 返回首页 HTML，`/ws` 是 WebSocket 接口，每秒推一条假消息 |
| `static/index.html` | 前端：打开页面后用 JS 连接 `/ws`，收到消息就插入到日志区域 |
| `requirements.txt` | 依赖清单：fastapi（网页框架）、uvicorn（跑这个网页框架的服务器）、websockets |

## 这一步在验证什么

请求路径是：浏览器打开页面 → 页面里的 JS 主动连接 `ws://.../ws` → 触发 `main.py`
里的 `websocket_endpoint` 函数 → 这个函数每隔一秒调用一次 `send_text` → 浏览器的
`ws.onmessage` 收到后把文字塞进页面。

这条链路一旦跑通，后面 Phase 1（接入 Claude API）只需要把"假消息"换成"调用
Claude 拿到的真实回复"，页面这一侧的展示逻辑完全不用改。

## 下一步（Phase 1 预告）
给页面加一个输入框和按钮，点击后把输入内容发给 Claude API，把回复通过同一条
WebSocket 推回页面 —— 验证"网页 ↔ 后端 ↔ Claude API"三方连通。
