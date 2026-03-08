# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: proxy/__init__.py
# Purpose: Proxy package export surface reused from wings implementation.
# Status: Active reused module.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Keep export names stable for uvicorn app imports.
# - Avoid startup side effects at import time.
# -----------------------------------------------------------------------------
__all__ = [
    "gateway", "http_client", "queueing",
    "settings", "tags", "warmup"
]