# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025.
# -*- coding: utf-8 -*-

"""
Wings 引擎适配器（HunyuanVideo / Transformers LLM 服务）

对齐 vllm_adapter 的结构与 start_engine 调度风格（"统一入口 + 等待 + 日志转发"）：
- 当 engine=='wings' 且 model_type=='mmgm' ：启动 HunyuanVideo（多模态）服务（支持单卡和多卡）
    启动模块：python -m wings.servers.model.hunyuanvideo_server
    透传参数：--server-host/--server-port/--model-base/--save-path/--device/...
- 当 engine=='wings' 且 model_type=='llm'  ：启动 Transformers 文本 LLM 服务（仅支持单卡）
    启动模块：python -m wings.servers.transformers_server
    透传参数：--host/--port/--model-name/--model-path/--device

环境脚本（不强依赖；缺失仅告警，避免强耦合失败）：
- NVIDIA：{repo_root}/wings/config/set_wings_nvidia_env.sh
- Ascend：{repo_root}/wings/config/set_wings_ascend_env.sh
"""

import logging
import os
import shlex
import subprocess
from typing import Dict, Any, List, Optional, Tuple

from wings.utils.process_utils import log_process_pid, wait_for_process_startup, log_stream

logger = logging.getLogger(__name__)
root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .../wings


# ──────────────────────────────────────────────────────────────────────────────
# 判定函数（不同服务的选择逻辑，保持简单清晰）
# ──────────────────────────────────────────────────────────────────────────────
def _is_wings_mmgm(params: Dict[str, Any]) -> bool:
    """是否应启动 HunyuanVideo（多模态）服务。"""
    return (params.get("engine", "").strip().lower() == "wings"
            and params.get("model_type", "").strip().lower() == "mmgm")


def _is_wings_llm(params: Dict[str, Any]) -> bool:
    """是否应启动 Transformers（文本 LLM）服务。"""
    return (params.get("engine", "").strip().lower() == "wings"
            and params.get("model_type", "").strip().lower() == "llm")


# ──────────────────────────────────────────────────────────────────────────────
# 公共：环境脚本与通用参数拼装（对齐 vllm_adapter 的分层风格）
# ──────────────────────────────────────────────────────────────────────────────
def _env_script_paths(repo_root: str) -> Dict[str, str]:
    """返回 nvidia/ascend 两类环境脚本的绝对路径。"""
    return {
        "nvidia": os.path.join(repo_root, "wings", "config", "set_wings_nvidia_env.sh"),
        "ascend": os.path.join(repo_root, "wings", "config", "set_wings_ascend_env.sh"),
        # 新增：vLLM/Transformers 在 Ascend 的专用环境脚本（与原两个同目录）
        "vllm_ascend": os.path.join(repo_root, "wings", "config", "set_vllm_ascend_env.sh"),
    }


def _build_base_env_commands(params: Dict[str, Any], repo_root: str) -> List[str]:
    """
    构建基础环境脚本 source 命令：
    - 按 params['device'] 显式选择 nvidia/ascend；
    - 缺失或找不到时降级尝试；都没有则仅告警不阻断。
    """
    env_commands: List[str] = []
    engine_config = params.get("engine_config", {}) or {}

    device = (engine_config.get("device") or params.get("device")).strip().lower()
    paths = _env_script_paths(repo_root)

    want = None
    if device == "nvidia":
        want = paths["nvidia"]
    elif device == "ascend":
        # ⭐ 最小嵌入：当 wings + llm + ascend 时优先使用 set_vllm_ascend_env.sh
        is_wings = (params.get("engine", "").strip().lower() == "wings")
        is_llm = (params.get("model_type", "").strip().lower() == "llm")
        if is_wings and is_llm and os.path.exists(paths["vllm_ascend"]):
            want = paths["vllm_ascend"]
        else:
            want = paths["ascend"]

    if want and os.path.exists(want):
        env_commands.append(f"source {want}")
        return env_commands

    # 兜底：优先 Ascend，其次 Nvidia
    # （如果想在兜底阶段也尽量偏好 vllm_ascend，则先判断它是否存在）
    if os.path.exists(paths.get("vllm_ascend", "")) and device == "ascend":
        logger.warning("[wings] preferred env script missing; fallback to vLLM Ascend env script.")
        env_commands.append(f"source {paths['vllm_ascend']}")
    elif os.path.exists(paths["ascend"]):
        logger.warning("[wings] 'device' not set or script missing; fallback to Ascend env script.")
        env_commands.append(f"source {paths['ascend']}")
    elif os.path.exists(paths["nvidia"]):
        logger.warning("[wings] 'device' not set or script missing; fallback to Nvidia env script.")
        env_commands.append(f"source {paths['nvidia']}")
    else:
        logger.warning("[wings] No env script found under wings/config/. Will start without sourcing env script.")
    return env_commands


