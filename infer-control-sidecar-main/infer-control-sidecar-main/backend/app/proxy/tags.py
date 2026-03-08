# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: proxy/tags.py
# Purpose: Tag constants and helper utilities shared across proxy components.
# Status: Active reused constants module.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Treat tags as API-like constants.
# - Coordinate changes with all proxy log/metrics consumers.
# -----------------------------------------------------------------------------
# -*- coding: utf-8 -*-
"""
tagging.py
- 通用工具：流式识别 / 结构化日志 / 头部构造 / JSON 读取校验
- 新增：后端绝对 URL 构造器（build_backend_url），仅替换后端的 scheme+host+port
"""

from __future__ import annotations
import os
from typing import Any, Dict, Optional
from urllib.parse import urlsplit
import json
from fastapi import HTTPException, Request
import orjson

from . import settings as C


def want_topk(name: str, default: str = "1") -> bool:
    """topk环境变量的识别函数"""
    v = os.getenv(name, default)
    v = (v or "").strip().lower()

    if v in ("1", "true"):
        return True
    if v in ("0", "false"):
        return False

    # 非法值：回退 default（default 也按同样规则解析）
    d = (default or "").strip().lower()
    return d in ("1", "true")


def want_stream(v: Any) -> bool:
    """宽松识别 stream=true。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "t", "on")
    return False


def make_upstream_headers(req: Request, want_gzip: bool = False) -> Dict[str, str]:
    """
    组装转发到后端的请求头（最小化且保持长连）：
    - 透传 Authorization / X-Request-Id（若存在）
    - 接收编码：流式默认 identity；非流式亦可 identity，避免 gzip 影响 TTFT 观测
    """
    h = {
        "content-type": "application/json",
        "accept-encoding": "gzip" if want_gzip else "identity",
        "connection": "keep-alive",
    }
    auth = req.headers.get("authorization")
    if auth:
        h["authorization"] = auth
    rid = req.headers.get("x-request-id")
    if rid:
        h["x-request-id"] = rid
    return h


async def read_json_body(req: Request, rid: Optional[str], max_request_bytes: int) -> bytes:
    """
    读取并校验 JSON 请求体：
    - 限制最大字节数（413）
    - 校验 JSON 格式（400）
    - 返回原始 bytes，避免二次序列化损耗
    """
    body = await req.body()
    if max_request_bytes and len(body) > max_request_bytes:
        elog("req_too_large", rid=rid, body_len=len(body), limit=max_request_bytes)
        raise HTTPException(413, "request entity too large")
    try:
        _ = orjson.loads(body)
    except Exception as e:
        elog("req_json_error", rid=rid, error=str(e))
        raise HTTPException(400, f"invalid json: {e}") from e
    return body


def jlog(evt: str, **fields):
    """结构化 INFO 日志（JSON 一行）。"""
    try:
        log_entry = {"evt": evt, **fields}
        C.logger.info(json.dumps(log_entry, ensure_ascii=False))
    except (TypeError, ValueError) as e:
        C.logger.error(f"Failed to serialize log entry: {log_entry}", exc_info=e)
    except Exception as e:
        C.logger.error("Unexpected error while writing log", exc_info=e)


def elog(evt: str, **fields):
    """结构化 ERROR 日志（JSON 一行）。"""
    try:
        log_entry = {"evt": evt, **fields}
        C.logger.error(json.dumps(log_entry, ensure_ascii=False))
    except (TypeError, ValueError) as e:
        C.logger.error(f"Failed to serialize log entry: {log_entry}", exc_info=e)
    except Exception as e:
        C.logger.error("Unexpected error while writing log", exc_info=e)


def ms(sec: float) -> str:
    """秒 -> 毫秒字符串（x.xms）。"""
    return f"{sec * 1000:.1f}ms"


def _backend_origin() -> str:
    """
    从 BACKEND_URL 中提取 scheme://host[:port]，忽略任何 path/query/fragment。
    若发现带有路径，会告警但自动忽略（仅保留 origin）。
    """
    u = urlsplit(C.BACKEND_URL.strip())
    if not u.scheme or not u.netloc:
        raise ValueError("BACKEND_URL 必须包含协议与主机（例如 http://127.0.0.1:17000 ）")
    if u.path and u.path not in ("", "/"):
        C.logger.warning("BACKEND_URL 包含路径 '%s'，已忽略，仅使用 %s://%s", u.path, u.scheme, u.netloc)
    return f"{u.scheme}://{u.netloc}"


def build_backend_url(path: str) -> str:
    """
    用“后端 origin（scheme+host+port）”替换 URL 的前缀，组合成绝对 URL：
    - 保证 path 以 '/' 开头
    - 不做 base_url 拼接，始终返回完整绝对 URL（例如 http://10.0.0.8:17000/v1/chat/completions ）
    """
    if not path.startswith("/"):
        path = "/" + path
    return _backend_origin() + path


def rebuild_request_json(req: Request, new_payload: Dict[str, Any]) -> Request:
    """
    用新的 payload 重新构造 Request，避免触碰受保护成员。
    返回的新实例与原来的 scope、receive 兼容，仅 body 被替换。
    """
    new_bytes = json.dumps(new_payload, ensure_ascii=False).encode("utf-8")

    # 1. 复制并修正 content-length
    new_scope = req.scope.copy()
    headers = []
    for header_name, header_value in req.scope["headers"]:
        if header_name == b"content-length":
            header_value = str(len(new_bytes)).encode()
        headers.append((header_name, header_value))
    new_scope["headers"] = headers

    # 2. 构造一次性 receive
    async def receive() -> dict:
        return {"type": "http.request", "body": new_bytes, "more_body": False}

    return Request(new_scope, receive)