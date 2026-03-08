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
-  httpx.AsyncClientHTTP/2keepalive
-  base_urlgateway  URLscheme://host:port/path
"""

from __future__ import annotations
import httpx
from . import settings as C


async def create_async_client() -> httpx.AsyncClient:
    """
     AsyncClient
    -  keep-alive
    -  HTTP/2
    -  read=None /
    """
    limits = httpx.Limits(
        max_connections=C.MAX_CONN,
        max_keepalive_connections=C.MAX_KEEPALIVE,
        keepalive_expiry=C.KEEPALIVE_EXPIRY,
    )

    #  httpx  Client(http2=True)  H2 transport
    transport = httpx.AsyncHTTPTransport(
        http2=C.HTTP2_ENABLED,
    )

    client = httpx.AsyncClient(
        transport=transport,
        limits=limits,
        timeout=httpx.Timeout(
            connect=10.0,  #
            read=None,     #
            write=10.0,    #
            pool=None,     #
        ),
        follow_redirects=False,
        headers={"connection": "keep-alive"},
        trust_env=False,  # settings  *_proxy
        #  base_url
    )
    return client
