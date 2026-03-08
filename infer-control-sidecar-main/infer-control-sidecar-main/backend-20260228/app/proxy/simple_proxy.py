# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: proxy/simple_proxy.py
# Purpose: Simplified proxy implementation retained for fallback and testing scenarios.
# Status: Compatibility path; not primary in launcher MVP.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Keep compatibility behavior intact.
# - Avoid introducing new dependencies for this fallback path.
# -----------------------------------------------------------------------------
# -*- coding: utf-8 -*-
"""
轻量级代理服务：
- 只提供 /v1/chat/completions 的流式转发
- 使用优化的 HTTP 客户端（连接池、keepalive 等）
- 包含 TTFT（Time to First Token）优化
- 支持 HTTP 预热功能
- 支持禁止中间缓冲策略
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

# 导入配置和工具函数
from app.proxy import settings as C
from app.proxy.tags import build_backend_url, jlog, elog
from app.proxy.queueing import QueueGate

# ───────── TTFT 优化配置参数 ─────────
# 首包快速路径：小于等于此字节数的首包直接 yield，不缓冲
FAST_PATH_BYTES = C.FAST_PATH_BYTES

# 首包 Flush 规则
FIRST_FLUSH_BYTES = C.FIRST_FLUSH_BYTES
FIRST_FLUSH_MS = C.FIRST_FLUSH_MS

# 后续 Flush 规则
STREAM_FLUSH_BYTES = C.STREAM_FLUSH_BYTES
STREAM_FLUSH_MS = C.STREAM_FLUSH_MS

# 是否启用分隔符触发 flush（\n\n）
ENABLE_DELIM_FLUSH = C.ENABLE_DELIM_FLUSH

# 连接池配置
MAX_CONN = C.MAX_CONN
MAX_KEEPALIVE = C.MAX_KEEPALIVE
KEEPALIVE_EXPIRY = C.KEEPALIVE_EXPIRY

# 其他连接优化参数
MAX_REDIRECTS = int(os.getenv("HTTPX_MAX_REDIRECTS", "20"))
VERIFY_SSL = os.getenv("HTTPX_VERIFY_SSL", "true").lower() != "false"
TRUST_ENV = os.getenv("HTTPX_TRUST_ENV", "false").lower() != "false"

# ───────── 预热配置参数 ─────────
WARMUP_ENABLED = os.getenv("WARMUP_ENABLED", "false").lower() != "false"
WARMUP_CONN = C.WARMUP_CONN
WARMUP_PROMPT = C.WARMUP_PROMPT
WARMUP_ROUNDS = C.WARMUP_ROUNDS
WARMUP_TIMEOUT = C.WARMUP_TIMEOUT
WARMUP_MODEL = os.getenv("WARMUP_MODEL", "default-model")

# ───────── 禁止中间缓冲策略 ─────────
DISABLE_MIDDLE_BUFFER = os.getenv("DISABLE_MIDDLE_BUFFER", "true").lower() != "false"

# ───────── 重试机制配置参数 ─────────
RETRY_TRIES = C.RETRY_TRIES
RETRY_INTERVAL_MS = C.RETRY_INTERVAL_MS

# 兼容不同 httpx 版本：send() 是否支持 timeout 形参
_SEND_HAS_TIMEOUT = "timeout" in inspect.signature(httpx.AsyncClient.send).parameters

# 允许重试的异常类型与 5xx（仅在"尚未读取字节"的流式首部阶段）
_RETRIABLE_EXC = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_RETRIABLE_5XX = (502, 503, 504)

# 创建 FastAPI 应用
app = FastAPI()

# 创建优化的 HTTP 客户端（包含连接池、keepalive 等优化）
client = httpx.AsyncClient(
    limits=httpx.Limits(
        max_connections=MAX_CONN,
        max_keepalive_connections=MAX_KEEPALIVE,
        keepalive_expiry=KEEPALIVE_EXPIRY,
    ),
    timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None),
    max_redirects=MAX_REDIRECTS,
    verify=VERIFY_SSL,
    trust_env=TRUST_ENV,
)


@app.on_event("startup")
async def startup():
    """启动时初始化"""
    jlog("proxy_startup", host="0.0.0.0", port=18000, backend_url=C.BACKEND_URL)

    # 初始化队列闸门
    app.state.gate = QueueGate()

    # 执行预热
    if WARMUP_ENABLED:
        await warmup_connections()


async def warmup_connections():
    """
    预热连接池：
    - 建立多个到后端的连接，填充连接池
    - 发送预热请求，确保连接可用
    """
    jlog("warmup_start", conn_count=WARMUP_CONN, rounds=WARMUP_ROUNDS, model=WARMUP_MODEL)

    backend_url = build_backend_url("/v1/chat/completions")

    for round_num in range(1, WARMUP_ROUNDS + 1):
        jlog("warmup_round", round=round_num, total=WARMUP_ROUNDS)

        # 并发建立多个连接
        tasks = []
        for i in range(WARMUP_CONN):
            task = asyncio.create_task(
                send_warmup_request(backend_url, i, round_num)
            )
            tasks.append(task)

        # 等待所有预热请求完成
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=WARMUP_TIMEOUT
            )
        except asyncio.TimeoutError:
            elog("warmup_timeout", round=round_num, timeout=WARMUP_TIMEOUT)

    jlog("warmup_complete", total_conn=WARMUP_CONN * WARMUP_ROUNDS)


async def send_warmup_request(backend_url: str, conn_id: int, round_num: int):
    """
    发送单个预热请求
    """
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
            "max_tokens": 10  # 预热请求只需要少量 token
        }

        headers = {
            "content-type": "application/json",
            "accept-encoding": "identity",
            "connection": "keep-alive",
        }

        # 发送预热请求
        r = await client.post(
            backend_url,
            json=warmup_data,
            headers=headers,
            timeout=httpx.Timeout(connect=5.0, read=5.0, write=None, pool=None),
        )

        # 读取少量数据确保连接建立
        if r.status_code == 200:
            async for chunk in r.aiter_bytes(chunk_size=1024):
                break  # 只读取第一个 chunk 然后退出
            await r.aclose()
            jlog("warmup_success", conn_id=conn_id, round=round_num)
        else:
            elog("warmup_failed", conn_id=conn_id, round=round_num, status_code=r.status_code)
            await r.aclose()

    except Exception as e:
        elog("warmup_error", conn_id=conn_id, round=round_num, error_type=e.__class__.__name__, detail=str(e))


@app.on_event("shutdown")
async def shutdown():
    """关闭时清理资源"""
    await client.aclose()
    jlog("proxy_shutdown")


# ───────── 重试机制辅助函数 ─────────

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
    低层 httpx 发送封装（不含应用层重试）：
    - 构造 request
    - 发送（支持 stream）
    - 对旧版 httpx 进行 timeout 参数兼容处理
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
    应用层"固定间隔重试"：
    - 默认 RETRY_TRIES=3（总尝试：首发 + 重试若干次），RETRY_INTERVAL_MS=100ms。
    - 可重试情形：
        1) 连接类异常（ConnectError / ConnectTimeout / PoolTimeout）
        2) 流式首部阶段的 502/503/504（尚未读取任何字节；读取后不再重试）
    - 每次失败打印结构化日志；成功响应在 resp.extensions["app_retry_count"] 标注重试次数。
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

            # 仅"流式 & 首部阶段"对 5xx 重试（还没开始消费响应体）
            if _should_retry_status(stream, resp.status_code, attempt, total):
                last_status = resp.status_code
                await _close_resp_quiet(resp)
                await _log_and_wait_status_retry(rid, attempt, last_status, interval, t0)
                continue

            # 成功：在 extensions 标出重试次数（首发成功 -> 0）
            _mark_retry_count(resp, attempt)
            return resp

        except Exception as e:
            last_exc = e
            # 连接类异常：可按固定间隔重试
            if await _log_and_maybe_wait_exception(e, rid=rid, attempt=attempt, total=total, interval=interval, t0=t0):
                continue

            # 不可重试或已达上限：对 httpx.RequestError 统一转为 502
            if isinstance(e, httpx.RequestError):
                raise HTTPException(502, f"backend connect error: {e}") from e
            raise

    # 兜底：理论上不应走到这里
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


# ───────── TTFT 优化辅助函数 ─────────


# ───────── 流式处理辅助函数 ─────────


async def _acquire_gate_early(req: Request, gate: QueueGate, rid: str) -> Dict[str, str]:
    """
    获取处理权并在成功后立即释放；返回队列观测头部。
    失败时抛出 HTTPException 交由调用方处理。
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
    """发送流式请求到后端（带重试机制）"""
    url = build_backend_url(upstream_path)
    return await _send_with_fixed_retries(
        client, "POST", url,
        content=body_bytes,
        headers={k: v for k, v in req.headers.items() if k.lower() != "host"},
        stream=True,
        timeout=httpx.Timeout(connect=10, read=None, write=None, pool=None),
        rid=rid,
    )


