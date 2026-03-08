# =============================================================================
# 文件: proxy/simple_proxy.py
# 用途: 简化版代理实现，保留用于回退和测试场景
# 状态: 兼容路径，非 launcher MVP 主路径
#
# 功能概述:
#   本模块是简化版的代理实现，主要特点：
#   - 支持 /v1/chat/completions 等核心接口
#   - 复用 HTTP keepalive 连接池
#   - 测量 TTFT (Time to First Token) 指标
#   - 不使用 HTTP/2，配置更简单
#   - 透传原始 HTTP 状态码
#
# 与 gateway.py 的差异:
#   - 无复杂的双闸门排队控制
#   - 无观测头注入
#   - 简化的重试策略
#
# Sidecar 架构契约:
#   - 保持兼容性行为不变
#   - 避免为此回退路径引入新依赖
#
# =============================================================================
# -*- coding: utf-8 -*-
"""简化版代理实现。

主要用途:
- 回退场景: 当主 gateway 不可用时的备用路径
- 测试场景: 简单的端到端测试
- TTFT 测量: 首包延迟监控
- 轻量级部署: 不需要复杂流控功能时使用
"""

import os
import asyncio
import time
import random
import inspect
from typing import Dict
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

#
from app.proxy import settings as C
from app.proxy.tags import build_backend_url, jlog, elog, read_json_body
from app.proxy.queueing import QueueGate

#  TTFT
#  yield
FAST_PATH_BYTES = C.FAST_PATH_BYTES

#  Flush
FIRST_FLUSH_BYTES = C.FIRST_FLUSH_BYTES
FIRST_FLUSH_MS = C.FIRST_FLUSH_MS

#  Flush
STREAM_FLUSH_BYTES = C.STREAM_FLUSH_BYTES
STREAM_FLUSH_MS = C.STREAM_FLUSH_MS

#  flush\n\n
ENABLE_DELIM_FLUSH = C.ENABLE_DELIM_FLUSH

#
MAX_CONN = C.MAX_CONN
MAX_KEEPALIVE = C.MAX_KEEPALIVE
KEEPALIVE_EXPIRY = C.KEEPALIVE_EXPIRY

#
MAX_REDIRECTS = int(os.getenv("HTTPX_MAX_REDIRECTS", "20"))
VERIFY_SSL = os.getenv("HTTPX_VERIFY_SSL", "true").lower() != "false"
TRUST_ENV = os.getenv("HTTPX_TRUST_ENV", "false").lower() != "false"

#
WARMUP_ENABLED = os.getenv("WARMUP_ENABLED", "false").lower() != "false"
WARMUP_CONN = C.WARMUP_CONN
WARMUP_PROMPT = C.WARMUP_PROMPT
WARMUP_ROUNDS = C.WARMUP_ROUNDS
WARMUP_TIMEOUT = C.WARMUP_TIMEOUT
WARMUP_MODEL = os.getenv("WARMUP_MODEL", "default-model")

#
DISABLE_MIDDLE_BUFFER = os.getenv("DISABLE_MIDDLE_BUFFER", "true").lower() != "false"

#
RETRY_TRIES = C.RETRY_TRIES
RETRY_INTERVAL_MS = C.RETRY_INTERVAL_MS

# DoS
MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", str(2 * 1024 * 1024)))

#  httpx send()  timeout
_SEND_HAS_TIMEOUT = "timeout" in inspect.signature(httpx.AsyncClient.send).parameters

#  5xx""
_RETRIABLE_EXC = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_RETRIABLE_5XX = (502, 503, 504)

#  FastAPI
app = FastAPI()

#  HTTP keepalive
client = httpx.AsyncClient(
    limits=httpx.Limits(
        max_connections=MAX_CONN,
        max_keepalive_connections=MAX_KEEPALIVE,
        keepalive_expiry=KEEPALIVE_EXPIRY,
    ),
    timeout=httpx.Timeout(connect=float(os.getenv("HTTPX_CONNECT_TIMEOUT", "10")), read=None, write=None, pool=None),
    max_redirects=MAX_REDIRECTS,
    verify=VERIFY_SSL,
    trust_env=TRUST_ENV,
)


