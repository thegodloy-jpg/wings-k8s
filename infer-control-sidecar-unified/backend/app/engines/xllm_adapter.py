# =============================================================================
# 文件: engines/xllm_adapter.py
# 用途: XLLM 推理引擎适配器（华为昇腾自研引擎）
# 状态: 活跃适配器
#
# 功能概述:
#   本模块负责将统一参数转换为 xllm 引擎的启动命令。
#   XLLM 是华为昇腾原生推理引擎，支持多节点顺序启动。
#
# 支持的部署模式:
#   - 单机模式:    单节点运行
#   - 多节点模式:  顺序启动多个 xllm 实例
#
# 核心接口:
#   - build_start_script(params) : 返回完整 bash 脚本（推荐，含环境设置）
#   - build_start_command(params): 返回核心启动命令（兼容旧版）
#   - start_engine(params)       : 已禁用，sidecar 模式不允许直接启动进程
#
# Sidecar 架构契约:
#   - 仅负责命令拼装，不启动任何子进程
#   - 生成的脚本写入共享卷，由 engine 容器执行
#
# 引擎启动命令格式:
#   /usr/local/python3.11.13/lib/python3.11/site-packages/xllm/xllm \
#       --model <model_path> \
#       --port <port> \
#       --devices npu:<device_id> \
#       --master_node_addr <addr> \
#       --nnodes <n> --node_rank <rank>
#
# =============================================================================
# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
XLLM 引擎适配器。

在 sidecar launcher 模式下，将统一参数转换为 xllm 启动脚本。
"""

import json
import logging
import os
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# 模块根目录
root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# XLLM 二进制路径
XLLM_BINARY = "/usr/local/python3.11.13/lib/python3.11/site-packages/xllm/xllm"


def _sanitize_shell_path(path: str) -> str:
    """从文件路径中移除 shell 元字符。"""
    return re.sub(r"[^a-zA-Z0-9/_.-]", "", path)


def _build_base_env_commands(params: Dict[str, Any], root: str) -> List[str]:
    """构建 XLLM 基础环境设置命令。"""
    env_script = os.path.join(root, "wings", "config", "set_xllm_env.sh")
    if os.path.exists(env_script):
        return [f"source {env_script}"]
    logger.warning("XLLM env script not found at %s; starting without sourcing env script", env_script)
    return []


def _build_xllm_command(params: Dict[str, Any], node_rank: int = 0) -> str:
    """构建单个 xllm 节点的启动命令。

    Args:
        params: 参数字典
        node_rank: 节点排名，默认 0

    Returns:
        str: xllm 启动命令
    """
    engine_config = params.get("engine_config", {}) or {}
    nnodes = engine_config.get("nnodes", 1)
    start_port = engine_config.get("port", params.get("port", 17000))
    port = start_port + node_rank
    device = node_rank

    parts = [
        XLLM_BINARY,
        "--model", str(engine_config.get("model", params.get("model_path", "/weights"))),
        "--port", str(port),
        "--devices", f"npu:{device}",
        "--master_node_addr", str(engine_config.get("master_node_addr", "127.0.0.1")),
        "--nnodes", str(nnodes),
        "--node_rank", str(node_rank),
    ]

    # 添加引擎参数（跳过已处理的参数）
    skip_keys = {"model", "port", "master_node_addr", "nnodes", "node_rank",
                 "type", "host", "device", "devices"}
    for arg, value in engine_config.items():
        if value is None or arg in skip_keys:
            continue
        if isinstance(value, bool):
            if value:
                parts.append(f"--{arg}")
        elif isinstance(value, str) and value.strip().startswith('{') and value.strip().endswith('}'):
            parts.extend([f"--{arg}", f"'{value}'"])
        else:
            parts.extend([f"--{arg}", str(value)])

    return " ".join(parts)


def build_start_command(params: Dict[str, Any]) -> str:
    """生成 XLLM 的核心启动命令。

    Args:
        params: 参数字典

    Returns:
        str: 启动命令字符串
    """
    return _build_xllm_command(params, node_rank=0)


def build_start_script(params: Dict[str, Any]) -> str:
    """生成完整的 bash 启动脚本（start_command.sh 内容，不含 shebang）。

    多节点场景下，脚本会顺序启动所有节点：
    - 节点 0: 使用 exec 替换 shell
    - 节点 1+: 后台启动（&），然后 exec 节点 0

    Args:
        params: 参数字典

    Returns:
        str: 完整的 bash 脚本体（以换行符结尾）
    """
    engine_config = params.get("engine_config", {}) or {}
    nnodes = engine_config.get("nnodes", 1)

    env_cmds = _build_base_env_commands(params, root_dir)
    lines: List[str] = list(env_cmds)

    if nnodes <= 1:
        # 单节点：直接 exec
        cmd = _build_xllm_command(params, node_rank=0)
        lines.append(f"exec {cmd}")
    else:
        # 多节点：后台启动 node_rank > 0，最后 exec node_rank 0
        for rank in range(nnodes - 1, 0, -1):
            cmd = _build_xllm_command(params, node_rank=rank)
            lines.append(f"{cmd} &")
            lines.append("sleep 0.5")
        cmd0 = _build_xllm_command(params, node_rank=0)
        lines.append(f"exec {cmd0}")

    return "\n".join(lines) + "\n"


def start_engine(params: Dict[str, Any]):
    """旧版兼容接口（sidecar launcher 模式中已禁用）。

    Raises:
        RuntimeError: 始终抛出
    """
    raise RuntimeError(
        "start_engine is disabled in launcher mode. "
        "Use build_start_command() / build_start_script() and write to shared volume instead."
    )
