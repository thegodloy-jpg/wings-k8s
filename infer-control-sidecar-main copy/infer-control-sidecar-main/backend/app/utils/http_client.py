import httpx
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class HTTPClient:
    """HTTP客户端，用于转发请求到引擎服务"""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip('/')
        self.client = httpx.AsyncClient(timeout=300.0)

    async def close(self):
        """关闭客户端"""
        await self.client.aclose()

    async def health_check(self) -> bool:
        """
        检查引擎服务健康状态

        Returns:
            bool: 服务是否健康
        """
        try:
            response = await self.client.get(f"{self.base_url}/health")
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"Health check failed: {e}")
            return False

    async def forward_request(self, method: str, path: str, data: Optional[Dict[str, Any]] = None,
                            headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """
        转发请求到引擎服务

        Args:
            method: HTTP方法
            path: 请求路径
            data: 请求数据
            headers: 请求头

        Returns:
            Dict[str, Any]: 响应数据
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