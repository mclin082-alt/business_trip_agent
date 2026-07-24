"""
Phase 0: 项目骨架 + 最简单的"能跑起来看得见"

目标：验证"写 Python 代码 → 浏览器能看到网页 → 网页内容能实时更新"这条链路。
这一步不接 LLM，只用一个假的循环，每隔一秒往网页推一条假消息。
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="商务拜访执行 Agent - Phase 0")

# 把 static 目录挂载出去，方便以后放 css/js（现在只有 index.html 用得上）
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    """打开浏览器看到的首页。"""
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    网页请求发到这里的路径：
    浏览器打开页面 → index.html 里的 JS 建立 WebSocket 连接到 /ws
    → 这个函数被触发 → 服务器每隔一秒往这个连接推一条消息
    → 浏览器端 JS 收到消息后把它插入页面 DOM，页面因此"自动更新"而不需要手动刷新
    """
    await websocket.accept()
    step = 1
    try:
        while True:
            await asyncio.sleep(1)
            message = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "text": f"步骤 {step} 完成",
            }
            # 用 JSON 字符串传输，方便以后传更复杂的结构（比如 Plan/Action/Observation）
            await websocket.send_text(json.dumps(message, ensure_ascii=False))
            step += 1
    except WebSocketDisconnect:
        # 浏览器关掉页面或刷新时会走到这里，属于正常情况，不用当成错误处理
        print("客户端断开连接")


if __name__ == "__main__":
    import uvicorn

    # reload=True: 改代码保存后服务器自动重启，开发时更方便
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
