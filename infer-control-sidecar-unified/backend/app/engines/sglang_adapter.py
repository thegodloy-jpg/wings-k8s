# =============================================================================
# 文件: engines/sglang_adapter.py
# 用途: SGLang 推理引擎适配器
# 状态: 活跃适配器
#
# 功能概述:
#   本模块负责将统一的参数字典转换为 SGLang 的启动命令。
#   SGLang 是一个高性能 LLM 推理引擎，支持 RadixAttention 等优化技术。
#
# 支持的部署模式:
#   - 单机模式:    直接运行 sglang.launch_server
#   - 分布式模式:  多节点分布式推理 (--nnodes, --node-rank, --dist-init-addr)
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
#   python3 -m sglang.launch_server \\
#       --model-path <model_path> \\
#       --host 0.0.0.0 \\
#       --port 17000 \\
#       --tp-size <tp_size> \\
#       [--nnodes <n> --node-rank <rank> --dist-init-addr <addr:port>]
#
# =============================================================================
# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
SGLang 引擎适配器。

在 sidecar launcher 模式下，本模块将统一参数转换为 SGLang 启动脚本。
与 vllm-engine 类似，将 start_command.sh 写入共享卷供引擎容器执行。

核心接口:
  - build_start_command(params) -> str : 生成 SGLang 启动命令
  - build_start_script(params)  -> str : 生成完整 bash 脚本（不含 shebang）
