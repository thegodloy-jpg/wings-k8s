# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: engines/vllm_adapter.py
# Purpose: vLLM adapter responsible for assembling engine startup command arguments and env fragments.
# Status: Active adapter with process-start API intentionally disabled in launcher mode.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - build_start_command is the only supported launcher-facing entrypoint.
# - Do not reintroduce direct process launch in sidecar launcher path.
# -----------------------------------------------------------------------------
# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
VLLM 推理引擎适配器

在 sidecar launcher 模式下，此模块仅负责命令拼装，不直接拉起进程。
"""

import logging
import os
from typing import Dict, Any, List
import subprocess

from app.utils.env_utils import get_local_ip, get_lmcache_env, \
    get_pd_role_env, get_qat_env
from app.utils.process_utils import log_process_pid, wait_for_process_startup, log_stream

logger = logging.getLogger(__name__)
root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _start_vllm_single(params: Dict[str, Any]) -> subprocess.Popen:
    """
    启动单机模式 vLLM 服务
    
    Args:
        params (Dict[str, Any]): 参数字典
        
    Returns:
        subprocess.Popen: 启动的进程对象
        
    Raises:
        Exception: 如果启动过程中发生错误
    """
    try:
        cmd = _build_vllm_command(params)

        process = subprocess.Popen(
            ["/bin/bash", "-c", cmd],
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        # 记录进程PID
        log_process_pid(
            name="vllm",
            parent_pid=os.getpid(),
            child_pid=process.pid
        )
        
        return process
        
    except Exception as e:
        logger.error(f"Error starting VLLM service: {e}", exc_info=True)
        raise


def _build_base_env_commands(params, engine: str, root: str) -> List[str]:
    """构建基础环境设置命令"""
    env_commands = []
    if engine == "vllm":
        env_commands.append(f"source {root}/wings/config/set_vllm_env.sh")
    elif engine == "vllm_ascend":
        env_commands.append(f"source {root}/wings/config/set_vllm_ascend_env.sh")
        if params.get("engine_config", {}).get("use_kunlun_atb"):
            env_commands.append(f"export USE_KUNLUN_ATB=1")
            logger.info("kunlun atb is used")
    return env_commands


def _build_cache_env_commands(engine: str) -> List[str]:
    """构建 KVCache Offload 相关环境变量命令
    
    根据不同的推理引擎类型，设置相应的库路径到 LD_LIBRARY_PATH 环境变量中，
    以支持 KVCache Offload 功能的正常运行。
    
    Args:
        engine (str): 推理引擎类型，支持 "vllm" 和 "vllm_ascend"
        
    Returns:
        List[str]: 包含环境变量设置命令的列表
    """
    env_commands = []
    if not get_lmcache_env():
        return env_commands
    
    if engine == "vllm":
        # 获取 kv_agent 模块的库路径
        lib_path = "/opt/vllm_env/lib/python3.10/site-packages/kv_agent/lib"
        env_commands.append(f'export LD_LIBRARY_PATH="{lib_path}:$LD_LIBRARY_PATH"')
        logger.info(f"[KVCache Offload] Added LD_LIBRARY_PATH for vllm: {lib_path}")
    elif engine == "vllm_ascend":
        # 获取 lmcache 模块的库路径
        lib_path = "/opt/ascend_env/lib/python3.11/site-packages/lmcache"
        env_commands.append(f'export LD_LIBRARY_PATH="{lib_path}:$LD_LIBRARY_PATH"')
        logger.info(f"[KVCache Offload] Added LD_LIBRARY_PATH for vllm_ascend: {lib_path}")
    
    return env_commands


def _build_qat_env_commands(engine) -> List[str]:
    """
    构建KVCache QAT 压缩环境变量命令
    
    Args:
        engine (str): 推理引擎类型
        
    Returns:
        List[str]: QAT相关的环境变量导出命令列表
    """
    env_commands = []
    if not get_qat_env():
        return env_commands

    if engine == "vllm":
        env_commands.append('export LMCACHE_QAT_ENABLED=True')
    else:
        env_commands.append('export LMCACHE_QAT_ENABLED=False')
        logger.warning(f"[KVCache Offload] QAT compression feature is not supported by the current engine {engine}, "
                       "it has been automatically disabled")
    return env_commands


def _build_pd_role_env_commands(engine: str, current_ip: str, network_interface: str) -> List[str]:
    """构建PD角色环境变量命令"""
    env_commands = []
    if get_pd_role_env():
        if engine == "vllm":
            env_commands.append(f'export VLLM_NIXL_SIDE_CHANNEL_HOST={current_ip}')
        elif engine == "vllm_ascend":
            rpc_port = os.getenv('VLLM_LLMDD_RPC_PORT', "5569")
            env_commands.extend([
                f"source /usr/local/Ascend/ascend-toolkit/set_env.sh",
                f"source /usr/local/Ascend/nnal/atb/set_env.sh",
                f"export HCCL_IF_IP={current_ip}",
                f"export GLOO_SOCKET_IFNAME={network_interface}",
                f"export TP_SOCKET_IFNAME={network_interface}",
                f"export HCCL_SOCKET_IFNAME={network_interface}",
                f"export OMP_PROC_BIND=false",
                f"export OMP_NUM_THREADS=100",
                f"export VLLM_USE_V1=1",
                f"export LCCL_DETERMINISTIC=1",
                f"export HCCL_DETERMINISTIC=true",
                f"export CLOSE_MATMUL_K_SHIFT=1",
                f"export VLLM_LLMDD_RPC_PORT={rpc_port}",
                "export PYTORCH_NPU_ALLOC_CONF=max_split_size_mb:256"
            ])
    return env_commands


def _build_distributed_env_commands(params: Dict[str, Any], current_ip: str, 
                                    network_interface: str, engine: str) -> List[str]:
    """构建分布式环境配置命令"""
    if params.get("distributed", False):
        raise ValueError("Distributed mode is disabled in sidecar launcher MVP.")
    return []


def _build_env_commands(params: Dict[str, Any], current_ip: str, network_interface: str, root: str) -> List[str]:
    """构建环境变量设置命令列表"""
    engine = params.get("engine")
    env_commands = []
    
    # 依次调用各个模块的函数
    env_commands.extend(_build_base_env_commands(params, engine, root))
    env_commands.extend(_build_cache_env_commands(engine))
    env_commands.extend(_build_qat_env_commands(engine))
    env_commands.extend(_build_pd_role_env_commands(engine, current_ip, network_interface))
    env_commands.extend(_build_distributed_env_commands(params, current_ip, network_interface, engine))
    
    return env_commands


def _build_vllm_cmd_parts(params: Dict[str, Any]) -> str:
    """构建 vllm 命令部分"""
    engine_config = params.get("engine_config", {})
    # 删除自定义的非vllm参数
    if "use_kunlun_atb" in engine_config:
        engine_config.pop("use_kunlun_atb")
    if params.get("distributed"):
        raise ValueError("Distributed mode is disabled in sidecar launcher MVP.")

    # vllm/vllm-openai image guarantees python3, while `python` may be absent.
    cmd_parts = ["python3", "-m", "vllm.entrypoints.openai.api_server"]
    
    for arg, value in engine_config.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            # Skip empty-string values to avoid generating broken args like:
            # `--quantization  --gpu-memory-utilization ...`
            continue
        if arg == "max_num_batched_tokens":
            try:
                if int(value) <= 0:
                    logger.warning(
                        "Skip invalid max_num_batched_tokens=%s; vLLM requires >=1",
                        value,
                    )
                    continue
            except (TypeError, ValueError):
                logger.warning(
                    "Skip non-integer max_num_batched_tokens=%s",
                    value,
                )
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


def _build_vllm_command(params: Dict[str, Any]) -> str:
    """
    构建 vllm serve 命令行

    Args:
        params: 服务器参数

    Returns:
        str: 完整的 vllm serve 命令
    """
    current_ip = get_local_ip()
    # 单机场景下不做网卡自动探测，避免 netifaces 依赖
    network_interface = os.getenv("NETWORK_INTERFACE", os.getenv("GLOO_SOCKET_IFNAME", "eth0"))
    
    # 构建环境变量命令
    env_commands = _build_env_commands(
        params, current_ip, network_interface, root_dir
    )
    
    # 构建主命令
    command_str = _build_vllm_cmd_parts(params)
    
    # 组合完整命令
    if env_commands:
        return " && ".join(env_commands) + " && " + command_str
    return command_str


def build_start_command(params: Dict[str, Any]) -> str:
    """
    为 launcher 生成 vLLM 启动命令字符串。

    注意:
        该函数只做命令拼装，不拉起任何子进程。
    """
    if params.get("distributed", False):
        raise ValueError("Launcher MVP does not support distributed mode for vLLM.")
    return _build_vllm_cmd_parts(params)


def _start_vllm_api_server(params: Dict) -> subprocess.Popen:
    full_command = _build_vllm_command(params)
    logger.info("........ Starting vLLM service ........")

    # 使用subprocess运行命令
    process = subprocess.Popen(
        ["/bin/bash", "-c", full_command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    # 使用公共函数等待启动成功
    wait_for_process_startup(
        process=process,
        success_message="Application startup complete",
        _logger=logger
    )
    
    # 启动独立线程持续输出日志
    log_stream(process)

    return process


def start_vllm_distributed(params: Dict):
    """分布式模式入口（sidecar MVP 不支持）。"""
    raise RuntimeError("Distributed mode is disabled in sidecar launcher MVP.")


def start_engine(params: Dict[str, Any]):
    """
    兼容旧接口。

    sidecar launcher 模式下禁止由 adapter 直接拉起进程。
    """
    raise RuntimeError(
        "start_engine is disabled in launcher mode. "
        "Use build_start_command() and write it to shared volume instead."
    )

