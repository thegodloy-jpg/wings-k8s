from typing import Dict, Any
from app.config.settings import settings


class CommandBuilder:
    """引擎命令构建器"""

    @staticmethod
    def build_vllm_command() -> str:
        """
        构建vLLM启动命令

        Returns:
            str: vLLM启动命令
        """
        # 如果启用加速，添加环境变量
        env_vars = ""
        if settings.ENABLE_ACCEL:
            env_vars = f'export WINGS_ENGINE_PATCH_OPTIONS=\'{{"vllm": ["test_patch"]}}\' && '

        # 新版 vLLM 使用 --model 接收模型路径
        command = (
            f"{env_vars}"
            f"python3 -m vllm.entrypoints.openai.api_server "
            f"--model {settings.MODEL_PATH} "
            f"--host {settings.ENGINE_HOST} "
            f"--port {settings.ENGINE_PORT} "
            f"--tensor-parallel-size {settings.TP_SIZE} "
            f"--max-model-len {settings.MAX_MODEL_LEN} "
            f"--trust-remote-code --max-num-seqs 32"
        )
        return command

    @staticmethod
    def build_sglang_command() -> str:
        """
        构建SGLang启动命令

        Returns:
            str: SGLang启动命令
        """
        # 如果启用加速，添加环境变量
        env_vars = ""
        if settings.ENABLE_ACCEL:
            env_vars = f'export WINGS_ENGINE_PATCH_OPTIONS=\'{{"sglang": ["test_patch"]}}\' && '

        command = (
            f"{env_vars}"
            f"python3 -m sglang.launch_server "
            f"--model-path {settings.MODEL_PATH} "
            f"--host {settings.ENGINE_HOST} "
            f"--port {settings.ENGINE_PORT} "
            f"--tp {settings.TP_SIZE} "
            f"--context-length {settings.MAX_MODEL_LEN} "
            f"--trust-remote-code --max-num-seqs 32"
        )
        return command

    @classmethod
    def build_command(cls, engine_type: str = None) -> str:
        """
        根据引擎类型构建启动命令

        Args:
            engine_type: 引擎类型（vllm或sglang），默认使用配置

        Returns:
            str: 启动命令
        """
        engine_type = engine_type or settings.ENGINE_TYPE

        if engine_type.lower() == "vllm":
            return cls.build_vllm_command()
        elif engine_type.lower() == "sglang":
            return cls.build_sglang_command()
        else:
            raise ValueError(f"Unsupported engine type: {engine_type}")

    @staticmethod
    def build_status_command(status: str) -> str:
        """
        构建状态写入命令

        Args:
            status: 状态信息

        Returns:
            str: 状态命令
        """
        return status