def _build_env_commands(params: Dict[str, Any], repo_root: str) -> List[str]:
    """构建环境变量设置命令列表（保持简洁，预留扩展）。"""
    env_commands: List[str] = []
    env_commands.extend(_build_base_env_commands(params, repo_root))
    # 需要时可在此扩展更多环境开关，例如 KV offload/QAT/角色绑定等
    return env_commands


def _get_device_count(params: Dict[str, Any]) -> int:
    """获取设备数量，默认为 1"""
    return int(params.get("device_count", 1))


def _map_device_for_server(raw_device: Optional[str], fallback_from_params: Optional[str]) -> Optional[str]:
    """
    将上游 device 映射为服务端可识别值：
      nvidia -> cuda, ascend -> npu, 其余透传 cuda/npu/cpu；未知返回 None。
    """
    d = (raw_device or fallback_from_params or "").strip().lower()
    if not d:
        return None
    mapping = {"nvidia": "cuda", "ascend": "npu", "cuda": "cuda", "npu": "npu", "cpu": "cpu"}
    return mapping.get(d)


def _append_kv(parts: List[str], key: str, value: Any):
    """以 --k v 形式追加；bool True 仅追加 --k，False/None 忽略。"""
    if value is None:
        return
    if isinstance(value, bool):
        if value:
            parts.append(key)
        return
    # 只有当值不为空时才添加参数
    if str(value).strip():
        parts.extend([key, str(value)])