@app.on_event("startup")
async def startup():
    """(no description)"""
    jlog("proxy_startup", host="0.0.0.0", port=C.PORT, backend_url=C.BACKEND_URL)

    #
    app.state.gate = QueueGate()

    #
    if WARMUP_ENABLED:
        await warmup_connections()


async def warmup_connections():
    """

    -
    -
    """
    jlog("warmup_start", conn_count=WARMUP_CONN, rounds=WARMUP_ROUNDS, model=WARMUP_MODEL)

    backend_url = build_backend_url("/v1/chat/completions")

    for round_num in range(1, WARMUP_ROUNDS + 1):
        jlog("warmup_round", round=round_num, total=WARMUP_ROUNDS)

        #
        tasks = []
        for i in range(WARMUP_CONN):
            task = asyncio.create_task(
                send_warmup_request(backend_url, i, round_num)
            )
            tasks.append(task)

        #
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=WARMUP_TIMEOUT
            )
        except asyncio.TimeoutError:
            elog("warmup_timeout", round=round_num, timeout=WARMUP_TIMEOUT)

    jlog("warmup_complete", total_conn=WARMUP_CONN * WARMUP_ROUNDS)


async def send_warmup_request(backend_url: str, conn_id: int, round_num: int):
    """(no description)"""
    try:
        warmup_data = {
            "model": WARMUP_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": WARMUP_PROMPT
                }
            ],
            "stream": True,
            "max_tokens": 10  #  token
        }

        headers = {
            "content-type": "application/json",
            "accept-encoding": "identity",
            "connection": "keep-alive",
        }

        #
        r = await client.post(
            backend_url,
            json=warmup_data,
            headers=headers,
            timeout=httpx.Timeout(connect=5.0, read=5.0, write=None, pool=None),
        )

        #
        if r.status_code == 200:
            async for chunk in r.aiter_bytes(chunk_size=1024):
                break  #  chunk
            await r.aclose()
            jlog("warmup_success", conn_id=conn_id, round=round_num)
        else:
            elog("warmup_failed", conn_id=conn_id, round=round_num, status_code=r.status_code)
            await r.aclose()

    except Exception as e:
        elog("warmup_error", conn_id=conn_id, round=round_num, error_type=e.__class__.__name__, detail=str(e))


@app.on_event("shutdown")
async def shutdown():
    """(no description)"""
    await client.aclose()
    jlog("proxy_shutdown")


#

async def _raw_send(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    content: bytes | None = None,
    headers: dict | None = None,
    stream: bool = False,
    timeout: httpx.Timeout | None = None,
) -> httpx.Response:
    """
     httpx
    -  request
    -  stream
    -  httpx  timeout
    """
    req = client.build_request(method, url, content=content, headers=headers)
    if _SEND_HAS_TIMEOUT and timeout is not None:
        return await client.send(req, stream=stream, timeout=timeout)
    return await client.send(req, stream=stream)


def _should_retry_status(stream: bool, status_code: int, attempt: int, total: int) -> bool:
    return stream and status_code in _RETRIABLE_5XX and attempt < total


async def _close_resp_quiet(resp: httpx.Response) -> None:
    try:
        await resp.aclose()
    except Exception as e:
        elog("failed_to_close_response", error=str(e))


def _mark_retry_count(resp: httpx.Response, attempt: int) -> None:
    try:
        resp.extensions["app_retry_count"] = attempt - 1
    except Exception as e:
        elog("failed_to_set_retry_count", error=str(e))


async def _log_and_wait_status_retry(rid: str | None, attempt: int, status: int, interval: float, t0: float) -> None:
    elog(
        "retry_status",
        rid=rid, attempt=attempt, status=status,
        next_wait_ms=int(interval * 1000), elapsed=f"{(time.perf_counter() - t0) * 1000:.1f}ms",
    )
    await asyncio.sleep(interval)


