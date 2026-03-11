# =============================================================================
# 文件: engines/wings_adapter.py
# 用途: Wings 引擎适配器（HunyuanVideo / QwenImage / Transformers LLM 服务）
# 状态: 活跃适配器
#
# 功能概述:
#   本模块负责将统一参数转换为 Wings 自有引擎的启动命令。
#   根据 model_type 选择不同的后端服务：
#   - engine='wings' + model_type='mmgm' + model_name='hunyuan-video' → HunyuanVideo 文生视频
#   - engine='wings' + model_type='mmgm' + model_name='qwen-image'   → QwenImage 文生图
#   - engine='wings' + model_type='llm'                              → Transformers LLM
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
# =============================================================================
# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
Wings 引擎适配器。

在 sidecar launcher 模式下，根据 model_type 生成不同引擎的启动脚本：
- mmgm (hunyuan-video) → python -m wings.servers.model.hunyuanvideo_server
- mmgm (qwen-image)    → python -m wings.servers.model.qwenimage_server
- llm                  → python -m wings.servers.transformers_server
"""

import logging
import os
import re
import shlex
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 模块根目录
root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ──────────────── Shell 安全 ────────────────

def _sanitize_shell_path(path: str) -> str:
    """从文件路径中移除 shell 元字符，防止命令注入攻击。"""
    return re.sub(r"[^a-zA-Z0-9/_.-]", "", path)


# ──────────────── 判定函数 ────────────────

def _is_wings_text2video(params: Dict[str, Any]) -> bool:
    """是否应启动 HunyuanVideo（多模态文生视频）服务。"""
    engine_config = params.get("engine_config", {}) or {}
    model_name = (engine_config.get("model_name") or "").strip().lower()
    return (
        params.get("engine", "").strip().lower() == "wings"
        and params.get("model_type", "").strip().lower() == "mmgm"
        and model_name == "hunyuan-video"
    )


def _is_wings_text2image(params: Dict[str, Any]) -> bool:
    """是否应启动 QwenImage（多模态文生图）服务。"""
    engine_config = params.get("engine_config", {}) or {}
    model_name = (engine_config.get("model_name") or "").strip().lower()
    return (
        params.get("engine", "").strip().lower() == "wings"
        and params.get("model_type", "").strip().lower() == "mmgm"
        and model_name == "qwen-image"
    )


def _is_wings_llm(params: Dict[str, Any]) -> bool:
    """是否应启动 Transformers（文本 LLM）服务。"""
    return (
        params.get("engine", "").strip().lower() == "wings"
        and params.get("model_type", "").strip().lower() == "llm"
    )


# ──────────────── 环境脚本 ────────────────

def _env_script_paths(repo_root: str) -> Dict[str, str]:
    """返回 nvidia/ascend 两类环境脚本的绝对路径。"""
    return {
        "nvidia": os.path.join(repo_root, "wings", "config", "set_wings_nvidia_env.sh"),
        "ascend": os.path.join(repo_root, "wings", "config", "set_wings_ascend_env.sh"),
        "vllm_ascend": os.path.join(repo_root, "wings", "config", "set_vllm_ascend_env.sh"),
    }


def _build_base_env_commands(params: Dict[str, Any], repo_root: str) -> List[str]:
    """构建基础环境脚本 source 命令。"""
    env_commands: List[str] = []
    engine_config = params.get("engine_config", {}) or {}
    device = (engine_config.get("device") or params.get("device", "")).strip().lower()
    paths = _env_script_paths(repo_root)

    want = None
    if device == "nvidia":
        want = paths["nvidia"]
    elif device == "ascend":
        is_wings = params.get("engine", "").strip().lower() == "wings"
        is_llm = params.get("model_type", "").strip().lower() == "llm"
        if is_wings and is_llm and os.path.exists(paths["vllm_ascend"]):
            want = paths["vllm_ascend"]
        else:
            want = paths["ascend"]

    if want and os.path.exists(want):
        env_commands.append(f"source {want}")
        return env_commands

    # 兜底
    if os.path.exists(paths.get("vllm_ascend", "")) and device == "ascend":
        env_commands.append(f"source {paths['vllm_ascend']}")
    elif os.path.exists(paths["ascend"]):
        logger.warning("[wings] fallback to Ascend env script.")
        env_commands.append(f"source {paths['ascend']}")
    elif os.path.exists(paths["nvidia"]):
        logger.warning("[wings] fallback to Nvidia env script.")
        env_commands.append(f"source {paths['nvidia']}")
    else:
        logger.warning("[wings] No env script found. Starting without sourcing env script.")
    return env_commands


# ──────────────── 参数工具 ────────────────

def _append_kv(parts: List[str], key: str, value: Any):
    """以 --k v 形式追加；bool True 仅追加 --k，False/None 忽略。"""
    if value is None:
        return
    if isinstance(value, bool):
        if value:
            parts.append(key)
        return
    if isinstance(value, str) and not value.strip():
        return
    if hasattr(value, '__len__') and not isinstance(value, str) and len(value) == 0:
        return
    parts.extend([key, str(value)])


def _get_device_count(params: Dict[str, Any]) -> int:
    """获取设备数量，默认为 1"""
    return int(params.get("device_count", 1))


def _map_device_for_server(raw_device: Optional[str], fallback: Optional[str]) -> Optional[str]:
    """将上游 device 映射为服务端可识别值：nvidia→cuda, ascend→npu。"""
    d = (raw_device or fallback or "").strip().lower()
    if not d:
        return None
    mapping = {"nvidia": "cuda", "ascend": "npu", "cuda": "cuda", "npu": "npu", "cpu": "cpu"}
    return mapping.get(d)


def _setup_multicard_env_lines(svc_device: Optional[str], device_count: int) -> List[str]:
    """生成多卡环境变量的 export 行。"""
    lines: List[str] = []
    if device_count > 1:
        if svc_device == "npu":
            dev_ids = ",".join(str(i) for i in range(device_count))
            lines.extend([
                "export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True",
                "export TASK_QUEUE_ENABLE=2",
                "export CPU_AFFINITY_CONF=1",
                "export TOKENIZERS_PARALLELISM=false",
                "export ALGO=0",
                f"export ASCEND_RT_VISIBLE_DEVICES={dev_ids}",
            ])
        elif svc_device == "cuda":
            dev_ids = ",".join(str(i) for i in range(device_count))
            lines.append(f"export CUDA_VISIBLE_DEVICES={dev_ids}")
    return lines


# ──────────────── HunyuanVideo 命令构建 ────────────────

def _build_text2video_single_cmd(params: Dict[str, Any]) -> str:
    """构建 HunyuanVideo 单卡启动命令。"""
    engine_config = params.get("engine_config", {}) or {}
    parts = ["python", "-m", "wings.servers.model.hunyuanvideo_server"]

    _append_kv(parts, "--server-host", params.get("host"))
    _append_kv(parts, "--server-port", params.get("port"))
    model_path = engine_config.get("model_path") or engine_config.get("model_base")
    _append_kv(parts, "--model-base", model_path)
    _append_kv(parts, "--dit-weight", engine_config.get("dit_weight"))
    _append_kv(parts, "--vae-path", engine_config.get("vae_path"))
    _append_kv(parts, "--text-encoder-path", engine_config.get("text_encoder_path"))
    _append_kv(parts, "--text-encoder-2-path", engine_config.get("text_encoder_2_path"))
    _append_kv(parts, "--save-path", params.get("save_path"))
    _append_kv(parts, "--device", _map_device_for_server(
        engine_config.get("device"), params.get("device")))

    for arg in (engine_config.get("extra_cli") or []):
        if isinstance(arg, str) and arg:
            parts.append(arg)
    return " ".join(shlex.quote(p) for p in parts)


def _build_text2video_multicard_cmd(params: Dict[str, Any]) -> str:
    """构建 HunyuanVideo 多卡 torchrun 启动命令。"""
    engine_config = params.get("engine_config", {}) or {}
    device_count = _get_device_count(params)

    parts = ["torchrun"]
    _append_kv(parts, "--nproc_per_node", engine_config.get("nproc_per_node", device_count))
    _append_kv(parts, "--master_addr", engine_config.get("master_addr", "127.0.0.1"))
    _append_kv(parts, "--master_port", engine_config.get("master_port", 29501))
    parts.extend(["-m", "wings.servers.model.hunyuanvideo_server"])

    model_path = engine_config.get("model_path") or engine_config.get("model_base")
    _append_kv(parts, "--server-host", params.get("host"))
    _append_kv(parts, "--server-port", params.get("port"))
    _append_kv(parts, "--model-base", model_path)
    _append_kv(parts, "--dit-weight", engine_config.get("dit_weight"))
    _append_kv(parts, "--vae-path", engine_config.get("vae_path"))
    _append_kv(parts, "--text-encoder-path", engine_config.get("text_encoder_path"))
    _append_kv(parts, "--text-encoder-2-path", engine_config.get("text_encoder_2_path"))
    _append_kv(parts, "--save-path", params.get("save_path"))
    svc_device = _map_device_for_server(engine_config.get("device"), params.get("device"))
    _append_kv(parts, "--device", svc_device)
    _append_kv(parts, "--ring-degree", engine_config.get("ring_degree", 1))
    _append_kv(parts, "--ulysses-degree", engine_config.get("ulysses_degree", device_count))

    for arg in (engine_config.get("extra_cli") or []):
        if isinstance(arg, str) and arg:
            parts.append(arg)
    return " ".join(shlex.quote(p) for p in parts)


# ──────────────── QwenImage 命令构建 ────────────────

def _build_text2image_cmd(params: Dict[str, Any]) -> str:
    """构建 QwenImage 单卡启动命令。"""
    engine_config = params.get("engine_config", {}) or {}
    parts = ["python", "-m", "wings.servers.model.qwenimage_server"]

    model_path = engine_config.get("model_path") or params.get("model_path")
    svc_device = _map_device_for_server(engine_config.get("device"), params.get("device"))
    model_name = engine_config.get("model_name") or params.get("model_name") or "qwen-image"

    _append_kv(parts, "--model-path", model_path)
    _append_kv(parts, "--device", svc_device)
    _append_kv(parts, "--host", params.get("host"))
    _append_kv(parts, "--port", params.get("port"))
    _append_kv(parts, "--save-path", engine_config.get("save_path") or params.get("save_path"))
    _append_kv(parts, "--model-name", model_name)
    _append_kv(parts, "--device-count", 1)

    for arg in (engine_config.get("extra_cli") or []):
        if isinstance(arg, str) and arg:
            parts.append(arg)
    return " ".join(shlex.quote(p) for p in parts)


# ──────────────── Transformers LLM 命令构建 ────────────────

def _build_transformers_cmd(params: Dict[str, Any]) -> str:
    """构建 Transformers LLM 单卡启动命令。"""
    engine_config = params.get("engine_config", {}) or {}
    parts = ["python", "-m", "wings.servers.transformers_server"]

    svc_device = _map_device_for_server(engine_config.get("device"), params.get("device"))
    _append_kv(parts, "--host", params.get("host"))
    _append_kv(parts, "--port", params.get("port"))
    _append_kv(parts, "--model-name", params.get("model_name"))
    _append_kv(parts, "--model-path", params.get("model_path"))
    _append_kv(parts, "--device", svc_device)

    for arg in (engine_config.get("extra_cli") or []):
        if isinstance(arg, str) and arg:
            parts.append(arg)
    return " ".join(shlex.quote(p) for p in parts)


# ──────────────── 公共入口 ────────────────

def build_start_command(params: Dict[str, Any]) -> str:
    """生成 Wings 启动命令（不含环境设置）。

    根据 model_type 和 model_name 分派到不同服务：
    - mmgm + hunyuan-video → HunyuanVideo
    - mmgm + qwen-image    → QwenImage
    - llm                  → Transformers LLM

    Args:
        params: 参数字典

    Returns:
        str: 核心启动命令

    Raises:
        ValueError: 无匹配的 wings 服务
    """
    if _is_wings_text2video(params):
        device_count = _get_device_count(params)
        if device_count > 1:
            return _build_text2video_multicard_cmd(params)
        return _build_text2video_single_cmd(params)

    if _is_wings_text2image(params):
        return _build_text2image_cmd(params)

    if _is_wings_llm(params):
        return _build_transformers_cmd(params)

    raise ValueError(
        f"No matching wings service for engine='{params.get('engine')}', "
        f"model_type='{params.get('model_type')}'"
    )


def build_start_script(params: Dict[str, Any]) -> str:
    """生成完整的 bash 启动脚本体（start_command.sh 内容，不含 shebang）。

    脚本结构：
        [source env script]
        [export 多卡环境变量]
        [export 特定场景环境变量]
        exec <command>

    Args:
        params: 参数字典

    Returns:
        str: 完整的 bash 脚本体（以换行符结尾）
    """
    engine_config = params.get("engine_config", {}) or {}
    device_count = _get_device_count(params)
    svc_device = _map_device_for_server(engine_config.get("device"), params.get("device"))

    lines: List[str] = []

    # 1. 环境脚本
    lines.extend(_build_base_env_commands(params, root_dir))

    # 2. 多卡环境变量
    lines.extend(_setup_multicard_env_lines(svc_device, device_count))

    # 3. 场景特定环境变量
    if _is_wings_text2video(params) and svc_device == "npu" and device_count == 1:
        lines.extend([
            "export TOKENIZERS_PARALLELISM=false",
            "export ALGO=0",
        ])

    if _is_wings_text2image(params) and svc_device == "npu" and device_count == 1:
        lines.extend([
            "export ROPE_FUSE=1",
            "export ADALN_FUSE=1",
            "export COND_CACHE=1",
            "export UNCOND_CACHE=1",
        ])

    # 4. 核心命令
    cmd = build_start_command(params)
    lines.append(f"exec {cmd}")

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
