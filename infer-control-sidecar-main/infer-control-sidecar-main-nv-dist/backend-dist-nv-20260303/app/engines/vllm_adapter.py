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
vLLM engine adapter.
In sidecar launcher mode, this module assembles commands only and does not launch processes.
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
    Start vLLM service in single-node mode.

    Args:
        params (Dict[str, Any]): Parameter dictionary.

    Returns:
        subprocess.Popen: The launched process object.
    Raises:
        Exception: If startup fails.
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

        # PID
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
    """Build base environment setup commands."""
    env_commands = []
    #  app/config/ ?wings/
    config_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config"
    )
    if engine == "vllm":
        local_script = os.path.join(config_dir, "set_vllm_env.sh")
        if os.path.exists(local_script):
            env_commands.append(f"source {local_script}")
    elif engine == "vllm_ascend":
        local_script = os.path.join(config_dir, "set_vllm_ascend_env.sh")
        if os.path.exists(local_script):
            env_commands.append(f"source {local_script}")
        else:
            env_commands.append("source /usr/local/Ascend/ascend-toolkit/set_env.sh")
            env_commands.append("source /usr/local/Ascend/nnal/atb/set_env.sh")
        if params.get("engine_config", {}).get("use_kunlun_atb"):
            env_commands.append(f"export USE_KUNLUN_ATB=1")
            logger.info("kunlun atb is used")
    return env_commands


def _build_cache_env_commands(engine: str) -> List[str]:
    """
    Build KVCache Offload related environment variable commands.

    Sets LD_LIBRARY_PATH for the appropriate engine to support KVCache Offload.

    Args:
        engine (str): Engine type, supports "vllm" and "vllm_ascend".

    Returns:
        List[str]: List of environment variable setup commands.
    """
    env_commands = []
    if not get_lmcache_env():
        return env_commands

    if engine == "vllm":
        #  kv_agent
        lib_path = "/opt/vllm_env/lib/python3.10/site-packages/kv_agent/lib"
        env_commands.append(f'export LD_LIBRARY_PATH="{lib_path}:$LD_LIBRARY_PATH"')
        logger.info(f"[KVCache Offload] Added LD_LIBRARY_PATH for vllm: {lib_path}")
    elif engine == "vllm_ascend":
        #  lmcache
        lib_path = "/opt/ascend_env/lib/python3.11/site-packages/lmcache"
        env_commands.append(f'export LD_LIBRARY_PATH="{lib_path}:$LD_LIBRARY_PATH"')
        logger.info(f"[KVCache Offload] Added LD_LIBRARY_PATH for vllm_ascend: {lib_path}")

    return env_commands


def _build_qat_env_commands(engine) -> List[str]:
    """
    Build KVCache QAT compression environment variable commands.

    Args:
        engine (str): Inference engine type.

    Returns:
        List[str]: List of QAT-related environment variable commands.
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
    """Build PD role environment variable commands."""
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
    """Build distributed environment configuration commands."""
    if params.get("distributed", False):
        pass  # raise ValueError("Distributed mode is disabled in sidecar launcher MVP.")
    return []


def _build_env_commands(params: Dict[str, Any], current_ip: str, network_interface: str, root: str) -> List[str]:
    """Build the full list of environment variable setup commands."""
    engine = params.get("engine")
    env_commands = []

    env_commands.extend(_build_base_env_commands(params, engine, root))
    env_commands.extend(_build_cache_env_commands(engine))
    env_commands.extend(_build_qat_env_commands(engine))
    env_commands.extend(_build_pd_role_env_commands(engine, current_ip, network_interface))
    env_commands.extend(_build_distributed_env_commands(params, current_ip, network_interface, engine))

    return env_commands


def _build_vllm_cmd_parts(params: Dict[str, Any]) -> str:
    """Build the vLLM command parts string."""
    engine_config = params.get("engine_config", {})
    # llm
    if "use_kunlun_atb" in engine_config:
        engine_config.pop("use_kunlun_atb")
    # if params.get("distributed"):
        # raise ValueError("Distributed mode is disabled in sidecar launcher MVP.")

    # vllm/vllm-openai image guarantees python3, while python may be absent.
    cmd_parts = ["python3", "-m", "vllm.entrypoints.openai.api_server"]

    for arg, value in engine_config.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            # Skip empty-string values to avoid generating broken args like:
            # --quantization  --gpu-memory-utilization ...
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
    Build complete vllm serve command string.

    Args:
        params: Server parameter dictionary.
    Returns:
        str: Complete vllm serve command string.
    """
    current_ip = get_local_ip()
    # Skip netifaces auto-detection to avoid dependency
    network_interface = os.getenv("NETWORK_INTERFACE", os.getenv("GLOO_SOCKET_IFNAME", "eth0"))

    # Build environment variable commands
    env_commands = _build_env_commands(
        params, current_ip, network_interface, root_dir
    )

    # Build main command
    command_str = _build_vllm_cmd_parts(params)

    # Combine full command
    if env_commands:
        return " && ".join(env_commands) + " && " + command_str
    return command_str


def build_start_command(params: Dict[str, Any]) -> str:
    """
    Generate vLLM startup command string for the launcher.
    Only performs command assembly, does not launch any subprocess.
    """
    if params.get("distributed", False):
        raise ValueError("Launcher MVP does not support distributed mode for vLLM.")
    return _build_vllm_cmd_parts(params)


