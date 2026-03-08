# =============================================================================
# 文件: utils/http_client.py
# 用途: 通用 HTTP 客户端工具（遗留代码兼容）
# 状态: 已废弃但保留以兼容旧组件
#
# 功能概述:
#   提供异步 HTTP 客户端基础类，封装 httpx 请求。
#   当前代码中无活跃调用者，仅保留用于向后兼容。
#   新代码应使用 proxy/http_client.py 中的异步客户端。
#
# Sidecar 架构契约:
#   - 保持请求超时/错误处理可预测
#   - 避免全局副作用
#
# =============================================================================
import httpx
import logging
import os
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class HTTPClient:
    """HTTP

    .. deprecated::
        This class has no active callers in the current codebase and is
        retained only for legacy compatibility.  New code should use the
        async client helpers in ``proxy/http_client.py`` instead.
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip('/')
        self.client = httpx.AsyncClient(timeout=float(os.getenv("HTTP_CLIENT_TIMEOUT", "300")))

    async def close(self):
        """关闭底层 httpx 客户端连接。"""
        await self.client.aclose()

    async def health_check(self) -> bool:
        """对 /health 端点执行健康检查。

        Returns:
            bool: 请求成功且状态码为 200 时返回 True
        """
        try:
            response = await self.client.get(f"{self.base_url}/health")
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"Health check failed: {e}")
            return False

    async def forward_request(self, method: str, path: str, data: Optional[Dict[str, Any]] = None,
                            headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """将请求转发到后端服务。

        Args:
            method:  HTTP 方法（GET、POST）
            path:    请求路径（会拼接 base_url）
            data:    请求数据（GET 时作查询参数，POST 时作 JSON body）
            headers: 可选 HTTP 头

        Returns:
            Dict[str, Any]: 解析后的 JSON 响应。出错时返回包含 error 字段的字典
        """
        url = f"{self.base_url}{path}"

        try:
            if method.upper() == "GET":
                response = await self.client.get(url, params=data, headers=headers)
            elif method.upper() == "POST":
                response = await self.client.post(url, json=data, headers=headers)
            else:
                raise ValueError(f"Unsupported method: {method}")

            response.raise_for_status()
            return response.json()

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error forwarding request: {e}")
            return {"error": str(e), "status_code": e.response.status_code}
        except Exception as e:
            logger.error(f"Error forwarding request: {e}")
            return {"error": str(e)}