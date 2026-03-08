# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: proxy/gateway.py
# Purpose: Primary business proxy app forwarding OpenAI-compatible requests to backend engine.
# Status: Active reused gateway implementation from wings.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Preserve forwarding semantics and retry behavior.
# - Avoid incompatible rewrites; apply minimal adaptation only.
# -----------------------------------------------------------------------------
# -*- coding: utf-8 -*-
"""
Method 2 + /think
==============================================================================

1)  URL http://10.0.0.8:17000  ****
    build_backend_url(path) ** URL**
   scheme://host:port/path httpx  base_url
2)  acquire release/
   jlog/elog/ flush
3)
   POST : /v1/chat/completions  /v1/chat/completions/
          /v1/completions       /v1/completions/
          /v1/rerank            /v1/rerank
          /v1/embeddings        /v1/embeddings
          /tokenize             /tokenizevLLM
   GET  : /health             200
          /v1/version           /v1/version
          /metrics              /metricsvLLM, Prometheus
4) /think****
5)  health.py /health
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
    want_topk,
    rebuild_request_json,
    make_upstream_headers,
    read_json_body,
    jlog, elog, ms,
    build_backend_url,
)
from .speaker_logging import configure_worker_logging

#  health.py  API/health  health.py
from .health import (
    setup_health_monitor,
    teardown_health_monitor,
    map_http_code_from_state,
    build_health_body,
    build_health_headers,
)

configure_worker_logging()
#  413
MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", str(2 * 1024 * 1024)))

#  FastAPI
app = FastAPI()

#  URL
app.state.backend = C.BACKEND_URL

#  httpx send()  timeout
_SEND_HAS_TIMEOUT = "timeout" in inspect.signature(httpx.AsyncClient.send).parameters


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

#  5xx
_RETRIABLE_EXC = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_RETRIABLE_5XX = (502, 503, 504)

#  /


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
    # rid/attempt/total/interval/t0
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

    -  RETRY_TRIES=3 + RETRY_INTERVAL_MS=100ms
    -
        1) ConnectError / ConnectTimeout / PoolTimeout
        2)  502/503/504
    -  resp.extensions["app_retry_count"]
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

            #  &  5xx
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


#   http


@app.on_event("startup")
async def _startup():
    """

    -  httpx.AsyncClient
    -
    -  health.py
    """
    configure_worker_logging(force=True)
    C.log_boot_plan()
    app.state.client = await create_async_client()
    app.state.gate = QueueGate()

    #
    setup_health_monitor(app)
    C.logger.info("Reason-Proxy is starting on %s:%s (health monitor loop enabled)", C.HOST, C.PORT)


@app.on_event("shutdown")
async def _shutdown():
    """ http """
    try:
        await teardown_health_monitor(app)
    except asyncio.CancelledError:
        #  CancelledError  Starlette
        pass
    await app.state.client.aclose()


#


def _copy_entity_headers(resp: httpx.Response) -> Dict[str, str]:
    """ X-* """
    return {
        k: v for k, v in resp.headers.items()
        if k.lower().startswith("x-") or k.lower() in ("content-type", "content-encoding", "etag", "last-modified")
    }


def _merge_obs_and_retry_headers(
        gate: QueueGate,
        queue_headers: Dict[str, str],
        resp: httpx.Response) -> Dict[str, str]:
    """ X-Retry-Count"""
    merged = gate.obs_headers(queue_headers)
    ext = getattr(resp, "extensions", None)
    if isinstance(ext, dict):
        retry_cnt = ext.get("app_retry_count")
        if retry_cnt is not None:
            merged["X-Retry-Count"] = str(retry_cnt)
    return merged


def _content_length(resp: httpx.Response) -> int | None:
    """ Content-Length None"""
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
    """ stream=True"""
    url = build_backend_url(upstream_path)
    return await _send_with_fixed_retries(
        client, "POST", url,
        content=body_bytes,
        headers=make_upstream_headers(req, want_gzip=False),
        stream=True,  #
        rid=rid,
    )


async def _pipe_nonstream(req: Request, r: httpx.Response, rid: str | None) -> AsyncIterator[bytes]:
    """(no description)"""
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
    """ HTTPException"""
    queue_headers = await gate.acquire(dict(req.headers))
    await gate.release()
    jlog("gate_acquired_released_early", rid=rid, wait_hdr=queue_headers.get("X-Queued-Wait"))
    return queue_headers


async def _forward_nonstream(req: Request, upstream_path: str):
    """
     stream=True
    1)  JSON
    2)
    3)
    4)
    """
    client: httpx.AsyncClient = app.state.client
    gate: QueueGate = app.state.gate
    rid = req.headers.get("x-request-id")

    #
    body_bytes = await read_json_body(req, rid, MAX_REQUEST_BYTES)
    jlog("req_recv", rid=rid, path=str(req.url.path), body_len=len(body_bytes))

    #
    try:
        queue_headers = await _acquire_gate_early_nonstream(req, gate, rid)
    except HTTPException as e:
        elog("gate_acquire_error", rid=rid, status=e.status_code, detail=str(e.detail))
        return JSONResponse(status_code=e.status_code,
                            content={"detail": e.detail},
                            headers=gate.obs_headers(e.headers))

    #
    r = await _send_nonstream_request(client, upstream_path, body_bytes, req, rid)

    #  + /
    merged = _merge_obs_and_retry_headers(gate, queue_headers, r)
    entity_headers = _copy_entity_headers(r)

    #
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

    #
    return StreamingResponse(
        _pipe_nonstream(req, r, rid),
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "application/json"),
        headers={**entity_headers, **merged},
    )


#


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
     r  r
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

            #
            if not first_flush_done and len(chunk) <= C.FAST_PATH_BYTES:
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
        await r.aclose()


def _build_passthrough_headers(r: httpx.Response, gate: QueueGate, queue_headers: Dict[str, str]) -> Dict[str, str]:
    """
     X-* retry  SSE
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

     HTTPException
    """
    queue_headers = await gate.acquire(dict(req.headers))
    await gate.release()
    jlog("gate_acquired_released_early", rid=rid, wait_hdr=queue_headers.get("X-Queued-Wait"))
    return queue_headers


async def _forward_stream(req: Request, upstream_path: str):
    """
    SSE/Chunked
    -
    - / flush
    -  5xx
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
        #
        if not released_early:
            try:
                await gate.release()
                jlog("gate_release_on_finally", rid=rid)
            except Exception as ex:
                elog("gate_release_error", rid=rid, detail=str(ex))


