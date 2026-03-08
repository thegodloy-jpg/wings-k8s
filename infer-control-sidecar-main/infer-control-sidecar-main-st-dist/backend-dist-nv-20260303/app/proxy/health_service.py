# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: proxy/health_service.py
# Purpose: HTTP health service exposing aggregated status output for probe endpoints.
# Status: Active reused health API service.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Serve /health on dedicated health port.
# - Do not bypass the internal health state machine.
# -----------------------------------------------------------------------------
# -*- coding: utf-8 -*-
"""(no description)"""

from __future__ import annotations
import asyncio
import os
import time
from typing import Optional, Tuple
import httpx
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
import uvicorn

from app.proxy import settings as C
from app.proxy.health import (
    init_health_state,
    tick_observe_and_advance,
    map_http_code_from_state,
    build_health_body,
    teardown_health_monitor,
    _jittered_sleep_base,
)

#  health.py  build_health_headers
from app.proxy.health import build_health_headers

#  FastAPI
app = FastAPI()

#
HEALTH_SERVICE_PORT = int(os.getenv("HEALTH_SERVICE_PORT", "19000"))


@app.on_event("startup")
async def startup_event():
    """(no description)"""
    app.state.client = httpx.AsyncClient()
    app.state.health = init_health_state()
    #
    app.state.health_task = asyncio.create_task(health_monitor_loop(), name="health-monitor")


async def health_monitor_loop():
    """(no description)"""
    while True:
        try:
            await tick_observe_and_advance(app.state.health, app.state.client)
        except Exception as e:
            C.logger.warning("health_monitor_error: %s", e)
        await asyncio.sleep(_jittered_sleep_base(app.state.health))


@app.on_event("shutdown")
async def shutdown_event():
    """(no description)"""
    #  teardown_health_monitor  cancel
    await teardown_health_monitor(app)
    await app.state.client.aclose()


@app.get("/health")
async def health_check(minimal: bool = False):
    """

    :
        minimal=False    JSON
        minimal=True     HTTP HEAD
    """
    #
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
     HTTP
    """
    #
    h = app.state.health
    code = map_http_code_from_state(h)
    headers = build_health_headers(h)
    return Response(status_code=code, headers=headers)


#
def run_standalone():
    uvicorn.run(app, host="0.0.0.0", port=HEALTH_SERVICE_PORT)


if __name__ == "__main__":
    run_standalone()
