# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
SGLang 推理引擎适配器（sidecar launcher 模式）

在 sidecar launcher 模式下，此模块仅负责命令拼装，不直接拉起进程。
引擎进程由 vllm-engine（或 sglang-engine）容器读取 start_command.sh 后自行启动。

对外接口：
  - build_start_command(params) -> str   返回核心命令字符串
  - build_start_script(params)  -> str   返回完整 bash 脚本体（无 shebang）
"""

import logging
import os
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _build_base_env_commands(params: Dict[str, Any], root: str) -> List[str]:
    """读取 sglang 环境脚本（如容器内存在）。"""
    env_script = os.path.join(root, "wings", "config", "set_sglang_env.sh")
    if os.path.exists(env_script):
        return [f"source {env_script}"]
    return []


def _build_sglang_cmd_parts(params: Dict[str, Any]) -> str:
    """
    根据合并后的 engine_config 拼装 sglang.launch_server 命令行。

    参数转换规则：
    - key 中的下划线转连字符：tp_size -> --tp-size
    - bool True  -> 仅输出 flag（--disable-log-stats）
    - bool False -> 不输出
    - JSON 对象字符串 -> 用单引号包裹
    """
    engine_config = params.get("engine_config", {})

    # python3 保证在不含 /usr/bin/python 的镜像内也可用
    cmd_parts = ["python3", "-m", "sglang.launch_server"]

    for arg, value in engine_config.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            # 跳过空字符串，避免生成 --model-path 这样的残缺参数
            continue

        arg_name = f"--{arg.replace('_', '-')}"

        if isinstance(value, bool):
            if value:
                cmd_parts.append(arg_name)
        elif isinstance(value, str) and value.strip().startswith('{') and value.strip().endswith('}'):
            cmd_parts.extend([arg_name, f"'{value}'"])
        else:
            cmd_parts.extend([arg_name, str(value)])

    return " ".join(cmd_parts)


def build_start_command(params: Dict[str, Any]) -> str:
    """
    为 launcher 生成 SGLang 启动命令字符串（核心命令，不含 shebang/env setup）。

    注意：
        该函数只做命令拼装，不拉起任何子进程。
    """
    if params.get("distributed", False):
        raise ValueError("Launcher MVP does not support distributed mode for SGLang.")
    return _build_sglang_cmd_parts(params)


def build_start_script(params: Dict[str, Any]) -> str:
    """
    返回完整的 bash 脚本体（start_command.sh 内容，不含 shebang）。

    对于 sglang，脚本等价于：
        [env setup if applicable]
        exec python3 -m sglang.launch_server --model-path ... --host ... --port ...
    """
    env_cmds = _build_base_env_commands(params, root_dir)
    core_cmd = build_start_command(params)

    lines: List[str] = []
    lines.extend(env_cmds)
    lines.append(f"exec {core_cmd}")
    return "\n".join(lines) + "\n"


def start_engine(params: Dict[str, Any]):
    """
    兼容旧接口。

    sidecar launcher 模式下禁止由 adapter 直接拉起进程。
    """
    raise RuntimeError(
        "start_engine is disabled in launcher mode. "
        "Use build_start_command() / build_start_script() and write to shared volume instead."
    )
