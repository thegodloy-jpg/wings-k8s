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
-  /  /  / JSON
-  URL build_backend_url scheme+host+port
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
    """topk"""
    v = os.getenv(name, default)
    v = (v or "").strip().lower()

    if v in ("1", "true"):
        return True
    if v in ("0", "false"):
        return False

    #  defaultdefault
    d = (default or "").strip().lower()
    return d in ("1", "true")


def want_stream(v: Any) -> bool:
    """ stream=true"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "t", "on")
    return False


def make_upstream_headers(req: Request, want_gzip: bool = False) -> Dict[str, str]:
    """

    -  Authorization / X-Request-Id
    -  identity identity gzip  TTFT
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
     JSON
    - 413
    -  JSON 400
    -  bytes
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
    """ INFO JSON """
    try:
        log_entry = {"evt": evt, **fields}
        C.logger.info(json.dumps(log_entry, ensure_ascii=False))
    except (TypeError, ValueError) as e:
        C.logger.error(f"Failed to serialize log entry: {log_entry}", exc_info=e)
    except Exception as e:
        C.logger.error("Unexpected error while writing log", exc_info=e)


def elog(evt: str, **fields):
    """ ERROR JSON """
    try:
        log_entry = {"evt": evt, **fields}
        C.logger.error(json.dumps(log_entry, ensure_ascii=False))
    except (TypeError, ValueError) as e:
        C.logger.error(f"Failed to serialize log entry: {log_entry}", exc_info=e)
    except Exception as e:
        C.logger.error("Unexpected error while writing log", exc_info=e)


def ms(sec: float) -> str:
    """ -> x.xms"""
    return f"{sec * 1000:.1f}ms"


def _backend_origin() -> str:
    """
     BACKEND_URL  scheme://host[:port] path/query/fragment
     origin
    """
    u = urlsplit(C.BACKEND_URL.strip())
    if not u.scheme or not u.netloc:
        raise ValueError("BACKEND_URL  http://127.0.0.1:17000 ")
    if u.path and u.path not in ("", "/"):
        C.logger.warning("BACKEND_URL  '%s' %s://%s", u.path, u.scheme, u.netloc)
    return f"{u.scheme}://{u.netloc}"


def build_backend_url(path: str) -> str:
    """
     originscheme+host+port URL  URL
    -  path  '/'
    -  base_url  URL http://10.0.0.8:17000/v1/chat/completions
    """
    if not path.startswith("/"):
        path = "/" + path
    return _backend_origin() + path


def rebuild_request_json(req: Request, new_payload: Dict[str, Any]) -> Request:
    """
     payload  Request
     scopereceive  body
    """
    new_bytes = json.dumps(new_payload, ensure_ascii=False).encode("utf-8")

    # 1.  content-length
    new_scope = req.scope.copy()
    headers = []
    for header_name, header_value in req.scope["headers"]:
        if header_name == b"content-length":
            header_value = str(len(new_bytes)).encode()
        headers.append((header_name, header_value))
    new_scope["headers"] = headers

    # 2.  receive
    async def receive() -> dict:
        return {"type": "http.request", "body": new_bytes, "more_body": False}

    return Request(new_scope, receive)