def _setup_multicard_env_vars(svc_device: Optional[str], device_count: int) -> List[str]:
    """设置多卡环境变量，避免在多个地方重复此逻辑"""
    extra_env_commands = []
    if device_count > 1:
        if svc_device == "npu":
            os.environ["PYTORCH_NPU_ALLOC_CONF"] = "expandable_segments:True"
            os.environ["TASK_QUEUE_ENABLE"] = "2"
            os.environ["CPU_AFFINITY_CONF"] = "1"
            os.environ["TOKENIZERS_PARALLELISM"] = "false"
            os.environ["ALGO"] = "0"
            os.environ["ASCEND_RT_VISIBLE_DEVICES"] = ",".join(str(i) for i in range(device_count))
            extra_env_commands.extend([
                f"export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True",
                f"export TASK_QUEUE_ENABLE=2",
                f"export CPU_AFFINITY_CONF=1",
                f"export TOKENIZERS_PARALLELISM=false",
                f"export ALGO=0",
                f"export ASCEND_RT_VISIBLE_DEVICES={','.join(str(i) for i in range(device_count))}"
            ])
        elif svc_device == "cuda":
            os.environ["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in range(device_count))
            os.environ["NCCL_SOCKET_IFNAME"] = "$IFACE"
            extra_env_commands.extend([
                f"export CUDA_VISIBLE_DEVICES={','.join(str(i) for i in range(device_count))}",
                f"export NCCL_SOCKET_IFNAME=\$IFACE"
            ])
    return extra_env_commands


def _build_torchrun_prefix(params: Dict[str, Any]) -> List[str]:
    """构建 torchrun 命令前缀"""
    engine_config = params.get("engine_config", {}) or {}
    device_count = _get_device_count(params)
    
    # 多卡特定参数
    master_addr = engine_config.get("master_addr", "127.0.0.1")
    master_port = engine_config.get("master_port", 29501)
    nproc_per_node = engine_config.get("nproc_per_node", device_count)
    
    # 构建 torchrun 命令前缀
    torchrun_parts = ["torchrun"]
    _append_kv(torchrun_parts, "--nproc_per_node", nproc_per_node)
    _append_kv(torchrun_parts, "--master_addr", master_addr)
    _append_kv(torchrun_parts, "--master_port", master_port)
    
    return torchrun_parts


def _collect_common_params(params: Dict[str, Any], engine_config: Dict[str, Any]) -> Dict[str, Any]:
    """收集公共参数，避免重复代码"""
    return {
        "host": params.get("host"),
        "port": params.get("port"),
        "svc_device": _map_device_for_server(engine_config.get("device"), params.get("device")),
        "extra_cli": engine_config.get("extra_cli") or []
    }


def _collect_hunyuan_params(params: Dict[str, Any], engine_config: Dict[str, Any]) -> Dict[str, Any]:
    """收集 HunyuanVideo 特定参数"""
    common_params = _collect_common_params(params, engine_config)
    common_params.update({
        "model_path": engine_config.get("model_path") or engine_config.get("model_base"),
        "dit_weight": engine_config.get("dit_weight"),
        "vae_path": engine_config.get("vae_path"),
        "te_path": engine_config.get("text_encoder_path"),
        "te2_path": engine_config.get("text_encoder_2_path"),
        "save_path": params.get("save_path"),
        # 多卡特定参数
        "ring_degree": engine_config.get("ring_degree", 1),
        "ulysses_degree": engine_config.get("ulysses_degree", _get_device_count(params))
    })
    return common_params


def _collect_transformers_params(params: Dict[str, Any], engine_config: Dict[str, Any]) -> Dict[str, Any]:
    """收集 Transformers 特定参数"""
    common_params = _collect_common_params(params, engine_config)
    common_params.update({
        "model_name": params.get("model_name"),
        "model_path": params.get("model_path")
    })
    return common_params


# ──────────────────────────────────────────────────────────────────────────────
# HunyuanVideo（mmgm）命令构建
# ──────────────────────────────────────────────────────────────────────────────
def _build_hunyuan_single_cmd_parts(params: Dict[str, Any]) -> str:
    """
    构建 HunyuanVideo 单机单卡服务端命令部分（参数透传）。
    启动模块：wings.servers.model.hunyuanvideo_server
    """
    engine_config = params.get("engine_config", {}) or {}
    cmd_parts = ["python", "-m", "wings.servers.model.hunyuanvideo_server"]
    
    # 收集参数
    hunyuan_params = _collect_hunyuan_params(params, engine_config)

    _append_kv(cmd_parts, "--server-host", hunyuan_params["host"])
    _append_kv(cmd_parts, "--server-port", hunyuan_params["port"])
    _append_kv(cmd_parts, "--model-base", hunyuan_params["model_path"])
    _append_kv(cmd_parts, "--dit-weight", hunyuan_params["dit_weight"])
    _append_kv(cmd_parts, "--vae-path", hunyuan_params["vae_path"])
    _append_kv(cmd_parts, "--text-encoder-path", hunyuan_params["te_path"])
    _append_kv(cmd_parts, "--text-encoder-2-path", hunyuan_params["te2_path"])
    _append_kv(cmd_parts, "--save-path", hunyuan_params["save_path"])
    _append_kv(cmd_parts, "--device", hunyuan_params["svc_device"])

    # 可选额外 CLI（原样透传）
    for arg in hunyuan_params["extra_cli"]:
        if isinstance(arg, str) and arg:
            cmd_parts.append(arg)

    return " ".join(shlex.quote(p) for p in cmd_parts)


def _build_hunyuan_multinode_cmd_parts(params: Dict[str, Any]) -> str:
    """
    构建 HunyuanVideo 单机多卡服务端命令部分（参数透传）。
    启动模块：wings.servers.model.hunyuanvideo_server（通过 torchrun）
    """
    engine_config = params.get("engine_config", {}) or {}
    
    # 构建 torchrun 命令前缀
    torchrun_parts = _build_torchrun_prefix(params)
    
    # 添加模块
    torchrun_parts.extend(["-m", "wings.servers.model.hunyuanvideo_server"])
    
    # 收集参数
    hunyuan_params = _collect_hunyuan_params(params, engine_config)

    # 构建服务端参数
    server_args = []
    _append_kv(server_args, "--server-host", hunyuan_params["host"])
    _append_kv(server_args, "--server-port", hunyuan_params["port"])
    _append_kv(server_args, "--model-base", hunyuan_params["model_path"])
    _append_kv(server_args, "--dit-weight", hunyuan_params["dit_weight"])
    _append_kv(server_args, "--vae-path", hunyuan_params["vae_path"])
    _append_kv(server_args, "--text-encoder-path", hunyuan_params["te_path"])
    _append_kv(server_args, "--text-encoder-2-path", hunyuan_params["te2_path"])
    _append_kv(server_args, "--save-path", hunyuan_params["save_path"])
    _append_kv(server_args, "--device", hunyuan_params["svc_device"])
    _append_kv(server_args, "--ring-degree", hunyuan_params["ring_degree"])
    _append_kv(server_args, "--ulysses-degree", hunyuan_params["ulysses_degree"])


    # 可选额外 CLI（原样透传）
    extra_cli = []
    for arg in hunyuan_params["extra_cli"]:
        if isinstance(arg, str) and arg:
            extra_cli.append(arg)

    # 组合完整命令
    full_cmd_parts = torchrun_parts + server_args + extra_cli
    return " ".join(shlex.quote(p) for p in full_cmd_parts)


def _build_hunyuan_command(params: Dict[str, Any]) -> str:
    """构建 HunyuanVideo 服务端完整命令（env && main）。
    根据 device_count 判断是否使用单卡或多卡。
    """
    device_count = _get_device_count(params)
    
    # 构建环境命令
    env_commands = _build_env_commands(params, root_dir)
    
    # 设置多卡环境变量
    engine_config = params.get("engine_config", {}) or {}
    svc_device = _map_device_for_server(engine_config.get("device"), params.get("device"))
    
    extra_env_commands = _setup_multicard_env_vars(svc_device, device_count)
    
    # 选择命令构建方式
    if device_count > 1:
        command_str = _build_hunyuan_multinode_cmd_parts(params)
    else:
        command_str = _build_hunyuan_single_cmd_parts(params)
    
    # 组合环境命令和主命令
    all_env_commands = env_commands + extra_env_commands
    return " && ".join(all_env_commands + [command_str]) if all_env_commands else command_str


# ──────────────────────────────────────────────────────────────────────────────
# Transformers LLM（llm）命令构建
# ──────────────────────────────────────────────────────────────────────────────
def _build_transformers_single_cmd_parts(params: Dict[str, Any]) -> str:
    """
    构建 Transformers LLM 单机单卡服务端命令部分（参数透传）。
    启动模块：wings.servers.transformers_server
    需要传入：--host/--port/--model-name/--model-path/--device
    """
    engine_config = params.get("engine_config", {}) or {}
    cmd_parts = ["python", "-m", "wings.servers.transformers_server"]
    
    # 收集参数
    transformers_params = _collect_transformers_params(params, engine_config)

    _append_kv(cmd_parts, "--host", transformers_params["host"])
    _append_kv(cmd_parts, "--port", transformers_params["port"])
    _append_kv(cmd_parts, "--model-name", transformers_params["model_name"])
    _append_kv(cmd_parts, "--model-path", transformers_params["model_path"])
    _append_kv(cmd_parts, "--device", transformers_params["svc_device"])

    # 可选额外 CLI（原样透传）
    for arg in transformers_params["extra_cli"]:
        if isinstance(arg, str) and arg:
            cmd_parts.append(arg)

    return " ".join(shlex.quote(p) for p in cmd_parts)


def _build_transformers_command(params: Dict[str, Any]) -> str:
    """构建 Transformers LLM 服务端完整命令（env && main）。
    仅支持单卡模式。
    """
    # 构建环境命令
    env_commands = _build_env_commands(params, root_dir)
    
    # 构建命令
    command_str = _build_transformers_single_cmd_parts(params)
    
    # 组合环境命令和主命令
    return " && ".join(env_commands + [command_str]) if env_commands else command_str


# ──────────────────────────────────────────────────────────────────────────────
# 单机启动（对齐 vllm_adapter：_start_* 返回 Popen；start_engine 负责等待+日志）
# ──────────────────────────────────────────────────────────────────────────────
def _start_hunyuan_single(params: Dict[str, Any]) -> subprocess.Popen:
    """启动单机 HunyuanVideo 服务。"""
    try:
        cmd = _build_hunyuan_command(params)
        
        # 显式传递环境变量
        env = os.environ.copy()
        if params.get("host"):
            env["SERVER_HOST"] = str(params["host"])
        if params.get("port"):
            env["SERVER_PORT"] = str(params["port"])
        
        process = subprocess.Popen(
            ["/bin/bash", "-c", cmd],
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env
        )
        log_process_pid(name="wings_mmgm", parent_pid=os.getpid(), child_pid=process.pid)
        return process
    except Exception as e:
        logger.error(f"Error starting HunyuanVideo service: {e}", exc_info=True)
        raise


def _start_transformers_single(params: Dict[str, Any]) -> subprocess.Popen:
    """启动单机 Transformers LLM 服务。"""
    try:
        cmd = _build_transformers_command(params)
        process = subprocess.Popen(
            ["/bin/bash", "-c", cmd],
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        log_process_pid(name="wings_llm_transformers", parent_pid=os.getpid(), child_pid=process.pid)
        return process
    except Exception as e:
        logger.error(f"Error starting Transformers LLM service: {e}", exc_info=True)
        raise


def _select_and_start_single(params: Dict[str, Any]) -> Tuple[Optional[subprocess.Popen], Optional[str]]:
    """
    选择对应服务并启动（单机）。
    返回：(process, service_tag)；若不匹配任何服务，返回 (None, None)。
    """
    if _is_wings_mmgm(params):
        return _start_hunyuan_single(params), "mmgm"
    if _is_wings_llm(params):
        return _start_transformers_single(params), "llm"
    return None, None


# ──────────────────────────────────────────────────────────────────────────────
# 统一入口：start_engine（仿照 vllm_adapter.start_engine 的逻辑）
# ──────────────────────────────────────────────────────────────────────────────
def start_engine(params: Dict[str, Any]):
    """
    启动入口统一分发（尽量简单）：
      - wings + mmgm -> HunyuanVideo
      - wings + llm  -> Transformers LLM
      其它组合直接跳过（返回 False，保持对其他引擎零影响）。
    """
    logger.info("Wings adapter: Preparing to start service...")
    logger.info('-- Initial parameters logged --')

    process, service_tag = None, None
    try:
        # 仅提供单机模式；与 vllm_adapter 匹配的"单机/分布式"分流在此保持简单
        process, service_tag = _select_and_start_single(params)

        # 检查返回值有效性
        if process is None:
            logger.info(
                "Wings adapter: skip. engine=%r, model_type=%r (no matching wings service).",
                params.get("engine"), params.get("model_type")
            )
            return False
        
        if not service_tag:
            logger.warning("Wings adapter: service_tag is empty, but process started successfully")

        # 验证进程状态
        if process.poll() is not None:
            raise RuntimeError(f"Engine process terminated immediately with return code: {process.returncode}")

        # 与 vllm_adapter 一致：等待"应用启动完成"关键字（Uvicorn 常见文案）
        wait_for_process_startup(
            process=process,
            success_message="Application startup complete",
            _logger=logger
        )

        # 持续流式输出子进程日志
        log_stream(process)

        logger.info(f"Wings adapter: Service started successfully with tag: {service_tag}")
        return True

    except ValueError as e:
        # 参数相关错误
        logger.error(f"Wings adapter: Parameter error - {e}")
        return False
    except RuntimeError as e:
        # 运行时错误（如进程启动失败）
        logger.error(f"Wings adapter: Runtime error - {e}")
        # 清理资源
        if process and process.poll() is None:
            process.terminate()
        return False
    except Exception as e:
        # 其他未知错误
        logger.error(f"Wings adapter: Unexpected error - {e}", exc_info=True)
        # 清理资源
        if process and process.poll() is None:
            process.terminate()
        return False