# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
SGLang 推理引擎适配器

负责启动 SGLang 推理服务。
支持分布式推理架构。
实现方式依赖于 SGLang 提供的 Python API。
"""

import logging
import os
import subprocess
from typing import Dict, Any
from wings.utils.process_utils import log_process_pid, wait_for_process_startup, log_stream
from wings.engines.vllm_adapter import detect_network_interface
from wings.utils.env_utils import get_master_ip, get_local_ip

logger = logging.getLogger(__name__)
root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _build_sglang_command(params: Dict[str, Any]) -> str:
    """
    根据参数构建启动 SGLang 服务器的命令行字符串。

    Args:
        params (Dict[str, Any]): 合并后的参数字典。

    Returns:
        str: 构建好的命令行字符串
    """
    # 基本命令
    engine_config = params.get("engine_config")

    # 构建环境变量设置命令
    env_commands = [f"source {root_dir}/wings/config/set_sglang_env.sh"]

    # 构建vllm命令部分
    cmd_parts = ["python", "-m", "sglang.launch_server"]
    
    if params.get("distributed"):
        nodes = params["nodes"].split(',')
        dist_port = params['dist_port']
        master_ip = get_master_ip()
        current_ip = get_local_ip()
        network_interface = detect_network_interface(current_ip)
        dist_env_commands = [
            f"export GLOO_SOCKET_IFNAME={network_interface}",
            f"export TP_SOCKET_IFNAME={network_interface}",
            f"export NCCL_SOCKET_IFNAME={network_interface}"
        ]
        env_commands += dist_env_commands

        nnodes = len(nodes)
        node_rank = nodes.index(current_ip)

        cmd_parts.extend(["--nnodes", str(nnodes)])
        cmd_parts.extend(["--node-rank", str(node_rank)])
        cmd_parts.extend(["--dist-init-addr", f"{master_ip}:{dist_port}"])
        if current_ip != master_ip:
            cmd_parts.extend(["--host", f"0.0.0.0"])

    # 添加引擎参数
    for arg, value in engine_config.items():
        if value is None:
            continue
        
        # 将参数名转换为命令行格式 (--param-name)
        arg_name = f"--{arg.replace('_', '-')}"
        if arg_name in cmd_parts:
            continue
        if isinstance(value, bool):
            if value:
                cmd_parts.append(arg_name)
        elif isinstance(value, str) and value.strip().startswith('{') and value.strip().endswith('}'):  # 处理json字符串类型参数
            cmd_parts.extend([arg_name, f"'{value}'"])
        else:
            cmd_parts.extend([arg_name, str(value)])
    
    # 组合完整命令
    command_str = " ".join(cmd_parts)
    full_command = " && ".join(env_commands) + " && " + command_str

    return full_command


def start_sglang_distributed(params: Dict):
    """SGlang分布式模式启动入口"""
    logger.info("Starting SGlang distributed mode...")
    # 解析节点列表

    full_command = _build_sglang_command(params)
    logger.info("........ Starting SGlang service ........")

    # 使用subprocess运行命令
    process = subprocess.Popen(
        ["/bin/bash", "-c", full_command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    log_process_pid(
        name="salang_distributed",
        parent_pid=os.getpid(),
        child_pid=process.pid
    )

    # 使用公共函数等待启动成功
    wait_for_process_startup(
        process=process,
        success_message="Application startup complete",
        _logger=logger
    )

    # 启动独立线程持续输出日志
    log_stream(process)


def start_engine(params: Dict[str, Any]):
    """
    启动 SGLang 推理服务的入口函数。

    当前实现优先使用命令行工具 `sglang.launch_server` 启动。

    Args:
        params (Dict[str, Any]): 合并后的参数字典。
    """
    logger.info("SGLang adapter: Preparing to start SGLang service...")
    try:
        if params.get("distributed", False):
            start_sglang_distributed(params)
        else:
            # 优先尝试构建命令行
            command_list = _build_sglang_command(params)

            if command_list is None:
                logger.error("Failed to build SGLang startup command, missing key parameters.")
                raise ValueError("Failed to build SGLang startup command, please check configuration.")

            logger.info("........ Starting sglang service ........")
            # 使用 subprocess 启动服务
            process = subprocess.Popen(
                ["/bin/bash", "-c", command_list],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # 行缓冲
                universal_newlines=True
            )
            logger.info(f"SGLang service process started (PID: {process.pid})")
            log_process_pid(
                name="salang",
                parent_pid=os.getpid(),
                child_pid=process.pid
            )
            # 使用公共函数等待启动成功
            wait_for_process_startup(
                process=process,
                success_message="Application startup complete",
                _logger=logger
            )

            # 启动独立线程持续输出日志
            log_stream(process)
    except Exception as e:
        logger.error(f"Error starting SGLang service process: {e}", exc_info=True)
        raise
    return True