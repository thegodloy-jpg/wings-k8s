# =============================================================================
# 文件: proxy/http_client.py
# 用途: 代理层专用的异步 HTTP 客户端工厂
# 状态: 活跃，复用自 wings 项目
#
# 功能概述:
#   本模块提供统一配置的 httpx.AsyncClient 实例创建函数，
#   供 gateway 使用。
#
# 配置要点:
#   - 启用 keep-alive 连接复用，减少 TCP 建连开销
#   - 可选启用 HTTP/2 多路复用
#   - 读取超时设为 None，支持长时间流式响应
#   - 不设置 base_url，由 gateway 在每次请求时拼接
#
# Sidecar 架构契约:
#   - 超时/重试行为与 gateway 保持一致
#   - 轻量级封装，集中管理后端调用策略
#
# =============================================================================
# -*- coding: utf-8 -*-
"""
http_client.py - 异步 HTTP 客户端工厂。

负责:
  - 创建配置统一的 httpx.AsyncClient 实例
  - 启用 HTTP/2、连接池、keepalive 等特性
  - 不设置 base_url，由 gateway 拼接完整 URL
"""

from __future__ import annotations
import httpx
from . import settings as C


async def create_async_client() -> httpx.AsyncClient:
    """创建配置统一的异步 HTTP 客户端实例。

    配置说明:
        - max_connections:          最大连接数 (C.MAX_CONN)
        - max_keepalive_connections: 最大 keep-alive 连接数 (C.MAX_KEEPALIVE)
        - keepalive_expiry:         keep-alive 超时秒数
        - http2:                    是否启用 HTTP/2 (C.HTTP2_ENABLED)
        - connect timeout:          连接超时 (C.HTTPX_CONNECT_TIMEOUT)
        - read timeout:             None - 流式响应可能持续很长
        - write timeout:            写入超时 (C.HTTPX_WRITE_TIMEOUT)
        - pool timeout:             连接池超时 (C.HTTPX_POOL_TIMEOUT)

    Returns:
        httpx.AsyncClient: 配置完成的客户端实例

    注意:
        - trust_env=False 禁止继承系统代理配置，避免本地后端走代理
        - follow_redirects=False 不跟随重定向，避免性能不可预测
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
            connect=C.HTTPX_CONNECT_TIMEOUT,
            read=None,     #
            write=C.HTTPX_WRITE_TIMEOUT,
            pool=C.HTTPX_POOL_TIMEOUT,
        ),
        follow_redirects=False,
        headers={"connection": "keep-alive"},
        trust_env=False,  # settings  *_proxy
        #  base_url
    )
    return client
