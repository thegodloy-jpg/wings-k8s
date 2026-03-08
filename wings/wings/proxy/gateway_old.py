# -*- coding: utf-8 -*-
"""
（Method 2：全局闸门 + 本地兜底，保留原有逻辑，移除“思考/think”相关功能）
==============================================================================
改动要点：
1) 后端仅通过基础 URL（例如 http://10.0.0.8:17000）指定，**不包含任何路径**。
   本文件在每个路由中通过 build_backend_url(path) 生成**绝对 URL**
   （scheme://host:port/path），不依赖 httpx 的 base_url 自动拼接。
2) 保留：固定间隔重试、早释放闸门（先 acquire，随后尽早 release）、流/非流透传、
   结构化日志字段（jlog/elog），并按你的原始实现细节进行字节/时间触发 flush。
3) 路由映射（→ 后端）：
   POST : /v1/chat/completions → /v1/chat/completions（支持流式/非流式）
          /v1/completions      → /v1/completions（支持流式/非流式）
          /v1/rerank           → /v1/rerank
          /v1/embeddings       → /v1/embeddings
          /tokenize            → /tokenize（vLLM）
   GET  : /health           → 代理自身健康检查（细粒度状态机、严格 200 后端探测）
          /v1/version          → /v1/version
          /metrics             → /metrics（vLLM, Prometheus 文本）
4) “思考/think”相关代码与配置引用已**全部移除**。
5) 健康探测与状态机逻辑已迁移至 health.py；本文件仅保留 /health 路由。
"""

from __future__ import annotations
import asyncio
import inspect
import random
import time
from typing import Any, AsyncIterator, Dict

import os
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import settings as C
from .http_client import create_async_client
from .queueing import QueueGate
from .tags import (
    want_stream,
    make_upstream_headers,
    read_json_body,
    jlog, elog, ms,
    build_backend_url,
)
from .speaker_logging import configure_worker_logging

# 新：仅导入 health.py 的 API；/health 细节全部在 health.py 中
from .health import (
    setup_health_monitor,
    teardown_health_monitor,
    map_http_code_from_state,
    build_health_body,
    build_health_headers,
)

configure_worker_logging()
# 允许的最大请求体（字节）。超过则返回 413。
MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", str(2 * 1024 * 1024)))

# 初始化 FastAPI
app = FastAPI()

# 仅记录后端基础 URL（供调试可观测）；不含路径。
app.state.backend = C.BACKEND_URL

# 兼容不同 httpx 版本：send() 是否支持 timeout 形参
_SEND_HAS_TIMEOUT = "timeout" in inspect.signature(httpx.AsyncClient.send).parameters


# ───────────────────────── 基础发送与“固定间隔重试” ─────────────────────────


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

# 允许重试的异常类型与 5xx（仅在“尚未读取字节”的流式首部阶段）
_RETRIABLE_EXC = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_RETRIABLE_5XX = (502, 503, 504)

# ─────── 重试辅助函数（不改原有变量/行为） ───────


def _should_retry_status(stream: bool, status_code: int, attempt: int, total: int) -> bool:
    return stream and status_code in _RETRIABLE_5XX and attempt < total


async def _close_resp_quiet(resp: httpx.Response) -> None:
    try:
        await resp.aclose()
    except Exception as e:
        C.logger.error(f"Failed to close response: {e}")


def _mark_retry_count(resp: httpx.Response, attempt: int) -> None:
    try:
        resp.extensions["app_retry_count"] = attempt - 1
    except Exception as e:
        C.logger.error(f"Failed to set retry count in response extensions: {e}")


async def _log_and_wait_status_retry(rid: str | None, attempt: int, status: int, interval: float, t0: float) -> None:
    elog(
        "retry_status",
        rid=rid, attempt=attempt, status=status,
        next_wait_ms=int(interval * 1000), elapsed=ms(time.perf_counter() - t0),
    )
    await asyncio.sleep(interval)


def _is_retriable_exception(e: Exception) -> bool:
    return isinstance(e, _RETRIABLE_EXC)