#
FORCE_TOPK_TOPP = want_topk("WINGS_FORCE_CHAT_TOPK_TOPP", "1")  #


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    """
      /v1/chat/completions
     stream
    """
    try:
        payload: Dict[str, Any] = await req.json()
    except Exception:
        payload = {}

    #  top_k/topp
    if FORCE_TOPK_TOPP and isinstance(payload, dict):
        payload["top_k"] = -1
        payload["top_p"] = 1
        req = rebuild_request_json(req, payload)

    if want_stream(payload.get("stream", False)):
        return await _forward_stream(req, "/v1/chat/completions")
    return await _forward_nonstream(req, "/v1/chat/completions")


@app.post("/v1/completions")
async def completions(req: Request):
    """
      /v1/completions
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
    Responses   /v1/responses
    -  /v1/chat/completions/v1/completions
      *  "stream"
      *
    - / flush
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
    #  /v1/rerank
    return await _forward_nonstream(req, "/v1/rerank")


@app.post("/v1/embeddings")
async def embeddings(req: Request):
    #  /v1/embeddings
    return await _forward_nonstream(req, "/v1/embeddings")


@app.post("/tokenize")
async def tokenize(req: Request):
    # vLLM  /tokenize
    return await _forward_nonstream(req, "/tokenize")


def _extract_metrics_headers(r: httpx.Response) -> dict:
    """ x-* Content-Type"""
    headers = {k: v for k, v in r.headers.items() if k.lower().startswith("x-")}
    headers["Content-Type"] = r.headers.get(
        "content-type",
        "text/plain; version=0.0.4; charset=utf-8",
    )
    return headers


async def _pipe_metrics(req: Request, r: httpx.Response):
    """(no description)"""
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
    vLLM GET  /metrics
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
    HunyuanVideo
     /v1/videos/text2video
     MAX_REQUEST_BYTES
    """
    return await _forward_nonstream(req, "/v1/videos/text2video")


@app.get("/v1/videos/text2video/{task_id}")
async def hv_text2video_status(task_id: str, req: Request):
    """
    HunyuanVideo
     /v1/videos/text2video/{task_id}
    """
    client: httpx.AsyncClient = app.state.client
    rid = req.headers.get("x-request-id")
    url = build_backend_url(f"/v1/videos/text2video/{task_id}")

    #  JSON
    r = await _send_with_fixed_retries(
        client, "GET", url,
        headers=make_upstream_headers(req, want_gzip=False),
        stream=True,  #
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

#  /health health.py


@app.get("/health")
async def health_get(request: Request):
    """
    GET /health   health.py
    HTTP 200=201=502=503=
    """
    h = app.state.health
    code = map_http_code_from_state(h)
    headers = build_health_headers(h)
    body = build_health_body(h, code)
    return JSONResponse(status_code=code, content=body, headers=headers)


@app.head("/health")
async def health_head(request: Request):
    """
    HEAD /health
     Kubernetes readinessProbe/livenessProbe httpGet
    """
    h = app.state.health
    code = map_http_code_from_state(h)
    headers = build_health_headers(h)
    return Response(status_code=code, headers=headers)


#  /v1/models


@app.get("/v1/models")
async def models_proxy(request: Request):
    """
    GET /v1/models  vllm/sglang

    """
    rid = request.headers.get("x-request-id")
    url = build_backend_url("/v1/models")
    try:
        upstream_headers = make_upstream_headers(request)
        resp = await app.state.client.get(url, headers=upstream_headers, timeout=10.0)
        entity_headers = _copy_entity_headers(resp)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=entity_headers,
            media_type=resp.headers.get("content-type", "application/json"),
        )
    except Exception as e:
        elog("models_proxy_error", rid=rid, detail=str(e))
        raise HTTPException(status_code=502, detail=f"Backend unavailable: {e}")


@app.get("/v1/version")
async def version_proxy(req: Request):
    """
     WINGS_VERSION / WINGS_BUILD_DATE
    : WINGS_VERSION="25.0.0.1", WINGS_BUILD_DATE="2025-08-30"
     JSON
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