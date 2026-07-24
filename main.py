"""
Phase 1: 接入 Qwen API（阿里云百炼 MaaS，OpenAI 兼容模式），跑通"一次调用"

目标：不做循环、不做工具，只验证"网页上有个按钮，点一下，发一句话给 Qwen，
把回复显示在网页上"这个最小闭环。

对应实施计划 v2 里说的"网页 ↔ 你的代码 ↔ Claude API 三方连通"——这里把
Claude API 换成了 Qwen API，其它链路完全一样。
"""

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

# 从项目根目录的 .env 文件里读取 QWEN_API_KEY 等配置（.env 不会被提交/分享，安全）
load_dotenv()

app = FastAPI(title="商务拜访执行 Agent - Phase 1")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ------------------------------------------------------------------
# Qwen API 配置
#
# 你提供了两个地址：
#   1) .../compatible-mode/v1   → OpenAI 兼容模式（/chat/completions 格式）
#   2) .../apps/anthropic       → Anthropic 兼容模式（/v1/messages 格式）
#
# Phase 1 用的是第 1 个（compatible-mode/v1），因为它可以直接用官方 openai
# 这个 Python 包调用，代码最简单、最稳；后面 Phase 2 要用到"工具调用/tool use"，
# OpenAI 兼容模式下叫 function calling，Qwen 对这个格式支持是成熟的，
# 所以先在这条路径上把地基打好，后面不用换路子。
# 如果你后面想改用 anthropic 兼容模式，把 base_url 换成 .../apps/anthropic，
# 并把下面 OpenAI 客户端换成 anthropic 官方 SDK 即可，调用逻辑是类似的。
# ------------------------------------------------------------------
QWEN_API_KEY = os.environ.get("QWEN_API_KEY")
QWEN_BASE_URL = os.environ.get(
    "QWEN_BASE_URL",
    "https://ws-76amum4g5z46vcjk.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
)
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen3.7-max")

client: OpenAI | None = None
if QWEN_API_KEY:
    client = OpenAI(api_key=QWEN_API_KEY, base_url=QWEN_BASE_URL)


@app.get("/", response_class=HTMLResponse)
async def index():
    """打开浏览器看到的首页。"""
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


def _call_qwen_sync(user_text: str) -> str:
    """
    真正发请求的地方（同步函数，因为 openai 这个包是同步阻塞的）。
    Phase 1 只做最简单的单轮问答，不带历史、不带工具，先把连通性跑通。
    """
    response = client.chat.completions.create(
        model=QWEN_MODEL,
        messages=[
            {"role": "system", "content": "你是一个商务拜访执行 Agent 的测试助手，简洁回复即可。"},
            {"role": "user", "content": user_text},
        ],
    )
    return response.choices[0].message.content


async def call_qwen(user_text: str) -> str:
    """
    异步包一层：FastAPI 的事件循环是单线程的，如果直接在这里同步调用
    client.chat.completions.create（网络请求，可能要等几秒），会把整个
    服务器卡住、连别的请求都处理不了。用 asyncio.to_thread 扔到线程池里跑，
    不阻塞主循环。
    """
    if client is None:
        return (
            "⚠️ 还没配置 QWEN_API_KEY，请在项目根目录新建 .env 文件，"
            "参考 .env.example 填入你的 API Key 后重启程序。"
        )
    try:
        return await asyncio.to_thread(_call_qwen_sync, user_text)
    except Exception as exc:  # noqa: BLE001 - Phase 1 先简单兜底，方便你看到报错原因
        return f"⚠️ 调用 Qwen API 失败：{exc}"


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    请求路径：
    浏览器输入框输入文字、点"发送" → 页面 JS 通过这个 WebSocket 连接发一条
    {"type": "user_message", "text": "..."} 消息
    → 这个函数收到后调用 call_qwen()
    → 拿到 Qwen 的回复
    → 通过同一个 WebSocket 把 {"type": "assistant_message", "text": "..."} 发回去
    → 浏览器 JS 收到后把文字插入页面。

    这一步只验证"网页 ↔ 后端 ↔ Qwen API"三方连通，不涉及任何业务逻辑、
    不涉及工具调用，那些是 Phase 2 的事。
    """
    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if payload.get("type") != "user_message":
                continue

            user_text = (payload.get("text") or "").strip()
            if not user_text:
                continue

            # 先把用户自己发的话回显到日志里，这样对话顺序在页面上看得清楚
            await websocket.send_text(json.dumps(
                {"type": "user_message", "text": user_text}, ensure_ascii=False
            ))

            # Qwen 请求可能要等几秒，先推一条"思考中"状态，页面上会显示 loading 效果
            await websocket.send_text(json.dumps({"type": "thinking"}, ensure_ascii=False))

            reply_text = await call_qwen(user_text)

            await websocket.send_text(json.dumps(
                {"type": "assistant_message", "text": reply_text}, ensure_ascii=False
            ))
    except WebSocketDisconnect:
        print("客户端断开连接")


if __name__ == "__main__":
    import uvicorn

    # reload=True: 改代码保存后服务器自动重启，开发时更方便
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