async def _log_and_maybe_wait_exception(e: Exception, **ctx) -> bool:
    # 兼容原字段名：rid/attempt/total/interval/t0
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
        elapsed=ms(time.perf_counter() - t0),
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
    应用层“固定间隔重试”：
    - 默认 RETRY_TRIES=3（总尝试：首发 + 重试若干次），RETRY_INTERVAL_MS=100ms。
    - 可重试情形：
        1) 连接类异常（ConnectError / ConnectTimeout / PoolTimeout）
        2) 流式首部阶段的 502/503/504（尚未读取任何字节；读取后不再重试）
    - 每次失败打印结构化日志；成功响应在 resp.extensions["app_retry_count"] 标注重试次数。
    """
    total = max(1, int(C.RETRY_TRIES))
    interval = max(0, int(C.RETRY_INTERVAL_MS)) / 1000.0
    last_exc: Exception | None = None
    last_status: int | None = None

    for attempt in range(1, total + 1):
        t0 = time.perf_counter()
        try:
            resp = await _raw_send(
                client, method, url,
                content=content, headers=headers, stream=stream, timeout=timeout
            )

            # 仅“流式 & 首部阶段”对 5xx 重试（还没开始消费响应体）
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


# ───────────────────────── 生命周期：创建 http 客户端与并发闸门 ─────────────────────────


@app.on_event("startup")
async def _startup():
    """
    进程启动：
    - 创建 httpx.AsyncClient
    - 初始化本地并发闸门
    - 启动健康监控（全部在 health.py 中）
    """
    configure_worker_logging(force=True)
    C.log_boot_plan()
    app.state.client = await create_async_client()
    app.state.gate = QueueGate()

    # 健康监控初始化（状态与后台循环）
    setup_health_monitor(app)
    C.logger.info("Reason-Proxy is starting on %s:%s (health monitor loop enabled)", C.HOST, C.PORT)


@app.on_event("shutdown")
async def _shutdown():
    """优雅关闭 http 客户端与健康监控。"""
    try:
        await teardown_health_monitor(app)
    except asyncio.CancelledError:
        # 双保险：绝不让 CancelledError 冒泡到 Starlette
        pass
    await app.state.client.aclose()


# ───────────────────────── 通用透传：非流式 ─────────────────────────


def _copy_entity_headers(resp: httpx.Response) -> Dict[str, str]:
    """仅保留 X-* 与常见实体头。"""
    return {
        k: v for k, v in resp.headers.items()
        if k.lower().startswith("x-") or k.lower() in ("content-type", "content-encoding", "etag", "last-modified")
    }


def _merge_obs_and_retry_headers(
        gate: QueueGate,
        queue_headers: Dict[str, str],
        resp: httpx.Response) -> Dict[str, str]:
    """合并队列观测头，并注入 X-Retry-Count（如有）。"""
    merged = gate.obs_headers(queue_headers)
    ext = getattr(resp, "extensions", None)
    if isinstance(ext, dict):
        retry_cnt = ext.get("app_retry_count")
        if retry_cnt is not None:
            merged["X-Retry-Count"] = str(retry_cnt)
    return merged


def _content_length(resp: httpx.Response) -> int | None:
    """解析 Content-Length，无法解析返回 None。"""
    try:
        v = resp.headers.get("content-length")
        return int(v) if v is not None and v.isdigit() else None
    except Exception:
        return None


async def _send_nonstream_request(
    client: httpx.AsyncClient,
    upstream_path: str,
    body_bytes: bytes,
    req: Request,
    rid: str | None,
) -> httpx.Response:
    """非流式透传的发送动作（保持 stream=True，便于边读边发）。"""
    url = build_backend_url(upstream_path)
    return await _send_with_fixed_retries(
        client, "POST", url,
        content=body_bytes,
        headers=make_upstream_headers(req, want_gzip=False),
        stream=True,  # 与原实现一致
        rid=rid,
    )


async def _pipe_nonstream(req: Request, r: httpx.Response, rid: str | None) -> AsyncIterator[bytes]:
    """大响应的管道转发（边读边发），客户端断开则中止。"""
    try:
        async for chunk in r.aiter_bytes():
            if not chunk:
                continue
            if await req.is_disconnected():
                elog("client_disconnected_nonstream", rid=rid)
                break
            yield chunk
    finally:
        await r.aclose()


async def _acquire_gate_early_nonstream(req: Request, gate: QueueGate, rid: str | None) -> Dict[str, str]:
    """获取处理权并立即释放；失败抛 HTTPException。"""
    queue_headers = await gate.acquire(dict(req.headers))
    await gate.release()
    jlog("gate_acquired_released_early", rid=rid, wait_hdr=queue_headers.get("X-Queued-Wait"))
    return queue_headers


async def _forward_nonstream(req: Request, upstream_path: str):
    """
    非流式透传（内部仍以 stream=True 从后端读取，以便大响应时“边读边发”）：
    1) 读取并校验 JSON 请求体
    2) 闸门“尽早释放”
    3) 固定间隔重试发送
    4) 小响应聚合返回；大响应管道转发
    """
    client: httpx.AsyncClient = app.state.client
    gate: QueueGate = app.state.gate
    rid = req.headers.get("x-request-id")

    # 读取与校验请求体
    body_bytes = await read_json_body(req, rid, MAX_REQUEST_BYTES)
    jlog("req_recv", rid=rid, path=str(req.url.path), body_len=len(body_bytes))

    # 获取处理权并“尽早释放”
    try:
        queue_headers = await _acquire_gate_early_nonstream(req, gate, rid)
    except HTTPException as e:
        elog("gate_acquire_error", rid=rid, status=e.status_code, detail=str(e.detail))
        return JSONResponse(status_code=e.status_code,
                            content={"detail": e.detail},
                            headers=gate.obs_headers(e.headers))

    # 发送请求
    r = await _send_nonstream_request(client, upstream_path, body_bytes, req, rid)

    # 组装响应头（实体 + 观测/重试）
    merged = _merge_obs_and_retry_headers(gate, queue_headers, r)
    entity_headers = _copy_entity_headers(r)

    # 小响应：聚合返回
    content_len = _content_length(r)
    if content_len and content_len <= C.NONSTREAM_THRESHOLD:
        data = await r.aread()
        await r.aclose()
        return Response(
            data,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
            headers={**entity_headers, **merged},
        )

    # 大响应：管道转发（边读边发）
    return StreamingResponse(
        _pipe_nonstream(req, r, rid),
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "application/json"),
        headers={**entity_headers, **merged},
    )


# ───────────────────────── 通用透传：流式 ─────────────────────────


def _should_flush_first_packet(buf: bytearray, first_flush_done: bool, now: float, last_flush: float) -> bool:
    if first_flush_done:
        return False
    if len(buf) >= C.FIRST_FLUSH_BYTES:
        return True
    if C.ENABLE_DELIM_FLUSH and b"\n\n" in buf:
        return True
    if C.FIRST_FLUSH_MS and (now - last_flush) >= C.FIRST_FLUSH_MS:
        return True
    return False


def _should_flush(buf: bytearray, dyn_bytes: int, last_flush: float, now: float) -> bool:
    return (
        len(buf) >= dyn_bytes or
        (C.ENABLE_DELIM_FLUSH and b"\n\n" in buf) or
        (now - last_flush) >= C.STREAM_FLUSH_MS
    )


async def _stream_gen(req: Request, r: httpx.Response, rid: str) -> AsyncIterator[bytes]:
    """
    读取上游响应 r 并按规则向下游产生字节；结束时自动关闭 r。
    """
    buf = bytearray()
    last_flush = time.perf_counter()
    first_flush_done = False
    dyn_base = max(C.STREAM_FLUSH_BYTES, int(C.MAX_CONN / 4))

    try:
        async for chunk in r.aiter_raw():
            if not chunk:
                continue
            if await req.is_disconnected():
                elog("client_disconnected_stream", rid=rid)
                break

            # 首包：极小块直出，加快首字节到达
            if not first_flush_done and len(chunk) <= C.FAST_PATH_BYTES:
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
        await r.aclose()


def _build_passthrough_headers(r: httpx.Response, gate: QueueGate, queue_headers: Dict[str, str]) -> Dict[str, str]:
    """
    仅透传 X-*，注入 retry 计数与队列观测头；并设置 SSE 相关默认头。
    """
    headers = {k: v for k, v in r.headers.items() if k.lower().startswith("x-")}

    ext = getattr(r, "extensions", None)
    if isinstance(ext, dict):
        retry_cnt = ext.get("app_retry_count")
        if retry_cnt is not None:
            headers["X-Retry-Count"] = str(retry_cnt)

    headers.setdefault("X-Accel-Buffering", "no")
    headers.setdefault("Cache-Control", "no-transform")
    headers.update(gate.obs_headers(queue_headers))
    return headers


async def _send_stream_request(
    client: httpx.AsyncClient,
    upstream_path: str,
    body_bytes: bytes,
    req: Request,
    rid: str,
) -> httpx.Response:
    url = build_backend_url(upstream_path)
    return await _send_with_fixed_retries(
        client, "POST", url,
        content=body_bytes,
        headers=make_upstream_headers(req, want_gzip=False),
        stream=True,
        timeout=httpx.Timeout(connect=10, read=None, write=None, pool=None),
        rid=rid,
    )


async def _acquire_gate_early(req: Request, gate: QueueGate, rid: str) -> Dict[str, str]:
    """
    获取处理权并在成功后立即释放；返回队列观测头部。
    失败时抛出 HTTPException 交由调用方处理。
    """
    queue_headers = await gate.acquire(dict(req.headers))
    await gate.release()
    jlog("gate_acquired_released_early", rid=rid, wait_hdr=queue_headers.get("X-Queued-Wait"))
    return queue_headers


async def _forward_stream(req: Request, upstream_path: str):
    """
    流式透传（SSE/Chunked）：
    - 闸门“早释放”
    - 首包/后续 flush 规则
    - 首部阶段 5xx 固定间隔重试，开始消费体后不再重试
    """
    client: httpx.AsyncClient = app.state.client
    gate: QueueGate = app.state.gate
    rid = req.headers.get("x-request-id")

    body_bytes = await read_json_body(req, rid, MAX_REQUEST_BYTES)
    jlog("req_recv", rid=rid, path=str(req.url.path), body_len=len(body_bytes))

    released_early = False
    try:
        queue_headers = await _acquire_gate_early(req, gate, rid)
    except HTTPException as e:
        elog("gate_acquire_error", rid=rid, status=e.status_code, detail=str(e.detail))
        return JSONResponse(
            status_code=e.status_code,
            content={"detail": e.detail},
            headers=gate.obs_headers(e.headers)
        )

    released_early = True

    r = await _send_stream_request(client, upstream_path, body_bytes, req, rid)

    passthrough = _build_passthrough_headers(r, gate, queue_headers)
    try:
        return StreamingResponse(
            _stream_gen(req, r, rid),
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "text/event-stream"),
            headers=passthrough,
        )
    finally:
        # 理论上不会执行（已早释放），此处保留防御性代码
        if not released_early:
            try:
                await gate.release()
                jlog("gate_release_on_finally", rid=rid)
            except Exception as ex:
                elog("gate_release_error", rid=rid, detail=str(ex))


# ───────────────────────── 业务路由：与后端路径一一映射 ─────────────────────────


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    """
    聊天补全（可流式）→ 后端 /v1/chat/completions
    通过请求体中的 stream 标记选择流式或非流式转发。
    """
    try:
        payload: Dict[str, Any] = await req.json()
    except Exception:
        payload = {}
    if want_stream(payload.get("stream", False)):
        return await _forward_stream(req, "/v1/chat/completions")
    return await _forward_nonstream(req, "/v1/chat/completions")


@app.post("/v1/completions")
async def completions(req: Request):
    """
    文本补全（可流式）→ 后端 /v1/completions
    """
    try:
        payload: Dict[str, Any] = await req.json()
    except Exception:
        payload = {}
    if want_stream(payload.get("stream", False)):
        return await _forward_stream(req, "/v1/completions")
    return await _forward_nonstream(req, "/v1/completions")


@app.post("/v1/responses")
async def responses(req: Request):
    """
    Responses 统一生成接口（可流式）→ 后端 /v1/responses
    - 行为与 /v1/chat/completions、/v1/completions 一致：
      * 根据请求体的 "stream" 字段决定是否走流式透传
      * 仅做透传，不做语义改写
    - 其余逻辑（闸门早释放、固定间隔重试、日志、首包/定时 flush 等）复用已有实现
    """
    try:
        payload: Dict[str, Any] = await req.json()
    except Exception:
        payload = {}
    if want_stream(payload.get("stream", False)):
        return await _forward_stream(req, "/v1/responses")
    return await _forward_nonstream(req, "/v1/responses")


@app.post("/v1/rerank")
async def rerank(req: Request):
    # 非流式透传到后端 /v1/rerank
    return await _forward_nonstream(req, "/v1/rerank")


@app.post("/v1/embeddings")
async def embeddings(req: Request):
    # 非流式透传到后端 /v1/embeddings
    return await _forward_nonstream(req, "/v1/embeddings")


@app.post("/tokenize")
async def tokenize(req: Request):
    # vLLM 的 /tokenize：非流式透传
    return await _forward_nonstream(req, "/tokenize")


def _extract_metrics_headers(r: httpx.Response) -> dict:
    """提取响应头：仅保留 x-*，补充 Content-Type"""
    headers = {k: v for k, v in r.headers.items() if k.lower().startswith("x-")}
    headers["Content-Type"] = r.headers.get(
        "content-type",
        "text/plain; version=0.0.4; charset=utf-8",
    )
    return headers


async def _pipe_metrics(req: Request, r: httpx.Response):
    """边读边转发后端响应数据"""
    try:
        async for chunk in r.aiter_bytes():
            if not chunk:
                continue
            if await req.is_disconnected():
                elog("client_disconnected_metrics", rid=req.headers.get("x-request-id"))
                break
            yield chunk
    finally:
        await r.aclose()


@app.get("/metrics")
async def metrics(req: Request):
    """
    vLLM 指标：GET 透传到后端 /metrics，边读边发（流式）
    """
    client: httpx.AsyncClient = app.state.client
    url = build_backend_url("/metrics")

    r = await _send_with_fixed_retries(
        client, "GET", url,
        headers=make_upstream_headers(req, want_gzip=False),
        stream=True,
        timeout=httpx.Timeout(connect=5.0, read=None, write=None, pool=None),
        rid=req.headers.get("x-request-id"),
    )

    headers = _extract_metrics_headers(r)
    return StreamingResponse(
        _pipe_metrics(req, r),
        status_code=r.status_code,
        headers=headers,
    )


@app.post("/v1/videos/text2video")
async def hv_text2video(req: Request):
    """
    HunyuanVideo 文生视频提交：
    直接非流式透传到后端 /v1/videos/text2video
    （请求体大小受 MAX_REQUEST_BYTES 限制）
    """
    return await _forward_nonstream(req, "/v1/videos/text2video")


@app.get("/v1/videos/text2video/{task_id}")
async def hv_text2video_status(task_id: str, req: Request):
    """
    HunyuanVideo 任务状态查询：
    透传到后端 /v1/videos/text2video/{task_id} 并返回其响应。
    """
    client: httpx.AsyncClient = app.state.client
    rid = req.headers.get("x-request-id")
    url = build_backend_url(f"/v1/videos/text2video/{task_id}")

    # 小 JSON，聚合返回即可；保留实体相关头部
    r = await _send_with_fixed_retries(
        client, "GET", url,
        headers=make_upstream_headers(req, want_gzip=False),
        stream=True,  # 读取时使用流，随后聚合
        timeout=httpx.Timeout(connect=5.0, read=15.0, write=None, pool=None),
        rid=rid,
    )

    entity_headers = _copy_entity_headers(r)
    data = await r.aread()
    await r.aclose()
    return Response(
        data,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "application/json"),
        headers=entity_headers,
    )

# ───────────────────────── /health（仅路由；实现细节在 health.py） ─────────────────────────


@app.get("/v1/version")
async def version_proxy(req: Request):
    """
    版本信息：从环境变量 WINGS_VERSION / WINGS_BUILD_DATE 读取
    默认值: WINGS_VERSION="25.0.0.1", WINGS_BUILD_DATE="2025-08-30"
    返回 JSON 结构：
    {
      "WINGS_VERSION": "2.0.0",
      "WINGS_BUILD_DATE": "2025-08-18"
    }
    """
    rid = req.headers.get("x-request-id")

    try:
        version = os.getenv("WINGS_VERSION", "25.0.0.1")
        build_date = os.getenv("WINGS_BUILD_DATE", "2025-08-30")

        return JSONResponse(
            status_code=200,
            content={
                "WINGS_VERSION": version,
                "WINGS_BUILD_DATE": build_date
            }
        )
    except Exception as e:
        elog("version_env_error", rid=rid, detail=str(e))
        return JSONResponse(
            status_code=200,
            content={
                "WINGS_VERSION": "unknown",
                "WINGS_BUILD_DATE": "unknown",
                "error": "failed to read environment variables"
            },
            headers={"X-Version-Error": "env_read_error"},
        )
