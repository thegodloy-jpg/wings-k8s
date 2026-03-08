# -*- coding: utf-8 -*-
"""
独立的健康检查服务
提供轻量级的健康检查接口，避免与主网关服务竞争资源
"""

from __future__ import annotations
import asyncio
import os
import time
from typing import Optional, Tuple
import httpx
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
import uvicorn

from wings.proxy import settings as C
from wings.proxy.health import (
    init_health_state,
    tick_observe_and_advance,
    map_http_code_from_state,
    build_health_body,
    teardown_health_monitor,
    _jittered_sleep_base,
)

# 从 health.py 中导入 build_health_headers 函数
from wings.proxy.health import build_health_headers

# 初始化 FastAPI 应用
app = FastAPI()

# 健康检查服务端口
HEALTH_SERVICE_PORT = int(os.getenv("HEALTH_SERVICE_PORT", "19000"))


@app.on_event("startup")
async def startup_event():
    """启动健康监控循环"""
    app.state.client = httpx.AsyncClient()
    app.state.health = init_health_state()
    # 启动后台健康检查循环
    app.state.health_task = asyncio.create_task(health_monitor_loop(), name="health-monitor")


async def health_monitor_loop():
    """后台健康检查循环"""
    while True:
        try:
            await tick_observe_and_advance(app.state.health, app.state.client)
        except Exception as e:
            C.logger.warning("health_monitor_error: %s", e)
        await asyncio.sleep(_jittered_sleep_base(app.state.health))


@app.on_event("shutdown")
async def shutdown_event():
    """关闭健康监控循环"""
    # 取消后台任务
    if hasattr(app.state, 'health_task'):
        app.state.health_task.cancel()
        try:
            await app.state.health_task
        except asyncio.CancelledError:
            pass
    # 先关闭客户端，再调用teardown_health_monitor
    await app.state.client.aclose()
    await teardown_health_monitor(app)


@app.get("/health")
async def health_check(minimal: bool = False):
    """
    独立的健康检查接口
    参数:
        minimal=False  → 返回完整 JSON（含状态详情）
        minimal=True   → 仅返回 HTTP 状态码与响应头（HEAD 风格）
    """
    # 使用最新的健康状态，不需要重新检查
    h = app.state.health
    code = map_http_code_from_state(h)
    headers = build_health_headers(h)
    
    if minimal:
        return Response(status_code=code, headers=headers)
    
    body = build_health_body(h, code)
    return JSONResponse(status_code=code, content=body, headers=headers)


@app.head("/health")
async def health_head():
    """
    HEAD /health  
    仅返回 HTTP 状态码与响应头，无响应体，用于轻量级探活。
    """
    # 使用最新的健康状态，不需要重新检查
    h = app.state.health
    code = map_http_code_from_state(h)
    headers = build_health_headers(h)
    return Response(status_code=code, headers=headers)


# 用于独立运行的入口
def run_standalone():
    uvicorn.run(app, host="0.0.0.0", port=HEALTH_SERVICE_PORT)


if __name__ == "__main__":
    run_standalone()