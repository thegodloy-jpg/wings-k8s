from typing import Dict, Any, Optional
from app.utils.http_client import HTTPClient
from app.config.settings import settings
import logging

logger = logging.getLogger(__name__)


class ProxyService:
    """代理服务，负责将请求转发到引擎服务"""

    def __init__(self):
        self.engine_client = None
        self._initialized = False

    def initialize(self):
        """初始化引擎客户端"""
        if not self._initialized:
            engine_url = f"http://{settings.ENGINE_HOST}:{settings.ENGINE_PORT}"
            self.engine_client = HTTPClient(base_url=engine_url)
            self._initialized = True
            logger.info(f"Proxy service initialized with engine URL: {engine_url}")

    async def forward_completion(self, prompt: str, **kwargs) -> Dict[str, Any]:
        """
        转发补全请求

        Args:
            prompt: 提示文本
            **kwargs: 其他参数

        Returns:
            Dict[str, Any]: 引擎响应
        """
        if not self._initialized:
            self.initialize()

        data = {
            "prompt": prompt,
            **kwargs
        }

        return await self.engine_client.forward_request(
            method="POST",
            path="/v1/completions",
            data=data
        )

    async def forward_chat(self, messages: list, **kwargs) -> Dict[str, Any]:
        """
        转发聊天请求

        Args:
            messages: 消息列表
            **kwargs: 其他参数

        Returns:
            Dict[str, Any]: 引擎响应
        """
        if not self._initialized:
            self.initialize()

        data = {
            "messages": messages,
            **kwargs
        }

        return await self.engine_client.forward_request(
            method="POST",
            path="/v1/chat/completions",
            data=data
        )

    async def forward_generate(self, prompt: str, **kwargs) -> Dict[str, Any]:
        """
        转发生成请求

        Args:
            prompt: 提示文本
            **kwargs: 其他参数

        Returns:
            Dict[str, Any]: 引擎响应
        """
        if not self._initialized:
            self.initialize()

        data = {
            "prompt": prompt,
            **kwargs
        }

        return await self.engine_client.forward_request(
            method="POST",
            path="/generate",
            data=data
        )

    async def health_check(self) -> bool:
        """
        检查引擎服务健康状态

        Returns:
            bool: 服务是否健康
        """
        if not self._initialized:
            return False

        return await self.engine_client.health_check()

    async def close(self):
        """关闭代理服务"""
        if self.engine_client:
            await self.engine_client.close()
        self._initialized = False
        logger.info("Proxy service closed")


# 全局代理服务实例
proxy_service = ProxyService()