"""

import logging
import os
import re
import shlex
from typing import Dict, Any, List


def _sanitize_shell_path(path: str) -> str:
    """从文件路径中移除 shell 元字符，防止命令注入攻击。

    安全处理用户输入或环境变量中的路径，仅保留安全字符：
    - 字母 (a-z, A-Z)
    - 数字 (0-9)
    - 路径字符 (/)
    - 下划线 (_)、点 (.)、横线 (-)

    Args:
        path: 原始文件路径字符串

    Returns:
        str: 清理后的安全路径
    """
    return re.sub(r"[^a-zA-Z0-9/_.-]", "", path)


# 日志记录器
logger = logging.getLogger(__name__)

# 模块根目录：用于定位环境脚本文件
root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _build_base_env_commands(params: Dict[str, Any], root: str) -> List[str]:
    """构建 SGLang 基础环境设置命令。

    查找并加载项目中的 SGLang 环境设置脚本（如果存在）。
    脚本路径: <root>/wings/config/set_sglang_env.sh

    Args:
        params: 参数字典（当前未使用，保留为扩展点）
        root:   项目根目录路径

    Returns:
        List[str]: 环境设置命令列表，可能为空

    注意:
        - 脚本不存在时记录警告并返回空列表
        - 不会导致启动失败，仅影响特定特性
    """
    env_script = os.path.join(root, "wings", "config", "set_sglang_env.sh")
    if os.path.exists(env_script):
        return [f"source {env_script}"]
    logger.warning("SGLang env script not found at %s; starting without sourcing env script", env_script)
    return []


def _build_sglang_cmd_parts(params: Dict[str, Any]) -> str:
    """构建 SGLang 核心启动命令字符串。

    将 engine_config 字典转换为 sglang.launch_server CLI 参数格式：
    python3 -m sglang.launch_server --arg1 value1 --arg2 value2 ...

    参数转换规则:
    - 参数名: snake_case → kebab-case (如 tp_size → --tp-size)
    - 布尔值: True → 仅输出 flag (如 --disable-log-stats)
    - 布尔值: False → 跳过，不输出任何内容
    - 空字符串: 跳过，避免生成空参数（如 --model-path '')
    - JSON 字典: 用单引号包裹，确保 shell 正确解析
    - 其他值: 使用 shlex.quote 安全转义

    Args:
        params: 参数字典，必须包含 engine_config 子字典

    Returns:
        str: 完整的 SGLang 启动命令字符串

    示例输出:
        python3 -m sglang.launch_server \\
            --model-path /weights --host 0.0.0.0 --port 17000 \\
            --tp-size 4 --trust-remote-code
    """
    engine_config = params.get("engine_config", {})

    # python3  /usr/bin/python
    cmd_parts = ["python3", "-m", "sglang.launch_server"]

    for arg, value in engine_config.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            #  --model-path
            continue

        arg_name = f"--{arg.replace('_', '-')}"
        # 参数去重：跳过已存在的 CLI 参数，防止分布式参数与 engine_config 冲突
        if arg_name in cmd_parts:
            continue

        if isinstance(value, bool):
            if value:
                cmd_parts.append(arg_name)
        elif isinstance(value, str) and value.strip().startswith('{') and value.strip().endswith('}'):
            cmd_parts.extend([arg_name, f"'{value}'"])
        else:
            cmd_parts.extend([arg_name, shlex.quote(str(value))])

    return " ".join(cmd_parts)


def build_start_command(params: Dict[str, Any]) -> str:
    """为 launcher 生成 SGLang 启动命令字符串（不含 shebang 和环境设置）。

    此函数仅进行命令拼装，不启动任何子进程。

    单机模式:
        python3 -m sglang.launch_server --model-path ... --host ... --port ...

    分布式模式 (多节点, nnodes > 1):
        python3 -m sglang.launch_server ... \\
            --nnodes <n> --node-rank <rank> --dist-init-addr <addr:port>

    Args:
        params: 参数字典，包含以下关键字段:
            - engine_config: SGLang 启动参数
            - distributed:   是否分布式模式
            - nnodes:        总节点数
            - node_rank:     当前节点编号
            - head_node_addr: 主节点地址 (可包含端口号)

    Returns:
        str: SGLang 启动命令字符串

    环境变量:
        - SGLANG_DIST_PORT: 分布式通信端口，默认 28030
    """
    cmd = _build_sglang_cmd_parts(params)
    is_distributed = params.get("distributed", False)
    nnodes = params.get("nnodes", 1)
    node_rank = params.get("node_rank", 0)
    head_node_addr = params.get("head_node_addr", "127.0.0.1")

    if is_distributed and nnodes > 1:
        cmd += f" --nnodes {nnodes} --node-rank {node_rank}"
        if ":" in head_node_addr:
            cmd += f" --dist-init-addr {head_node_addr}"
        else:
            # dist_port: params 优先（config_loader 从 distributed_config.json 注入），其次环境变量
            sglang_dist_port = str(params.get("dist_port", os.getenv("SGLANG_DIST_PORT", "28030")))
            cmd += f" --dist-init-addr {head_node_addr}:{sglang_dist_port}"
        # 非 master 节点需要绑定所有地址（对齐 A）
        if node_rank != 0:
            cmd += " --host 0.0.0.0"

    return cmd


def build_start_script(params: Dict[str, Any]) -> str:
    """生成完整的 bash 启动脚本体（start_command.sh 内容，不含 shebang）。

    这是 SGLang 适配器的主要入口，生成的脚本结构：

        [source set_sglang_env.sh]   # 环境设置（可选）
        exec python3 -m sglang.launch_server --model-path ... --host ... --port ...

    使用 exec 确保引擎进程替换 shell 成为 PID 1，正确接收容器信号。

    Args:
        params: 参数字典，传递给 build_start_command()

    Returns:
        str: 完整的 bash 脚本体（以换行符结尾）
    """
    env_cmds = _build_base_env_commands(params, root_dir)
    core_cmd = build_start_command(params)

    lines: List[str] = []
    lines.extend(env_cmds)

    # 分布式通信环境变量（对齐 A：GLOO/TP/NCCL_SOCKET_IFNAME）
    is_distributed = params.get("distributed", False)
    nnodes = params.get("nnodes", 1)
    if is_distributed and nnodes > 1:
        net_if = os.getenv("NETWORK_INTERFACE", os.getenv("GLOO_SOCKET_IFNAME", "eth0"))
        lines.append(f"export GLOO_SOCKET_IFNAME={net_if}")
        lines.append(f"export TP_SOCKET_IFNAME={net_if}")
        lines.append(f"export NCCL_SOCKET_IFNAME={net_if}")

    lines.append(f"exec {core_cmd}")
    return "\n".join(lines) + "\n"


def start_engine(params: Dict[str, Any]):
    """旧版兼容接口（sidecar launcher 模式中已禁用）。

    在 sidecar 架构中，适配器不允许直接启动推理进程。
    应使用 build_start_script() 或 build_start_command()
    生成脚本并写入共享卷。

    Raises:
        RuntimeError: 始终抛出，阻止意外调用
    """
    raise RuntimeError(
        "start_engine is disabled in launcher mode. "
        "Use build_start_command() / build_start_script() and write to shared volume instead."
    )

