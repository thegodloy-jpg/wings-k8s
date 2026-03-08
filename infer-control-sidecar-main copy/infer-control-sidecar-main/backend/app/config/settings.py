import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 共享卷路径
    SHARED_VOLUME_PATH: str = "/shared-volume"

    # 引擎配置
    ENGINE_TYPE: str = os.getenv("ENGINE_TYPE", "vllm")  # vllm 或 sglang
    ENGINE_HOST: str = "127.0.0.1"
    ENGINE_PORT: int = int(os.getenv("ENGINE_PORT", "8000"))

    # Wings-Infer服务端口
    WINGS_PORT: int = int(os.getenv("WINGS_PORT", "9000"))

    # 模型配置
    MODEL_NAME: str = os.getenv("MODEL_NAME", "meta-llama/Llama-2-7b-chat-hf")
    MODEL_PATH: str = os.getenv("MODEL_PATH", "/models")
    TP_SIZE: int = int(os.getenv("TP_SIZE", "1"))
    MAX_MODEL_LEN: int = int(os.getenv("MAX_MODEL_LEN", "4096"))

    # 健康检查配置
    HEALTH_CHECK_INTERVAL: int = 5  # 秒
    HEALTH_CHECK_TIMEOUT: int = 300  # 5分钟超时

    # 网络配置（从环境变量读取）
    SERVICE_CLUSTER_IP: str = os.getenv("SERVICE_CLUSTER_IP", "10.255.128.184")
    NODE_PORT: str = os.getenv("NODE_PORT", "30483")
    NODE_IP: str = os.getenv("NODE_IP", "90.90.161.168")

    # 加速配置
    ENABLE_ACCEL: bool = os.getenv("ENABLE_ACCEL", "false").lower() == "true"

    class Config:
        env_file = ".env"


settings = Settings()