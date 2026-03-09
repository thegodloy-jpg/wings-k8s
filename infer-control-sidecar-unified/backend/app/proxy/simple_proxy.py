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

# 导入代理相关依赖模块
from app.proxy import settings as C
from app.proxy.tags import build_backend_url, jlog, elog, read_json_body
from app.proxy.queueing import QueueGate

# TTFT 快速路径阈值：小于此字节数的 chunk 直接 yield，减少缓冲延迟
FAST_PATH_BYTES = C.FAST_PATH_BYTES

# 首包 Flush 阈值：首次刷新的字节数和时间上限
FIRST_FLUSH_BYTES = C.FIRST_FLUSH_BYTES
FIRST_FLUSH_MS = C.FIRST_FLUSH_MS

# 流式 Flush 阈值：后续数据刷新的字节数和时间间隔
STREAM_FLUSH_BYTES = C.STREAM_FLUSH_BYTES
STREAM_FLUSH_MS = C.STREAM_FLUSH_MS

# 分隔符触发 flush：遇到 \n\n 时立即刷新（SSE 事件边界）
ENABLE_DELIM_FLUSH = C.ENABLE_DELIM_FLUSH

# 连接池配置
MAX_CONN = C.MAX_CONN
MAX_KEEPALIVE = C.MAX_KEEPALIVE
KEEPALIVE_EXPIRY = C.KEEPALIVE_EXPIRY

# HTTP 客户端配置
MAX_REDIRECTS = int(os.getenv("HTTPX_MAX_REDIRECTS", "20"))
VERIFY_SSL = os.getenv("HTTPX_VERIFY_SSL", "true").lower() != "false"
TRUST_ENV = os.getenv("HTTPX_TRUST_ENV", "false").lower() != "false"

# 预热配置：启动时预建连接，减少首次请求延迟
WARMUP_ENABLED = os.getenv("WARMUP_ENABLED", "false").lower() != "false"
WARMUP_CONN = C.WARMUP_CONN
WARMUP_PROMPT = C.WARMUP_PROMPT
WARMUP_ROUNDS = C.WARMUP_ROUNDS
WARMUP_TIMEOUT = C.WARMUP_TIMEOUT
WARMUP_MODEL = os.getenv("WARMUP_MODEL", "default-model")

# 禁用中间缓冲：绕过 Nginx 等中间层的 response buffering
DISABLE_MIDDLE_BUFFER = os.getenv("DISABLE_MIDDLE_BUFFER", "true").lower() != "false"

# 重试策略配置
RETRY_TRIES = C.RETRY_TRIES
RETRY_INTERVAL_MS = C.RETRY_INTERVAL_MS

# 请求体大小上限（防 DoS 攻击）
MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", str(2 * 1024 * 1024)))

# 检测 httpx send() 是否支持 timeout 参数（兼容不同版本）
_SEND_HAS_TIMEOUT = "timeout" in inspect.signature(httpx.AsyncClient.send).parameters

# 可重试的异常类型和 5xx 状态码定义
_RETRIABLE_EXC = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_RETRIABLE_5XX = (502, 503, 504)

# 创建 FastAPI 应用实例
app = FastAPI()

# 创建共享的 HTTP keepalive 连接池客户端
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
    """代理服务启动钩子。

    初始化排队闸门（QueueGate），并在启用预热时执行连接预热。
    """
    jlog("proxy_startup", host="0.0.0.0", port=C.PORT, backend_url=C.BACKEND_URL)

    # 初始化排队闸门
    app.state.gate = QueueGate()

    # 若启用预热则执行连接预热
    if WARMUP_ENABLED:
        await warmup_connections()


async def warmup_connections():
    """预热后端连接池。

    通过发送少量推理请求建立 TCP/TLS 连接，减少首次真实请求的延迟。
    - 按轮次（WARMUP_ROUNDS）重复发送
    - 每轮并发 WARMUP_CONN 个预热请求
    """
    jlog("warmup_start", conn_count=WARMUP_CONN, rounds=WARMUP_ROUNDS, model=WARMUP_MODEL)

    backend_url = build_backend_url("/v1/chat/completions")

    for round_num in range(1, WARMUP_ROUNDS + 1):
        jlog("warmup_round", round=round_num, total=WARMUP_ROUNDS)

        # 并发发送预热请求
        tasks = []
        for i in range(WARMUP_CONN):
            task = asyncio.create_task(
                send_warmup_request(backend_url, i, round_num)
            )
            tasks.append(task)

        # 等待所有预热任务完成（带超时保护）
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=WARMUP_TIMEOUT
            )
        except asyncio.TimeoutError:
            elog("warmup_timeout", round=round_num, timeout=WARMUP_TIMEOUT)

    jlog("warmup_complete", total_conn=WARMUP_CONN * WARMUP_ROUNDS)