def _build_passthrough_headers(r: httpx.Response, gate: QueueGate, queue_headers: Dict[str, str]) -> Dict[str, str]:
    """
    构造透传响应头：
    - 仅透传 X-* 头
    - 注入重试计数
    - 添加队列观测头
    - 添加禁止中间缓冲策略的响应头
    """
    headers = {k: v for k, v in r.headers.items() if k.lower().startswith("x-")}

    ext = getattr(r, "extensions", None)
    if isinstance(ext, dict):
        retry_cnt = ext.get("app_retry_count")
        if retry_cnt is not None:
            headers["X-Retry-Count"] = str(retry_cnt)

    # 添加队列观测头
    headers.update(gate.obs_headers(queue_headers))

    # 添加禁止中间缓冲策略的响应头
    if DISABLE_MIDDLE_BUFFER:
        headers["X-Accel-Buffering"] = "no"  # 禁止 Nginx 缓冲
        headers["Cache-Control"] = "no-cache, no-store, no-transform, must-revalidate"  # 禁止缓存

    return headers


async def _stream_gen(req: Request, r: httpx.Response, rid: str, request_start_time: float):
    """
    TTFT 优化的流式生成器：
    - 首包快速路径：极小块直接 yield，加快首字节到达
    - 首包 flush：字节阈值 + 分隔符 + 时间阈值
    - 后续 flush：自适应字节阈值 + 分隔符 + 时间阈值
    - 记录 TTFT、Total Time 和每个 chunk 的时间间隔
    """
    buf = bytearray()
    last_flush = time.perf_counter()
    first_flush_done = False
    dyn_base = max(STREAM_FLUSH_BYTES, int(MAX_CONN / 4))

    # 记录第一个 chunk 到达的时间（TTFT）
    ttft_ms = None
    total_bytes = 0

    # 记录每个 chunk 的时间间隔
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

            # 记录 TTFT（第一个 chunk 到达的时间）
            if ttft_ms is None:
                ttft_ms = (current_chunk_time - request_start_time) * 1000
                jlog("ttft", rid=rid, ttft_ms=f"{ttft_ms:.2f}")

            # 记录每个 chunk 的时间间隔
            if last_chunk_time is not None:
                chunk_interval_ms = (current_chunk_time - last_chunk_time) * 1000
                chunk_intervals.append(chunk_interval_ms)
                jlog("chunk_interval", rid=rid, chunk_num=chunk_count,
                     interval_ms=f"{chunk_interval_ms:.2f}", chunk_size=len(chunk))

            last_chunk_time = current_chunk_time
            total_bytes += len(chunk)

            # 首包：极小块直出，加快首字节到达
            if not first_flush_done and len(chunk) <= FAST_PATH_BYTES:
                yield chunk
                first_flush_done = True
                continue

            buf.extend(chunk)
            now = time.perf_counter()

            # 首包 flush
            if _should_flush_first_packet(buf, first_flush_done, now, last_flush):
                yield bytes(buf)
                buf.clear()
                first_flush_done = True
                last_flush = now
                continue

            # 后续 flush：自适应字节阈值 + 分隔符 + 时间阈值
            dyn_bytes = dyn_base + random.randint(0, max(1, dyn_base // 8))
            if _should_flush(buf, dyn_bytes, last_flush, now):
                yield bytes(buf)
                buf.clear()
                last_flush = now
    finally:
        if buf:
            yield bytes(buf)

        # 记录 Total Time（最后一个 chunk 发送完成的时间）
        total_time_ms = (time.perf_counter() - request_start_time) * 1000
        jlog("total_time", rid=rid, total_time_ms=f"{total_time_ms:.2f}", total_bytes=total_bytes)

        # 计算并记录 chunk 间隔的平均值
        if chunk_intervals:
            avg_interval_ms = sum(chunk_intervals) / len(chunk_intervals)
            jlog("chunk_interval_avg", rid=rid, chunk_count=chunk_count,
                 avg_interval_ms=f"{avg_interval_ms:.2f}",
                 min_interval_ms=f"{min(chunk_intervals):.2f}",
                 max_interval_ms=f"{max(chunk_intervals):.2f}")

        await r.aclose()


# ───────── TTFT 优化辅助函数 ─────────

def _should_flush_first_packet(buf: bytearray, first_flush_done: bool, now: float, last_flush: float) -> bool:
    """
    判断是否应该 flush 首包：
    - 如果已经 flush 过，返回 False
    - 如果缓冲区字节数 >= FIRST_FLUSH_BYTES，返回 True
    - 如果启用分隔符 flush 且缓冲区包含 \n\n，返回 True
    - 如果启用时间 flush 且距离上次 flush 时间 >= FIRST_FLUSH_MS，返回 True
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
    判断是否应该 flush 后续数据：
    - 如果缓冲区字节数 >= 动态字节阈值，返回 True
    - 如果启用分隔符 flush 且缓冲区包含 \n\n，返回 True
    - 如果距离上次 flush 时间 >= STREAM_FLUSH_MS，返回 True
    """
    return (
        len(buf) >= dyn_bytes or
        (ENABLE_DELIM_FLUSH and b"\n\n" in buf) or
        (now - last_flush) >= STREAM_FLUSH_MS
    )


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    """
    聊天补全（流式）→ 后端 /v1/chat/completions
    采用 gateway.py 同样的处理方式：
    - 固定间隔重试
    - TTFT 优化的流式生成器
    - 禁止中间缓冲策略
    - 记录完整的端到端时间（TTFT 和 Total Time）
    - 队列闸门机制（早释放策略）
    """
    rid = req.headers.get("x-request-id")
    gate: QueueGate = app.state.gate

    # 记录请求开始时间（用于计算 TTFT 和 Total Time）
    request_start_time = time.perf_counter()

    # 步骤1: 读取请求体
    t1_start = time.perf_counter()
    body_bytes = await req.body()
    t1_end = time.perf_counter()
    t1_ms = (t1_end - t1_start) * 1000
    jlog("req_recv", rid=rid, path=str(req.url.path), body_len=len(body_bytes), step1_read_body_ms=f"{t1_ms:.2f}")

    # 步骤2: 获取并发闸门
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

    # 步骤3: 发送流式请求到后端（带重试机制）
    t3_start = time.perf_counter()
    r = await _send_stream_request(client, "/v1/chat/completions", body_bytes, req, rid)
    t3_end = time.perf_counter()
    t3_ms = (t3_end - t3_start) * 1000
    jlog("backend_response", rid=rid, status_code=r.status_code, step3_send_request_ms=f"{t3_ms:.2f}")

    # 步骤4: 构造透传响应头
    t4_start = time.perf_counter()
    passthrough = _build_passthrough_headers(r, gate, queue_headers)
    t4_end = time.perf_counter()
    t4_ms = (t4_end - t4_start) * 1000
    jlog("build_headers", rid=rid, step4_build_headers_ms=f"{t4_ms:.2f}")

    # 步骤5: 创建流式响应对象（传入请求开始时间用于计算 TTFT 和 Total Time）
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

    # 记录代理内部处理总耗时（不包括流式传输时间）
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
        port=18000,
        log_level="info",
    )