def _is_retriable_exception(e: Exception) -> bool:
    return isinstance(e, _RETRIABLE_EXC)


async def _log_and_maybe_wait_exception(e: Exception, **ctx) -> bool:
    rid = ctx.get("rid")
    attempt = ctx.get("attempt")
    total = ctx.get("total")
    interval = ctx.get("interval")
    t0 = ctx.get("t0")

    retriable = _is_retriable_exception(e)
    elog(
        "retry_exception",
        rid=rid,
        attempt=attempt,
        err_type=e.__class__.__name__,
        detail=str(e),
        retriable=retriable,
        next_wait_ms=(int(interval * 1000) if retriable and attempt < total else 0),
        elapsed=f"{(time.perf_counter() - t0) * 1000:.1f}ms",
    )
    if retriable and attempt < total:
        await asyncio.sleep(interval)
        return True
    return False


async def _send_with_fixed_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    content: bytes | None = None,
    headers: dict | None = None,
    stream: bool = False,
    timeout: httpx.Timeout | None = None,
    rid: str | None = None,
) -> httpx.Response:
    """
    ""
    -  RETRY_TRIES=3 + RETRY_INTERVAL_MS=100ms
    -
        1) ConnectError / ConnectTimeout / PoolTimeout
        2)  502/503/504
    -  resp.extensions["app_retry_count"]
    """
    total = max(1, int(RETRY_TRIES))
    interval = max(0, int(RETRY_INTERVAL_MS)) / 1000.0
    last_exc: Exception | None = None
    last_status: int | None = None

    for attempt in range(1, total + 1):
        t0 = time.perf_counter()
        try:
            resp = await _raw_send(
                client, method, url,
                content=content, headers=headers, stream=stream, timeout=timeout
            )

            # " & " 5xx
            if _should_retry_status(stream, resp.status_code, attempt, total):
                last_status = resp.status_code
                await _close_resp_quiet(resp)
                await _log_and_wait_status_retry(rid, attempt, last_status, interval, t0)
                continue

            #  extensions  -> 0
            _mark_retry_count(resp, attempt)
            return resp

        except Exception as e:
            last_exc = e
            #
            if await _log_and_maybe_wait_exception(e, rid=rid, attempt=attempt, total=total, interval=interval, t0=t0):
                continue

            #  httpx.RequestError  502
            if isinstance(e, httpx.RequestError):
                raise HTTPException(502, f"backend connect error: {e}") from e
            raise

    #
    if last_exc is not None:
        if isinstance(last_exc, httpx.RequestError):
            elog("retry_final_fail", rid=rid, tries=total, final_error=str(last_exc))
            raise HTTPException(502, f"backend connect error: {last_exc}") from last_exc
        elog("retry_final_fail", rid=rid, tries=total, final_error=str(last_exc))
        raise last_exc
    if last_status is not None:
        elog("retry_final_fail", rid=rid, tries=total, final_status=last_status)
        raise HTTPException(502, f"backend 5xx after retries: {last_status}")
    elog("retry_final_fail", rid=rid, tries=total, reason="unknown")
    raise HTTPException(502, "backend error after retries")


#  TTFT


#


async def _acquire_gate_early(req: Request, gate: QueueGate, rid: str) -> Dict[str, str]:
    """

     HTTPException
    """
    queue_headers = await gate.acquire(dict(req.headers))
    await gate.release()
    jlog("gate_acquired_released_early", rid=rid, wait_hdr=queue_headers.get("X-Queued-Wait"))
    return queue_headers


async def _send_stream_request(
    client: httpx.AsyncClient,
    upstream_path: str,
    body_bytes: bytes,
    req: Request,
    rid: str,
) -> httpx.Response:
    """(no description)"""
    url = build_backend_url(upstream_path)
    return await _send_with_fixed_retries(
        client, "POST", url,
        content=body_bytes,
        headers={k: v for k, v in req.headers.items() if k.lower() != "host"},
        stream=True,
        timeout=httpx.Timeout(connect=float(os.getenv("HTTPX_CONNECT_TIMEOUT", "10")), read=None, write=None, pool=None),
        rid=rid,
    )