async def send_warmup_request(backend_url: str, conn_id: int, round_num: int):
    """发送单个预热请求到后端推理引擎。

    构造最小化的推理请求体，建立连接并读取首个 chunk 后关闭。

    Args:
        backend_url: 后端推理服务的完整 URL。
        conn_id: 当前连接编号，用于日志追踪。
        round_num: 当前预热轮次编号。
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
            "max_tokens": 10  # 只需少量 token 即可完成预热
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

        # 读取首个 chunk 后关闭连接
        if r.status_code == 200:
            async for chunk in r.aiter_bytes(chunk_size=1024):
                break  # 只读首个 chunk 即可
            await r.aclose()
            jlog("warmup_success", conn_id=conn_id, round=round_num)
        else:
            elog("warmup_failed", conn_id=conn_id, round=round_num, status_code=r.status_code)
            await r.aclose()

    except Exception as e:
        elog("warmup_error", conn_id=conn_id, round=round_num, error_type=e.__class__.__name__, detail=str(e))


@app.on_event("shutdown")
async def shutdown():
    """代理服务关闭钩子。

    优雅关闭共享的 HTTP 连接池客户端，释放所有连接资源。
    """
    await client.aclose()
    jlog("proxy_shutdown")


# ── 底层发送与重试工具函数 ──

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
    """底层 HTTP 请求发送函数，封装 httpx 的请求构建与发送流程。

    手动构建 request 对象以支持 stream 模式，
    并根据 httpx 版本自动适配 timeout 参数传递方式。

    Args:
        client: httpx 异步客户端实例。
        method: HTTP 方法（如 "POST"、"GET"）。
        url: 请求目标 URL。
        content: 请求体字节内容。
        headers: 请求头字典。
        stream: 是否以流式模式发送。
        timeout: 可选的超时配置。

    Returns:
        httpx.Response: 后端返回的 HTTP 响应对象。
    """
    req = client.build_request(method, url, content=content, headers=headers)
    if _SEND_HAS_TIMEOUT and timeout is not None:
        return await client.send(req, stream=stream, timeout=timeout)
    return await client.send(req, stream=stream)


def _should_retry_status(stream: bool, status_code: int, attempt: int, total: int) -> bool:
    """判断是否应基于 HTTP 状态码进行重试。

    仅在流式请求且状态码为 502/503/504 且未达最大重试次数时返回 True。

    Args:
        stream: 是否为流式请求。
        status_code: 后端返回的 HTTP 状态码。
        attempt: 当前重试次数（从 1 开始）。
        total: 最大重试次数。

    Returns:
        bool: True 表示应重试，False 表示不重试。
    """
    return stream and status_code in _RETRIABLE_5XX and attempt < total


async def _close_resp_quiet(resp: httpx.Response) -> None:
    """静默关闭 HTTP 响应对象。

    在重试场景中用于安全释放前次响应资源，忽略关闭时可能发生的异常。

    Args:
        resp: 待关闭的 httpx 响应对象。
    """
    try:
        await resp.aclose()
    except Exception as e:
        elog("failed_to_close_response", error=str(e))


def _mark_retry_count(resp: httpx.Response, attempt: int) -> None:
    """在响应的 extensions 字典中记录实际重试次数。

    将重试计数写入 resp.extensions["app_retry_count"]，供下游透传到响应头。
    attempt 从 1 起计，因此实际重试次数 = attempt - 1。

    Args:
        resp: httpx 响应对象。
        attempt: 当前请求序号（1 表示首次请求，无重试）。
    """
    try:
        resp.extensions["app_retry_count"] = attempt - 1
    except Exception as e:
        elog("failed_to_set_retry_count", error=str(e))


async def _log_and_wait_status_retry(rid: str | None, attempt: int, status: int, interval: float, t0: float) -> None:
    """记录状态码重试日志并等待指定间隔后再发起下一次重试。

    Args:
        rid: 请求追踪 ID。
        attempt: 当前重试次数。
        status: 触发重试的 HTTP 状态码。
        interval: 重试等待间隔（秒）。
        t0: 本次请求的起始时间戳（perf_counter）。
    """
    elog(
        "retry_status",
        rid=rid, attempt=attempt, status=status,
        next_wait_ms=int(interval * 1000), elapsed=f"{(time.perf_counter() - t0) * 1000:.1f}ms",
    )
    await asyncio.sleep(interval)


def _is_retriable_exception(e: Exception) -> bool:
    """判断异常是否属于可重试类型。

    可重试异常包括：ConnectError、ConnectTimeout、PoolTimeout。

    Args:
        e: 捕获的异常实例。

    Returns:
        bool: True 表示该异常可重试。
    """
    return isinstance(e, _RETRIABLE_EXC)


async def _log_and_maybe_wait_exception(e: Exception, **ctx) -> bool:
    """记录异常日志，若异常可重试且未达上限则等待后返回 True。

    Args:
        e: 捕获的异常实例。
        **ctx: 上下文关键字参数，包含 rid、attempt、total、interval、t0。

    Returns:
        bool: True 表示应继续重试，False 表示放弃重试。
    """
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
    """带固定间隔重试的 HTTP 请求发送函数。

    采用固定重试策略（默认 RETRY_TRIES=3 次 + RETRY_INTERVAL_MS=100ms 间隔），
    在以下条件满足时自动重试：
        1) 连接异常：ConnectError / ConnectTimeout / PoolTimeout
        2) 流式请求收到 5xx 状态码：502/503/504
    成功后将重试次数写入 resp.extensions["app_retry_count"]。

    Args:
        client: httpx 异步客户端实例。
        method: HTTP 方法。
        url: 请求目标 URL。
        content: 请求体字节内容。
        headers: 请求头字典。
        stream: 是否以流式模式发送。
        timeout: 可选的超时配置。
        rid: 请求追踪 ID，用于日志关联。

    Returns:
        httpx.Response: 后端返回的 HTTP 响应对象。

    Raises:
        HTTPException: 当所有重试耗尽后抛出 502 错误。
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

            # 流式请求收到可重试的 5xx 状态码时进行重试
            if _should_retry_status(stream, resp.status_code, attempt, total):
                last_status = resp.status_code
                await _close_resp_quiet(resp)
                await _log_and_wait_status_retry(rid, attempt, last_status, interval, t0)
                continue

            # 将重试次数写入 extensions（首次成功则为 0）
            _mark_retry_count(resp, attempt)
            return resp

        except Exception as e:
            last_exc = e
            # 判断异常是否可重试，若可重试则等待后继续
            if await _log_and_maybe_wait_exception(e, rid=rid, attempt=attempt, total=total, interval=interval, t0=t0):
                continue

            # 非可重试的 httpx.RequestError 直接抛出 502
            if isinstance(e, httpx.RequestError):
                raise HTTPException(502, f"backend connect error: {e}") from e
            raise

    # 所有重试耗尽后的最终错误处理
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


