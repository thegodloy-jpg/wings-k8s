# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: proxy/http_client.py
# Purpose: Proxy-specific async HTTP client helper for backend calls.
# Status: Active reused helper module.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Keep timeout/retry behavior consistent with gateway expectations.
# - Use lightweight wrappers to centralize backend call policy.
# -----------------------------------------------------------------------------
# -*- coding: utf-8 -*-
"""
http_client.py
- 建立 httpx.AsyncClient，配置连接池、HTTP/2、keepalive 等
- 不使用 base_url（避免自动拼接）；gateway 每次构造“绝对 URL（scheme://host:port/path）”
"""

from __future__ import annotations
import httpx
from . import settings as C


async def create_async_client() -> httpx.AsyncClient:
    """
    创建一次性的全局 AsyncClient：
    - 限制连接池大小，启用 keep-alive，降低建连开销
    - 根据配置决定是否启用 HTTP/2（仅后端支持时开启）
    - 读超时 read=None 便于长流式；异常/重试在应用层处理
    """
    limits = httpx.Limits(
        max_connections=C.MAX_CONN,
        max_keepalive_connections=C.MAX_KEEPALIVE,
        keepalive_expiry=C.KEEPALIVE_EXPIRY,
    )

    # 说明：部分版本 httpx 通过 Client(http2=True) 控 H2，这里放在 transport 中以更好兼容
    transport = httpx.AsyncHTTPTransport(
        http2=C.HTTP2_ENABLED,
    )

    client = httpx.AsyncClient(
        transport=transport,
        limits=limits,
        timeout=httpx.Timeout(
            connect=10.0,  # 连接阶段超时
            read=None,     # 流式读取不设总超时
            write=10.0,    # 写入阶段超时
            pool=None,     # 不设置池等待超时（由应用层控制）
        ),
        follow_redirects=False,
        headers={"connection": "keep-alive"},
        trust_env=False,  # 不继承系统代理；settings 也清理了 *_proxy
        # 不设置 base_url，防止自动拼路径的陷阱
    )
    return client