def _build_passthrough_headers(r: httpx.Response, gate: QueueGate, queue_headers: Dict[str, str]) -> Dict[str, str]:
    """

    -  X-*
    -
    -
    -
    """
    headers = {k: v for k, v in r.headers.items() if k.lower().startswith("x-")}

    ext = getattr(r, "extensions", None)
    if isinstance(ext, dict):
        retry_cnt = ext.get("app_retry_count")
        if retry_cnt is not None:
            headers["X-Retry-Count"] = str(retry_cnt)

    #
    headers.update(gate.obs_headers(queue_headers))

    #
    if DISABLE_MIDDLE_BUFFER:
        headers["X-Accel-Buffering"] = "no"  #  Nginx
        headers["Cache-Control"] = "no-cache, no-store, no-transform, must-revalidate"  #

    return headers


async def _stream_gen(req: Request, r: httpx.Response, rid: str, request_start_time: float):
    """
    TTFT
    -  yield
    -  flush +  +
    -  flush +  +
    -  TTFTTotal Time  chunk
    """
    buf = bytearray()
    last_flush = time.perf_counter()
    first_flush_done = False
    dyn_base = max(STREAM_FLUSH_BYTES, int(MAX_CONN / 4))

    #  chunk TTFT
    ttft_ms = None
    total_bytes = 0

    #  chunk
    chunk_count = 0
    last_chunk_time = None
    chunk_intervals = []

    try:
        async for chunk in r.aiter_raw():
            if not chunk:
                continue
            if await req.is_disconnected():
                elog("client_disconnected_stream", rid=rid)
                break

            chunk_count += 1
            current_chunk_time = time.perf_counter()

            #  TTFT chunk
            if ttft_ms is None:
                ttft_ms = (current_chunk_time - request_start_time) * 1000
                jlog("ttft", rid=rid, ttft_ms=f"{ttft_ms:.2f}")

            #  chunk
            if last_chunk_time is not None:
                chunk_interval_ms = (current_chunk_time - last_chunk_time) * 1000
                chunk_intervals.append(chunk_interval_ms)
                jlog("chunk_interval", rid=rid, chunk_num=chunk_count,
                     interval_ms=f"{chunk_interval_ms:.2f}", chunk_size=len(chunk))

            last_chunk_time = current_chunk_time
            total_bytes += len(chunk)

            #
            if not first_flush_done and len(chunk) <= FAST_PATH_BYTES:
                yield chunk
                first_flush_done = True
                continue

            buf.extend(chunk)
            now = time.perf_counter()

            #  flush
            if _should_flush_first_packet(buf, first_flush_done, now, last_flush):
                yield bytes(buf)
                buf.clear()
                first_flush_done = True
                last_flush = now
                continue

            #  flush +  +
            dyn_bytes = dyn_base + random.randint(0, max(1, dyn_base // 8))
            if _should_flush(buf, dyn_bytes, last_flush, now):
                yield bytes(buf)
                buf.clear()
                last_flush = now
    finally:
        if buf:
            yield bytes(buf)

        #  Total Time chunk
        total_time_ms = (time.perf_counter() - request_start_time) * 1000
        jlog("total_time", rid=rid, total_time_ms=f"{total_time_ms:.2f}", total_bytes=total_bytes)

        #  chunk
        if chunk_intervals:
            avg_interval_ms = sum(chunk_intervals) / len(chunk_intervals)
            jlog("chunk_interval_avg", rid=rid, chunk_count=chunk_count,
                 avg_interval_ms=f"{avg_interval_ms:.2f}",
                 min_interval_ms=f"{min(chunk_intervals):.2f}",
                 max_interval_ms=f"{max(chunk_intervals):.2f}")

        await r.aclose()


#  TTFT

def _should_flush_first_packet(buf: bytearray, first_flush_done: bool, now: float, last_flush: float) -> bool:
    """
     flush
    -  flush  False
    -  >= FIRST_FLUSH_BYTES True
    -  flush  \n\n True
    -  flush  flush  >= FIRST_FLUSH_MS True
    """
    if first_flush_done:
        return False
    if len(buf) >= FIRST_FLUSH_BYTES:
        return True
    if ENABLE_DELIM_FLUSH and b"\n\n" in buf:
        return True
    if FIRST_FLUSH_MS and (now - last_flush) >= FIRST_FLUSH_MS:
        return True
    return False


def _should_flush(buf: bytearray, dyn_bytes: int, last_flush: float, now: float) -> bool:
    """
     flush
    -  >=  True
    -  flush  \n\n True
    -  flush  >= STREAM_FLUSH_MS True
    """
    return (
        len(buf) >= dyn_bytes or
        (ENABLE_DELIM_FLUSH and b"\n\n" in buf) or
        (now - last_flush) >= STREAM_FLUSH_MS
    )


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    """
      /v1/chat/completions
     gateway.py
    -
    - TTFT
    -
    - TTFT  Total Time
    -
    """
    rid = req.headers.get("x-request-id")
    gate: QueueGate = app.state.gate

    #  TTFT  Total Time
    request_start_time = time.perf_counter()

    # 1:
    t1_start = time.perf_counter()
    body_bytes = await read_json_body(req, rid, MAX_REQUEST_BYTES)
    t1_end = time.perf_counter()
    t1_ms = (t1_end - t1_start) * 1000
    jlog("req_recv", rid=rid, path=str(req.url.path), body_len=len(body_bytes), step1_read_body_ms=f"{t1_ms:.2f}")

    # 2:
    t2_start = time.perf_counter()
    try:
        queue_headers = await _acquire_gate_early(req, gate, rid)
    except HTTPException as e:
        elog("gate_acquire_error", rid=rid, status=e.status_code, detail=str(e.detail))
        return JSONResponse(
            status_code=e.status_code,
            content={"detail": e.detail},
            headers=gate.obs_headers(e.headers)
        )
    t2_end = time.perf_counter()
    t2_ms = (t2_end - t2_start) * 1000
    jlog("gate_acquired", rid=rid, step2_acquire_gate_ms=f"{t2_ms:.2f}")

    # 3:
    t3_start = time.perf_counter()
    r = await _send_stream_request(client, "/v1/chat/completions", body_bytes, req, rid)
    t3_end = time.perf_counter()
    t3_ms = (t3_end - t3_start) * 1000
    jlog("backend_response", rid=rid, status_code=r.status_code, step3_send_request_ms=f"{t3_ms:.2f}")

    # 4:
    t4_start = time.perf_counter()
    passthrough = _build_passthrough_headers(r, gate, queue_headers)
    t4_end = time.perf_counter()
    t4_ms = (t4_end - t4_start) * 1000
    jlog("build_headers", rid=rid, step4_build_headers_ms=f"{t4_ms:.2f}")

    # 5:  TTFT  Total Time
    t5_start = time.perf_counter()
    response = StreamingResponse(
        _stream_gen(req, r, rid, request_start_time),
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "text/event-stream"),
        headers=passthrough,
    )
    t5_end = time.perf_counter()
    t5_ms = (t5_end - t5_start) * 1000
    jlog("create_response", rid=rid, step5_create_response_ms=f"{t5_ms:.2f}")

    #
    proxy_internal_ms = t1_ms + t2_ms + t3_ms + t4_ms + t5_ms
    jlog("proxy_internal_timing", rid=rid, proxy_internal_ms=f"{proxy_internal_ms:.2f}",
         step1_read_body_ms=f"{t1_ms:.2f}",
         step2_acquire_gate_ms=f"{t2_ms:.2f}",
         step3_send_request_ms=f"{t3_ms:.2f}",
         step4_build_headers_ms=f"{t4_ms:.2f}",
         step5_create_response_ms=f"{t5_ms:.2f}")

    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "18000")),
        log_level="info",
    )