# ── TTFT 首包延迟测量与流式转发 ──


async def _acquire_gate_early(req: Request, gate: QueueGate, rid: str) -> Dict[str, str]:
    """提前获取并立即释放排队闸门。

    在发送后端请求前获取闸门令牌，获取后立即释放，
    用于统计排队等待时间。若闸门拒绝则抛出 HTTPException。

    Args:
        req: FastAPI 请求对象。
        gate: 排队闸门实例。
        rid: 请求追踪 ID。

    Returns:
        Dict[str, str]: 包含排队等待信息的响应头字典。

    Raises:
        HTTPException: 当闸门获取失败时抛出。
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
    """向后端发送流式推理请求。

    封装 _send_with_fixed_retries，构建 POST 请求并以 stream 模式发送到后端引擎。

    Args:
        client: httpx 异步客户端实例。
        upstream_path: 后端 API 路径（如 "/v1/chat/completions"）。
        body_bytes: 请求体字节内容。
        req: 原始 FastAPI 请求对象（用于提取请求头）。
        rid: 请求追踪 ID。

    Returns:
        httpx.Response: 后端返回的流式 HTTP 响应对象。
    """
    return await _send_with_fixed_retries(
        client, "POST", build_backend_url(upstream_path),
        content=body_bytes,
        headers={k: v for k, v in req.headers.items() if k.lower() != "host"},
        stream=True,
        timeout=httpx.Timeout(connect=float(os.getenv("HTTPX_CONNECT_TIMEOUT", "10")), read=None, write=None, pool=None),
        rid=rid,
    )


def _build_passthrough_headers(r: httpx.Response, gate: QueueGate, queue_headers: Dict[str, str]) -> Dict[str, str]:
    """构建透传给客户端的响应头。

    - 透传后端响应中的 X-* 自定义头
    - 添加重试计数头 X-Retry-Count
    - 合并排队观测头
    - 可选禁用中间层缓冲

    Args:
        r: 后端 httpx 响应对象。
        gate: 排队闸门实例。
        queue_headers: 排队观测头字典。

    Returns:
        Dict[str, str]: 合并后的响应头字典。
    """
    headers = {k: v for k, v in r.headers.items() if k.lower().startswith("x-")}

    ext = getattr(r, "extensions", None)
    if isinstance(ext, dict):
        retry_cnt = ext.get("app_retry_count")
        if retry_cnt is not None:
            headers["X-Retry-Count"] = str(retry_cnt)

    # 合并排队观测头
    headers.update(gate.obs_headers(queue_headers))

    # 禁用中间层缓冲，确保流式数据实时达到客户端
    if DISABLE_MIDDLE_BUFFER:
        headers["X-Accel-Buffering"] = "no"  # 禁用 Nginx 代理缓冲
        headers["Cache-Control"] = "no-cache, no-store, no-transform, must-revalidate"  # 禁用所有缓存

    return headers


async def _stream_gen(req: Request, r: httpx.Response, rid: str, request_start_time: float):
    """流式响应生成器，并测量 TTFT 首包延迟。

    - 小 chunk 直接 yield（快速路径）
    - 首包 flush：按字节数 + 分隔符 + 时间阈值触发
    - 后续 flush：按动态字节阈值 + 分隔符 + 时间间隔触发
    - 记录 TTFT、Total Time 和 chunk 间隔统计

    Args:
        req: FastAPI 请求对象，用于检测客户端断开。
        r: 后端流式 HTTP 响应对象。
        rid: 请求追踪 ID。
        request_start_time: 请求开始的时间戳（perf_counter）。

    Yields:
        bytes: 缓冲后的数据块。
    """
    buf = bytearray()
    last_flush = time.perf_counter()
    first_flush_done = False
    dyn_base = max(STREAM_FLUSH_BYTES, int(MAX_CONN / 4))

    # 记录首个 chunk 到达时间（TTFT）
    ttft_ms = None
    total_bytes = 0

    # 统计 chunk 间隔信息
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

            # 记录 TTFT（首个 chunk 到达时间）
            if ttft_ms is None:
                ttft_ms = (current_chunk_time - request_start_time) * 1000
                jlog("ttft", rid=rid, ttft_ms=f"{ttft_ms:.2f}")

            # 计算相邻 chunk 间隔
            if last_chunk_time is not None:
                chunk_interval_ms = (current_chunk_time - last_chunk_time) * 1000
                chunk_intervals.append(chunk_interval_ms)
                jlog("chunk_interval", rid=rid, chunk_num=chunk_count,
                     interval_ms=f"{chunk_interval_ms:.2f}", chunk_size=len(chunk))

            last_chunk_time = current_chunk_time
            total_bytes += len(chunk)

            # 小 chunk 快速路径：直接输出无需缓冲
            if not first_flush_done and len(chunk) <= FAST_PATH_BYTES:
                yield chunk
                first_flush_done = True
                continue

            buf.extend(chunk)
            now = time.perf_counter()

            # 首包 flush 判断
            if _should_flush_first_packet(buf, first_flush_done, now, last_flush):
                yield bytes(buf)
                buf.clear()
                first_flush_done = True
                last_flush = now
                continue

            # 后续 flush：动态字节阈值 + 随机抖动 + 时间间隔
            dyn_bytes = dyn_base + random.randint(0, max(1, dyn_base // 8))
            if _should_flush(buf, dyn_bytes, last_flush, now):
                yield bytes(buf)
                buf.clear()
                last_flush = now
    finally:
        if buf:
            yield bytes(buf)

        # 记录 Total Time 和 chunk 统计
        total_time_ms = (time.perf_counter() - request_start_time) * 1000
        jlog("total_time", rid=rid, total_time_ms=f"{total_time_ms:.2f}", total_bytes=total_bytes)

        # 输出 chunk 间隔平均值统计
        if chunk_intervals:
            avg_interval_ms = sum(chunk_intervals) / len(chunk_intervals)
            jlog("chunk_interval_avg", rid=rid, chunk_count=chunk_count,
                 avg_interval_ms=f"{avg_interval_ms:.2f}",
                 min_interval_ms=f"{min(chunk_intervals):.2f}",
                 max_interval_ms=f"{max(chunk_intervals):.2f}")

        await r.aclose()


# ── TTFT 刷新策略判断函数 ──

def _should_flush_first_packet(buf: bytearray, first_flush_done: bool, now: float, last_flush: float) -> bool:
    """判断是否应触发首包 flush。

    - 已完成首包 flush 则返回 False
    - 缓冲区大小 >= FIRST_FLUSH_BYTES 时返回 True
    - 启用分隔符 flush 且包含 \n\n 时返回 True
    - 距上次 flush 时间间隔 >= FIRST_FLUSH_MS 时返回 True

    Args:
        buf: 当前缓冲区内容。
        first_flush_done: 首包是否已刷新。
        now: 当前时间戳。
        last_flush: 上次刷新时间戳。

    Returns:
        bool: True 表示应立即刷新缓冲区。
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
    """判断是否应触发后续数据 flush。

    - 缓冲区大小 >= 动态字节阈值时返回 True
    - 启用分隔符 flush 且包含 \n\n 时返回 True
    - 距上次 flush 时间间隔 >= STREAM_FLUSH_MS 时返回 True

    Args:
        buf: 当前缓冲区内容。
        dyn_bytes: 动态计算的字节阈值（含随机抖动）。
        last_flush: 上次刷新时间戳。
        now: 当前时间戳。

    Returns:
        bool: True 表示应立即刷新缓冲区。
    """
    return (
        len(buf) >= dyn_bytes or
        (ENABLE_DELIM_FLUSH and b"\n\n" in buf) or
        (now - last_flush) >= STREAM_FLUSH_MS
    )


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    """处理 /v1/chat/completions 接口的简化版代理。

    相比 gateway.py 采用更简单的流程：
    - 读取并校验请求体
    - 获取排队闸门（用于观测统计）
    - 转发到后端引擎（带重试）
    - 流式返回并测量 TTFT 和 Total Time
    - 记录各阶段耗时

    Args:
        req: FastAPI 请求对象。

    Returns:
        StreamingResponse: 流式响应，透传后端状态码和内容。
    """
    rid = req.headers.get("x-request-id")
    gate: QueueGate = app.state.gate

    # 记录请求开始时间，用于计算 TTFT 和 Total Time
    request_start_time = time.perf_counter()

    # 步骤 1: 读取请求体
    t1_start = time.perf_counter()
    body_bytes = await read_json_body(req, rid, MAX_REQUEST_BYTES)
    t1_end = time.perf_counter()
    t1_ms = (t1_end - t1_start) * 1000
    jlog("req_recv", rid=rid, path=str(req.url.path), body_len=len(body_bytes), step1_read_body_ms=f"{t1_ms:.2f}")

    # 步骤 2: 获取排队闸门
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

    # 步骤 3: 发送流式请求到后端
    t3_start = time.perf_counter()
    r = await _send_stream_request(client, "/v1/chat/completions", body_bytes, req, rid)
    t3_end = time.perf_counter()
    t3_ms = (t3_end - t3_start) * 1000
    jlog("backend_response", rid=rid, status_code=r.status_code, step3_send_request_ms=f"{t3_ms:.2f}")

    # 步骤 4: 构建透传响应头
    t4_start = time.perf_counter()
    passthrough = _build_passthrough_headers(r, gate, queue_headers)
    t4_end = time.perf_counter()
    t4_ms = (t4_end - t4_start) * 1000
    jlog("build_headers", rid=rid, step4_build_headers_ms=f"{t4_ms:.2f}")

    # 步骤 5: 创建流式响应，TTFT 和 Total Time 在生成器中记录
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

    # 输出代理内部各阶段耗时统计
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