def build_start_script(params: Dict[str, Any]) -> str:
    """
    Return the complete bash script body (start_command.sh content, without shebang).
    - vllm:        exec python3 -m vllm.entrypoints.openai.api_server ...
    - vllm_ascend: source Ascend CANN/ATB env vars, then exec python3 ...
    """
    engine = params.get("engine", "vllm")
    cmd = _build_vllm_cmd_parts(params)
    is_distributed = params.get("distributed", False)
    node_rank = params.get("node_rank", 0)
    nnodes = params.get("nnodes", 1)
    backend = params.get("distributed_executor_backend", "ray")
    head_addr = params.get("head_node_addr", "infer-0.infer-hl")

    if is_distributed and nnodes > 1:
        script_parts = []
        if backend == "ray":
            if node_rank == 0:
                script_parts.append("export VLLM_HOST_IP=$(hostname -i)")
                script_parts.append("export NCCL_SOCKET_IFNAME=eth0")
                script_parts.append("export GLOO_SOCKET_IFNAME=eth0\n")
                script_parts.append("ray start --head --port=6379 --num-gpus=1 --dashboard-host=0.0.0.0\n")
                script_parts.append("for i in $(seq 1 60); do")
                script_parts.append("  COUNT=$(python3 -c \"import ray; ray.init(address='auto',ignore_reinit_error=True); print(len([n for n in ray.nodes() if n['alive']])); ray.shutdown()\" 2>/dev/null || echo 0)")
                script_parts.append("  [ \"$COUNT\" -ge \"2\" ] && break")
                script_parts.append("  sleep 5")
                script_parts.append("done\n")
                script_parts.append(f"exec {cmd} --tensor-parallel-size {nnodes} --distributed-executor-backend ray")
            else:
                script_parts.append("export NCCL_SOCKET_IFNAME=eth0")
                script_parts.append("export GLOO_SOCKET_IFNAME=eth0\n")
                script_parts.append("for i in $(seq 1 60); do")
                script_parts.append(f"  python3 -c \"import socket; s=socket.socket(); s.settimeout(2); s.connect(('{head_addr}',6379)); s.close()\" 2>/dev/null && break")
                script_parts.append("  sleep 5")
                script_parts.append("done\n")
                script_parts.append(f"exec ray start --address={head_addr}:6379 --num-gpus=1 --block")
        else: # dp_deployment
            if node_rank == 0:
                script_parts.append(f"exec {cmd} --data-parallel-address {head_addr} --data-parallel-rpc-port 13355 --data-parallel-size {nnodes} --data-parallel-size-local 1 --data-parallel-external-lb --data-parallel-rank 0")
            else:
                script_parts.append(f"exec {cmd} --data-parallel-address {head_addr} --data-parallel-rpc-port 13355 --data-parallel-size {nnodes} --data-parallel-size-local 1 --data-parallel-external-lb --headless --data-parallel-rank {node_rank}")
        return "\n".join(script_parts) + "\n"

    if engine == "vllm_ascend":
        # start_command.sh is generated by wings-infer but executed inside vllm-ascend engine container.
        # The engine container does not include wings-infer's /app/app/config/ path.
        # Must use inline image-internal standard paths, not source files from wings-infer container.
        # vllm-ascend official image pre-installs CANN toolkit at fixed paths:
        #   /usr/local/Ascend/ascend-toolkit/set_env.sh
        #   /usr/local/Ascend/nnal/atb/set_env.sh
        logger.info("vllm_ascend: inline Ascend CANN env setup in start_command.sh")
        # NOTE: k3s embedded env device selection:
        # - ASCEND_VISIBLE_DEVICES requires Ascend Docker Runtime hook; no-op inside k3s
        # - vllm TP=1 hardcodes local_rank=0 -> torch.npu.set_device(0), unaffected by ASCEND_DEVICE_ID
        # - In verification env use NPU 0 (physical card 0), no device switching
        # - To specify specific NPU, need Ascend Device Plugin for k8s or restructure deployment
        env_block = (
            "# set +u: nnal/atb/set_env.sh references ZSH_VERSION without default\n"
            "set +u\n"
            "[ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ] "
            "&& source /usr/local/Ascend/ascend-toolkit/set_env.sh "
            "|| echo 'WARN: ascend-toolkit/set_env.sh not found'\n"
            "[ -f /usr/local/Ascend/nnal/atb/set_env.sh ] "
            "&& source /usr/local/Ascend/nnal/atb/set_env.sh "
            "|| echo 'WARN: nnal/atb/set_env.sh not found'\n"
            "set -u\n"
        )
        return env_block + f"exec {cmd}\n"

    return f"exec {cmd}\n"


def _start_vllm_api_server(params: Dict) -> subprocess.Popen:
    full_command = _build_vllm_command(params)
    logger.info("........ Starting vLLM service ........")

    # subprocess
    process = subprocess.Popen(
        ["/bin/bash", "-c", full_command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    #
    wait_for_process_startup(
        process=process,
        success_message="Application startup complete",
        _logger=logger
    )

    #
    log_stream(process)

    return process


def start_vllm_distributed(params: Dict):
    """Distributed mode entry point (not supported in sidecar MVP)."""
    raise RuntimeError("Distributed mode is disabled in sidecar launcher MVP.")


def start_engine(params: Dict[str, Any]):
    """
    Legacy compatibility interface.
    In sidecar launcher mode, direct process launch from adapter is prohibited.
    """
    raise RuntimeError(
        "start_engine is disabled in launcher mode. "
        "Use build_start_command() and write it to shared volume instead."
    )

