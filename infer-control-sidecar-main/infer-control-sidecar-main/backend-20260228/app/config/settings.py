# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: config/settings.py
# Purpose: Environment-backed settings model shared by launcher, proxy, and health services.
# Status: Active configuration source.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Keep port/env defaults aligned with sidecar design docs.
# - Changes here affect launcher/proxy/health startup contracts.
# -----------------------------------------------------------------------------
import os
from pydantic_settings import BaseSettings


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseSettings):
    # Shared volume / launcher outputs
    SHARED_VOLUME_PATH: str = os.getenv("SHARED_VOLUME_PATH", "/shared-volume")
    START_COMMAND_FILENAME: str = os.getenv("START_COMMAND_FILENAME", "start_command.sh")

    # Engine / mode
    ENGINE_TYPE: str = os.getenv("ENGINE_TYPE", "vllm")
    ENGINE_HOST: str = os.getenv("ENGINE_HOST", "127.0.0.1")
    ENGINE_PORT: int = int(os.getenv("ENGINE_PORT", "17000"))
    ENABLE_REASON_PROXY: bool = _env_bool("ENABLE_REASON_PROXY", True)

    # Launcher + child service ports
    PORT: int = int(os.getenv("PORT", "18000"))
    HEALTH_PORT: int = int(os.getenv("HEALTH_PORT", "19000"))
    WINGS_PORT: int = int(os.getenv("WINGS_PORT", "9000"))  # legacy field

    # Process launch
    PYTHON_BIN: str = os.getenv("PYTHON_BIN", "python")
    UVICORN_MODULE: str = os.getenv("UVICORN_MODULE", "uvicorn")
    PROXY_APP: str = os.getenv("PROXY_APP", "app.proxy.gateway:app")
    HEALTH_APP: str = os.getenv("HEALTH_APP", "app.proxy.health_service:app")
    PROCESS_POLL_SEC: float = float(os.getenv("PROCESS_POLL_SEC", "1.0"))

    # Model defaults aligned with wings_start semantics
    MODEL_NAME: str = os.getenv("MODEL_NAME", "")
    MODEL_PATH: str = os.getenv("MODEL_PATH", "/weights")
    SAVE_PATH: str = os.getenv("SAVE_PATH", "/opt/wings/outputs")
    TP_SIZE: int = int(os.getenv("TP_SIZE", "1"))
    MAX_MODEL_LEN: int = int(os.getenv("MAX_MODEL_LEN", "4096"))

    # Legacy / existing fields
    HEALTH_CHECK_INTERVAL: int = 5
    HEALTH_CHECK_TIMEOUT: int = 300
    SERVICE_CLUSTER_IP: str = os.getenv("SERVICE_CLUSTER_IP", "10.255.128.184")
    NODE_PORT: str = os.getenv("NODE_PORT", "30483")
    NODE_IP: str = os.getenv("NODE_IP", "90.90.161.168")
    ENABLE_ACCEL: bool = _env_bool("ENABLE_ACCEL", False)

    class Config:
        env_file = ".env"


settings = Settings()
