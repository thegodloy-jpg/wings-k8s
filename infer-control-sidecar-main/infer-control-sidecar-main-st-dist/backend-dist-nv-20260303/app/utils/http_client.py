# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: utils/http_client.py
# Purpose: Generic HTTP client helpers used by legacy service modules.
# Status: Legacy-compatible utility module.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Keep request timeout/error handling predictable.
# - Avoid introducing global side effects.
# -----------------------------------------------------------------------------
import httpx
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class HTTPClient:
    """HTTP"""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip('/')
        self.client = httpx.AsyncClient(timeout=300.0)

    async def close(self):
        """(no description)"""
        await self.client.aclose()

    async def health_check(self) -> bool:
        """


        Returns:
            bool:
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


        Args:
            method: HTTP
            path:
            data:
            headers:

        Returns:
            Dict[str, Any]:
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