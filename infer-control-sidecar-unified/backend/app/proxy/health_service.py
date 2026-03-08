# -*- coding: utf-8 -*-
"""独立健康服务。

与 gateway 中的 `/health` 不同，这个模块单独跑在健康端口上，
便于 Kubernetes 探针在 proxy 高负载时仍然可靠读取健康状态。
"""

from __future__ import annotations

import asyncio
import os

import httpx
import uvicorn
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from app.proxy import settings as C
from app.proxy.health import (
    _jittered_sleep_base,
    build_health_body,
    build_health_headers,
    init_health_state,
    map_http_code_from_state,
    teardown_health_monitor,
    tick_observe_and_advance,
)

# 单独的 FastAPI 应用，通常监听 `HEALTH_SERVICE_PORT`。
app = FastAPI()

# 独立健康服务对外监听端口，通常由 launcher 注入。
HEALTH_SERVICE_PORT = int(os.getenv("HEALTH_SERVICE_PORT", "19000"))


@app.on_event("startup")
async def startup_event():
    """初始化 http client、状态字典和后台轮询任务。"""
    app.state.client = httpx.AsyncClient()
    app.state.health = init_health_state()
    app.state.health_task = asyncio.create_task(health_monitor_loop(), name="health-monitor")


async def health_monitor_loop():
    """独立健康服务使用的后台健康轮询循环。"""
    while True:
        try:
            await tick_observe_and_advance(app.state.health, app.state.client)
        except Exception as e:
            C.logger.warning("health_monitor_error: %s", e)
        await asyncio.sleep(_jittered_sleep_base(app.state.health))


@app.on_event("shutdown")
async def shutdown_event():
    """关闭后台任务并回收 http client。"""
    await teardown_health_monitor(app)
    await app.state.client.aclose()


@app.get("/health")
async def health_check(minimal: bool = False):
    """返回完整或精简版健康信息。"""
    h = app.state.health
    code = map_http_code_from_state(h)
    headers = build_health_headers(h)

    if minimal:
        return Response(status_code=code, headers=headers)

    body = build_health_body(h, code)
    return JSONResponse(status_code=code, content=body, headers=headers)


@app.head("/health")
async def health_head():
    """为探针提供轻量级 HEAD 健康接口。"""
    h = app.state.health
    code = map_http_code_from_state(h)
    headers = build_health_headers(h)
    return Response(status_code=code, headers=headers)


def run_standalone():
    """便于本地调试时直接单独启动健康服务。"""
    uvicorn.run(app, host="0.0.0.0", port=HEALTH_SERVICE_PORT)


if __name__ == "__main__":
    run_standalone()
