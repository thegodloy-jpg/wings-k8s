# =============================================================================
# File: core/config_loader.py
# Purpose: 多层配置加载与合并器 — 是 launcher 控制平面最大的单个模块。
# Architecture:
#   负责把多层配置源（硬件默认、引擎默认、模型专属、用户自定义、CLI 参数）
#   合并为一份统一的参数字典，提供给 engine adapter 使用。
# Config Merge Priority (low → high):
#   1. 硬件默认配置 (e.g., config/vllm_default.json)
#   2. 模型专属配置 (model_deploy_config 匹配)
#   3. 用户自定义配置 (--config-file 指定的 JSON)
#   4. CLI 参数 / 环境变量覆盖
# Key Responsibilities:
#   - 引擎自动选择（_auto_select_engine）
#   - 参数名映射（engine_parameter_mapping.json）
#   - 张量并行度自动设置
#   - PD 分离 / LMCache / Router / Soft FP8 等高级特性注入
#   - 分布式参数注入（Ray / NIXL / HCCL）
#   - MMGM (HunyuanVideo) 多模态特殊路径探测
# =============================================================================
# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

from typing import Dict, Any
import argparse
import json
import logging
import os
from pathlib import Path
import math

from app.utils.env_utils import get_master_ip, get_node_ips, get_lmcache_env, get_pd_role_env, \
    get_config_force_env, get_soft_fp8_env, get_vllm_distributed_port, get_sglang_distributed_port, get_router_env, \
    get_router_instance_group_name_env, get_router_instance_name_env, get_router_nats_path_env, \
    get_operator_acceleration_env, get_local_ip
from app.utils.file_utils import check_torch_dtype, get_directory_size, check_permission_640, load_json_config
from app.utils.model_utils import ModelIdentifier

logger = logging.getLogger(__name__)

#  解析默认配置目录路径（优先级：环境变量 > 包内自带 > 硬编码回退）
def _resolve_default_config_dir() -> str:
    """解析默认配置目录，放在此目录下的配置文件为引擎提供默认参数。

    查找顺序：
    1. WINGS_CONFIG_DIR 环境变量（支持部署时重定向）
    2. 包内的 app/config/ 目录（安装部署场景）
    3. "wings/config" 硬编码回退（兼容旧版目录结构）
    """
    env_dir = os.getenv("WINGS_CONFIG_DIR", "").strip()
    if env_dir:
        return env_dir
    bundled_dir = Path(__file__).resolve().parents[1] / "config"
    if bundled_dir.exists():
        return str(bundled_dir)
    return "wings/config"


# 配置目录单例（模块加载时解析一次）
DEFAULT_CONFIG_DIR = _resolve_default_config_dir()

# 各设备类型和引擎对应的默认配置文件名映射
DEFAULT_CONFIG_FILES = {
    "nvidia": "vllm_default.json",
    "ascend": "vllm_default.json",
    "distributed": "distributed_config.json",
    "engine_parameter_mapping": "engine_parameter_mapping.json",
    # Engine-specific fallback defaults (used when vllm_default.json
    # has no model-level section for the selected engine)
    "sglang": "sglang_default.json",
    "mindie": "mindie_default.json",
}

SUPPORTED_DEVICE_TYPES = {"nvidia", "ascend"}


def _load_mapping(config_path: str, mapping_key: str) -> Dict[str, Any]:
    """从 JSON 配置文件中安全加载指定 key 下的映射字典。

    如果文件不存在、内容为空或格式不对，均返回空字典并打印警告。
    用于加载参数名映射表（如 default_to_vllm_parameter_mapping）。
    """
    cfg = load_json_config(config_path)
    mapping = cfg.get(mapping_key, {})
    if not isinstance(mapping, dict):
        logger.warning(
            "Invalid mapping format: key=%s file=%s type=%s; fallback to empty mapping",
            mapping_key,
            config_path,
            type(mapping).__name__,
        )
        return {}
    if not mapping:
        logger.warning("Missing/empty mapping key=%s in file=%s", mapping_key, config_path)
    return mapping


def _get_h20_model_hint() -> str:
    """获取 H20 GPU 型号提示（用于 DeepSeek 模型的卡型专属配置）。

    H20 GPU 有两种型号：H20-96G 和 H20-141G，显存不同导致最优参数不同。
    通过 WINGS_H20_MODEL 环境变量显式指定，返回空串表示未指定或无效。
    """
    hint = os.getenv("WINGS_H20_MODEL", "").strip()
    if not hint:
        return ""
    if hint in ("H20-96G", "H20-141G"):
        return hint
    logger.warning("Invalid WINGS_H20_MODEL=%s, expected H20-96G or H20-141G", hint)
    return ""


def _check_vram_requirements(weight_path: str, hardware_env: Dict[str, Any], nodes_count: int) -> None:
    """检查总可用显存是否足以加载模型权重。

    将模型目录总大小与全部节点的可用显存总和对比：
    - 总显存 < 模型大小                → WARNING （可能 OOM）
    - 总显存 < 模型大小 × 1.5            → WARNING （性能不佳）
    - 总显存 ≥ 模型大小 × 1.5            → INFO   （充裕）

    Args:
        weight_path:  模型权重目录路径
        hardware_env: 硬件环境信息（含 details 列表，每项含 free_memory）
        nodes_count:  分布式节点总数，用于计算跨节点总显存
    """
    if not os.path.exists(weight_path):
        logger.warning(f"Model weight path not found: {weight_path}")
        return

    weight_size_bytes = get_directory_size(weight_path)
    weight_size_gb = weight_size_bytes / (1024 ** 3)

    if not hardware_env.get("details"):
        logger.warning("Cannot get VRAM details, skipping VRAM check")
        return

    # 如果 details 中缺少 free_memory 字段（只有 name），跳过 VRAM 检查
    if not all("free_memory" in d for d in hardware_env["details"]):
        logger.warning("VRAM details lack free_memory field, skipping VRAM check")
        return

    # VRAM
    free_vram_per_node = sum(d["free_memory"] for d in hardware_env["details"])
    total_free_vram = free_vram_per_node * nodes_count

    if total_free_vram < weight_size_gb:
        logger.warning(
            f"Insufficient VRAM: Required {weight_size_gb:.2f}GB, "
            f"but only {total_free_vram:.2f}GB available "
            f"({nodes_count} nodes  {free_vram_per_node:.2f}GB each)"
        )
    elif total_free_vram < weight_size_gb * 1.5:
        logger.warning(
            f"Performance warning: Total VRAM ({total_free_vram:.2f}GB) is less than 1.5x "
            f"model weight size ({weight_size_gb:.2f}GB) "
            f"({nodes_count} nodes  {free_vram_per_node:.2f}GB each)"
        )
    else:
        logger.info(
            f"VRAM check: Total VRAM ({total_free_vram:.2f}GB) is more than 1.5x "
            f"model weight size ({weight_size_gb:.2f}GB) "
            f"({nodes_count} nodes  {free_vram_per_node:.2f}GB each)"
        )


def _merge_cmd_params(hardware_env, engine_specific_defaults, cmd_known_params, model_info):
    """将硬件上下文、引擎默认参数和用户 CLI 参数三层合并。

    该函数是配置合并的核心入口：
    1. 从 hardware_env 和 cmd_known_params 抽取通用上下文（device、分布式、模型类型等）
    2. 从 cmd_known_params 抽取引擎级参数（host、port、dtype、quantization 等）
    3. 根据 engine 类型分发到 _merge_vllm_params / _merge_mindie_params / _merge_sglang_params

    Args:
        hardware_env:             硬件环境信息
        engine_specific_defaults: 从默认配置文件加载的引擎参数
        cmd_known_params:         用户 CLI 参数
        model_info:               模型元信息对象

    Returns:
        合并后的引擎参数字典
    """
    common_context = {
        "device": hardware_env.get("device"),
        "device_details": hardware_env.get("details"),
        "device_count": cmd_known_params.get("device_count", 1),
        "engine": cmd_known_params.get("engine"),
        "distributed": cmd_known_params.get("distributed"),
        "model_type": model_info.identify_model_type(),
        "gpu_usage_mode": cmd_known_params.get("gpu_usage_mode", "full")
    }
    engine_cmd_parameter = {
        "host": cmd_known_params.get("host"),
        "port": cmd_known_params.get("port"),
        "model_name": cmd_known_params.get("model_name"),
        "model_path": cmd_known_params.get("model_path"),
        "input_length": cmd_known_params.get("input_length"),
        "output_length": cmd_known_params.get("output_length"),
        "trust_remote_code": cmd_known_params.get("trust_remote_code"),
        "dtype": cmd_known_params.get("dtype"),
        "kv_cache_dtype": cmd_known_params.get("kv_cache_dtype"),
        "quantization": cmd_known_params.get("quantization"),
        "quantization_param_path": cmd_known_params.get("quantization_param_path"),
        "gpu_memory_utilization": cmd_known_params.get("gpu_memory_utilization"),
        "enable_chunked_prefill": cmd_known_params.get("enable_chunked_prefill"),
        "block_size": cmd_known_params.get("block_size"),
        "max_num_seqs": cmd_known_params.get("max_num_seqs"),
        "seed": cmd_known_params.get("seed"),
        "enable_expert_parallel": cmd_known_params.get("enable_expert_parallel"),
        "max_num_batched_tokens": cmd_known_params.get("max_num_batched_tokens"),
        "enable_prefix_caching": cmd_known_params.get("enable_prefix_caching")
    }

    # 根据引擎类型分发到不同的参数合并函数
    engine = common_context["engine"]
    # 将嵌套 dict 型配置值序列化为 JSON 字符串，便于作为 CLI 参数传递
    for key, value in engine_specific_defaults.items():
        if isinstance(value, dict):  # 字典值转为 JSON 字符串
            engine_specific_defaults[key] = json.dumps(value)
    if engine in ("vllm", "vllm_ascend"):
        return _merge_vllm_params(engine_specific_defaults, common_context, engine_cmd_parameter, model_info)
    elif engine == "mindie":
        return _merge_mindie_params(engine_specific_defaults, common_context, engine_cmd_parameter)
    elif engine == "sglang":
        return _merge_sglang_params(engine_specific_defaults, common_context, engine_cmd_parameter)
    return engine_specific_defaults


def _merge_vllm_params(params, ctx, engine_cmd_parameter, model_info):
    """合并 vLLM / vLLM-Ascend 引擎专属参数。

    调用多个 setter 函数将硬件上下文、引擎配置和模型信息合并到 params 字典。

    调用链路:
        1. _set_common_params       → 根据参数映射表翻译 CLI 参数
        2. _set_sequence_length     → 合并 input_length + output_length
        3. _set_parallelism_params  → 设置张量并行度
        4. _set_kv_cache_config     → LMCache / PD 分离 KV Transfer 配置
        5. _set_router_config       → Wings Router NATS 配置
        6. _set_operator_acceleration → 昇腾算子加速
        7. _set_soft_fp8            → Soft FP8 量化配置
        8. _set_cuda_graph_sizes    → CUDA Graph 捕获批次大小
        9. _set_task                → embedding/rerank 任务类型

    Args:
        params:              当前引擎参数字典（会被原地修改）
        ctx:                 通用上下文（device、device_count、distributed 等）
        engine_cmd_parameter: 用户 CLI 传入的引擎参数
        model_info:          模型元信息对象

    Returns:
        Dict[str, Any]: 合并后的引擎参数字典
    """
    # 加载引擎参数名映射表
    engine_param_map_config_path = os.path.join(
        DEFAULT_CONFIG_DIR,
        DEFAULT_CONFIG_FILES.get("engine_parameter_mapping")
    )

    #
    _set_common_params(params, engine_cmd_parameter, engine_param_map_config_path)
    _set_sequence_length(params, engine_cmd_parameter)
    _set_parallelism_params(params, ctx)
    _set_kv_cache_config(params, ctx)
    _set_router_config(params)
    _set_operator_acceleration(params, ctx)
    _set_soft_fp8(params, ctx, model_info)
    _set_cuda_graph_sizes(params, ctx, model_info)
    _set_task(params, ctx)
    return params


def _set_cuda_graph_sizes(params, ctx, model_info):
    """为 vllm/vllm_ascend 在非全量模式（gpu_usage_mode != full）下自动计算 cuda_graph_sizes。

    在共享显存（MIG/虚拟 GPU）场景下，需要根据可用显存和模型层数推算最优
    CUDA Graph 捕获批次大小上限，避免 CUDA Graph 构建过多导致显存溢出。

    显存获取优先级：
      1. device_details[0]["total_memory"] — 运行时检测
      2. WINGS_DEVICE_MEMORY 环境变量 — K8s 部署模板注入（单位 GB，整数或小数）
      3. 硬编码 fallback 12 GB
    """
    if ctx["gpu_usage_mode"] != "full" and ctx["model_type"] == "llm":
        if ctx["device_details"] and ctx["device_details"][0]:
            total_memory = ctx["device_details"][0].get("total_memory", 12)
            if total_memory is None:
                total_memory = 12
                logger.warning("total_memory is None in device details, defaulting to 12G")
        else:
            mem_env = os.getenv("WINGS_DEVICE_MEMORY", "").strip()
            if mem_env:
                try:
                    total_memory = float(mem_env)
                    logger.info("Using WINGS_DEVICE_MEMORY=%s GB for cuda-graph-sizes", total_memory)
                except ValueError:
                    total_memory = 12
                    logger.warning("Invalid WINGS_DEVICE_MEMORY='%s', fallback to 12GB", mem_env)
            else:
                total_memory = 12
                logger.warning("Can't get device details and WINGS_DEVICE_MEMORY not set, fallback to 12GB")
        max_capture_size = int(total_memory / 64 * 2048 - 256)
        max_num_batch_sizes = math.floor(
            max_capture_size / (model_info.num_hidden_layers + 1) / 2)
        cudagraph_capture_sizes = [1, 2, 4, 8, 16, 24, 32, 40, 48, 56, 64, \
                                72, 80, 88, 96, 104, 112, 120, 128, 136, 144, \
                                152, 160, 168, 176, 184, 192, 200, 208, 216, \
                                224, 232, 240, 256, 264, 272, 280, 288, 296, \
                                304, 312, 320, 328, 336, 344, 352, 360, 368, \
                                376, 384, 392, 400, 408, 416, 424, 432, 440, \
                                448, 456, 464, 472, 480, 488, 496, 504, 512]
        max_num_batch_sizes = max(min(max_num_batch_sizes, len(cudagraph_capture_sizes)), 1)
        cuda_graph_sizes = cudagraph_capture_sizes[max_num_batch_sizes - 1]
        params["cuda_graph_sizes"] = cuda_graph_sizes
        logger.info(f"cuda-graph-sizes is set by {cuda_graph_sizes}")


def _set_operator_acceleration(params, ctx):
    """当昇腾算子加速（USE_KUNLUN_ATB）启用时，注入 use_kunlun_atb=True 到参数字典。"""
    if get_operator_acceleration_env() and ctx["device"] == "ascend":
        params['use_kunlun_atb'] = True
    else:
        return


def _set_soft_fp8(params, ctx, model_info):
    """启用 Soft FP8 量化推理（仅支持昇腾设备上的 DeepSeekV3 FP8 量化模型）。

    Soft FP8 通过 ascend_scheduler_config + torchair_graph_config 联合开启，
    需要同时禁用 prefix caching 和专家并行（EP），固定 TP=4。
    """
    model_architecture = model_info.model_architecture
    model_quantize = model_info.model_quantize
    if get_soft_fp8_env():
        if ctx['device'] != "ascend":
            logger.warning("Soft FP8 is only supported on Ascend devices")
        elif model_architecture != "DeepseekV3ForCausalLM":
            logger.warning("Soft FP8 is only supported for DeepseekV3 Series models")
        elif model_quantize != "fp8":
            raise ValueError("Soft FP8 is only supported for quantized FP8 models")
        else:
            logger.info("Will use Soft FP8 configuration")
            params['quantization'] = 'ascend'
            additional_config = {
                "ascend_scheduler_config": {
                    "enabled": True
                },
                "torchair_graph_config": {
                    "enabled": True
                }
            }
            params['additional_config'] = json.dumps(additional_config)
            params['no_enable_prefix_caching'] = True
            # DP
            params['enable_expert_parallel'] = False
            params['tensor_parallel_size'] = 4
            params['use_kunlun_atb'] = False


def _set_common_params(params, engine_cmd_parameter, config_path):
    """根据参数映射表，将用户 CLI 参数翻译为引擎实际的参数键名并写入 params。"""
    vllm_param_map_config = _load_mapping(config_path, 'default_to_vllm_parameter_mapping')
    for key, value in vllm_param_map_config.items():
        if value and engine_cmd_parameter.get(key) is not None:
            params[value] = engine_cmd_parameter.get(key)


def _set_sequence_length(params, engine_cmd_parameter):
    """将 input_length + output_length 合并为 max_model_len 并写入 params。"""
    input_len = engine_cmd_parameter.get("input_length")
    output_len = engine_cmd_parameter.get("output_length")

    #  None
    input_len = input_len if input_len is not None else 0
    output_len = output_len if output_len is not None else 0

    max_model_len = input_len + output_len
    if max_model_len <= 0:
        return
    params['max_model_len'] = max_model_len


def _set_task(params, ctx):
    """根据模型类型（embedding/rerank）设置 vllm task 参数。

    昇腾设备上 embedding/rerank 模型需要强制启用 eager 模式并关闭 ATB 算子加速。
    """
    if ctx["model_type"] == "embedding":
        params["task"] = "embedding"
        if ctx["device"] == "ascend":
            params["enforce_eager"] = True
            params["use_kunlun_atb"] = False
    elif ctx["model_type"] == "rerank":
        params["task"] = "score"
        if ctx["device"] == "ascend":
            params["enforce_eager"] = True
            params["use_kunlun_atb"] = False
    else:
        return


def _set_parallelism_params(params, ctx):
    """根据设备数和分布式模式设置张量并行度（tensor_parallel_size）。"""
    #
    _adjust_tensor_parallelism(
        params,
        ctx["device_count"],
        'tensor_parallel_size',
        ctx['distributed']
    )


def _get_pd_config(ctx, pd_role):
    """生成 PD（Prefill-Decode）分离部署所需的 KV Transfer 配置片段。

    参数:
        pd_role: PD 角色，"P" 表示 Prefill 节点，"D" 表示 Decode 节点
        ctx: 运行上下文，如 {'device': 'ascend', 'device_count': 2}

    返回:
        包含 KV Transfer 配置项的字典
    """
    device = ctx.get('device', '')
    config = {}

    if device == "ascend":
        # AscendPD
        kv_role = "kv_producer" if pd_role == "P" else "kv_consumer"
        config = {
            "kv_connector": "LLMDataDistCMgrConnector",
            "kv_role": kv_role,
            "kv_buffer_device": "npu",
            "kv_parallel_size": ctx.get("device_count", 1),
            "kv_port": os.getenv("PD_KV_PORT", "20001"),
            "kv_connector_module_path": "vllm_ascend.distributed.llmdatadist_c_mgr_connector"
        }
        logger.info(f"[PD Config] Ascend device detected, role={pd_role}, kv_role={kv_role}")
    else:
        # AscendPD
        config = {
            "kv_connector": "NixlConnector",
            "kv_role": "kv_both"
        }
        logger.info(f"[PD Config] non-ascend device ({device}) detected, role={pd_role}")

    return config


def _set_kv_cache_config(params, ctx):
    """根据 LMCache Offload 和 PD 分离角色，生成 vllm kv_transfer_config 配置。

    优先级逻辑：
    - LMCache + PD 同时启用 → MultiConnector（同时承载 KV Offload 和 PD 传输）
    - 仅启用 LMCache → LMCacheConnectorV1
    - 仅启用 PD → 按设备类型选择 LLMDataDistCMgrConnector 或 NixlConnector
    - 两者都未启用 → 跳过不注入
    """
    lmcache_offload = get_lmcache_env()
    pd_role = get_pd_role_env()

    if lmcache_offload and pd_role:
        config = {
            "kv_connector": 'MultiConnector',
            "kv_role": "kv_both",
            "kv_connector_extra_config": {
                "connectors": [
                    _get_pd_config(ctx, pd_role),
                    {"kv_connector": "LMCacheConnectorV1", "kv_role": "kv_both"}
                ]
            }
        }
        logger.info(f"[KVCache Offload] KVCache Offload feature is enabled and PD role is {pd_role}")
    elif lmcache_offload:
        config = {
            "kv_connector": 'LMCacheConnectorV1',
            "kv_role": "kv_both"
        }
        logger.info(f"[KVCache Offload] KVCache Offload feature is enabled")
    elif pd_role:
        config = _get_pd_config(ctx, pd_role)
        logger.info(f"PD role is {pd_role}")
    else:
        return  #

    params['kv_transfer_config'] = json.dumps(config)


def _set_router_config(params):
    """当 Wings Router 路由功能启用时，注入 KV 事件 NATS 发布配置。

    Wings Router 依赖 NATS 消息队列来感知各实例的 KV Cache 命中情况，
    从而做智能路由。此函数将 NATS 发布配置序列化为 JSON 并写入 params。
    """
    router_enable = get_router_env()
    router_instance_group_name = get_router_instance_group_name_env()

    if not router_enable or not router_instance_group_name:
        return

    router_instance_name = get_router_instance_name_env()
    router_nats_path = get_router_nats_path_env()

    kv_events_config = json.dumps({
        "enable_kv_cache_events": True,
        "publisher": "nats",
        "instance_id": f"{router_instance_group_name}:{router_instance_name}",
        "nats_servers": router_nats_path
    })

    params['kv_events_config'] = kv_events_config
    logger.info(f"Wings Router for vllm is enabled")


def _merge_mindie_params(params, ctx, engine_cmd_parameter):
    """将通用参数合并为 MindIE config.json 所要求的字段格式。

    - 通过 mindie 参数映射表翻译 CLI 参数名；
    - 自动检测模型目录中是否含 mtp.safetensors（MTP 特性）；
    - 自动识别 MOE 模型（如 DeepSeek-R1-671B）；
    - 计算 maxSeqLen / maxPrefillTokens；
    - 分布式场景下设置 worldSize / npuDeviceIds，并禁用 multiNodesInferEnabled。
    """
    #
    engine_param_map_config_path = os.path.join(DEFAULT_CONFIG_DIR,
                                            DEFAULT_CONFIG_FILES.get("engine_parameter_mapping"))
    mindie_param_map_config = _load_mapping(engine_param_map_config_path, 'default_to_mindie_parameter_mapping')
    for key, value in mindie_param_map_config.items():
        if not value or engine_cmd_parameter.get(key) is None:
            continue
        else:
            params[value] = engine_cmd_parameter.get(key)

    #
    is_mtp = False
    is_moe = False

    # mtp.safetensors
    if engine_cmd_parameter["model_path"] and os.path.exists(engine_cmd_parameter["model_path"]):
        mtp_file = os.path.join(engine_cmd_parameter["model_path"], "mtp.safetensors")
        is_mtp = os.path.exists(mtp_file)

    # MOE
    moe_models = ["deepseek-r1-671b"]  # MOE
    if engine_cmd_parameter["model_name"].lower() in moe_models or params.get("enable_ep_moe"):
        is_moe = True

    #
    params.update({
        'isMTP': is_mtp,
        'isMOE': is_moe
    })

    #
    if engine_cmd_parameter["input_length"] and engine_cmd_parameter["output_length"]:
        params.update({
            'maxSeqLen': engine_cmd_parameter["input_length"] + engine_cmd_parameter["output_length"],
            'maxPrefillTokens': max(8192, engine_cmd_parameter["input_length"])
        })

    # ── distributed / single-node worldSize + npuDeviceIds ──────────────────
    if ctx.get('distributed'):
        node_ips = get_node_ips()
        # MindIE config.json worldSize = LOCAL TP degree (devices per node).
        # Each MindIE daemon runs independently with TP on local devices.
        # Cross-node coordination (DP) is handled by wings-infer sidecar.
        # npuDeviceIds lists only LOCAL device IDs for this node.
        params['worldSize'] = int(ctx["device_count"])
        # multiNodesInferEnabled must be false for individual daemon instances.
        # When true, ConfigManager auto-updates worldSize to total_ranks which
        # causes "Invalid DP number per node: 0".  Multi-node coordination
        # is handled by ms_coordinator/ms_controller at a higher level.
        params['multiNodesInferEnabled'] = False
        params['node_ips'] = node_ips  # Pass node IPs to adapter for HCCL_IF_IP
        params['npuDeviceIds'] = [[i for i in range(ctx["device_count"])]]
    else:
        _adjust_tensor_parallelism(params, ctx["device_count"], 'worldSize')
        params['npuDeviceIds'] = [[i for i in range(ctx["device_count"])]]
    return params


def _merge_sglang_params(params, ctx, engine_cmd_parameter):
    """将通用参数合并为 SGLang 启动参数格式。

    - 通过 sglang 参数映射表翻译 CLI 参数名；
    - 注意：sglang 的 enable_prefix_caching 语义与 vllm 相反，因此需要取反；
    - 合并 input_length + output_length 为 context_length；
    - 设置张量并行度（tp_size）；
    - sglang 4.10.0+ 中 --enable-ep-moe 已废弃，改为 ep_size = tp_size。
    """
    #
    engine_param_map_config_path = os.path.join(DEFAULT_CONFIG_DIR,
                                            DEFAULT_CONFIG_FILES.get("engine_parameter_mapping"))
    sglang_param_map_config = _load_mapping(engine_param_map_config_path, 'default_to_sglang_parameter_mapping')
    for key, value in sglang_param_map_config.items():
        if not value or engine_cmd_parameter.get(key) is None:
            continue
        else:
            params[value] = engine_cmd_parameter.get(key)
            # sglangvllm
            if key == "enable_prefix_caching":
                params[value] = not engine_cmd_parameter.get(key)

    #
    if engine_cmd_parameter["input_length"] and engine_cmd_parameter["output_length"]:
        params['context_length'] = engine_cmd_parameter["input_length"] + engine_cmd_parameter["output_length"]

    #
    _adjust_tensor_parallelism(params, ctx["device_count"], 'tp_size', ctx['distributed'])

    # sglang 4.10.0--enable-ep-moe is deprecated
    if "enable_ep_moe" in params:
        params.pop("enable_ep_moe")
        params['ep_size'] = params['tp_size']
    return params


def _adjust_tensor_parallelism(params, device_count, tp_key, if_distributed=False):
    """设置张量并行度（TP）参数。

    - 非分布式模式：TP = 当前节点设备数
    - 分布式模式（非 PD）：TP = 设备数 × 节点数，实现全局 TP
    - 若已有用户设置则不覆盖
    """
    default_tp = params.get(tp_key)
    if default_tp:
        return
    if not if_distributed:
        if default_tp is not None and default_tp != device_count:
            logger.warning(
                "Detected %s devices in current environment, "
                "while default recommended TP is %s, "
                "will use device count as final TP value",
                device_count, default_tp
            )
        params[tp_key] = int(device_count)
    else:
        node_ips = get_node_ips()
        n_nodes = len([ip.strip() for ip in node_ips.split(',')]) if node_ips else 1
        # PD+
        if get_pd_role_env():
            params[tp_key] = int(device_count)
        else:
            params[tp_key] = int(device_count) * n_nodes


def _merge_configs(*configs: Dict[str, Any]) -> Dict[str, Any]:
    """深度合并多个配置字典（后者覆盖前者，嵌套 dict 递归合并）。

    合并规则：
    - 若同一个 key 在多个字典中都是 dict，则递归合并；
    - 否则后续字典的值直接覆盖之前的值。

    Args:
        *configs: 任意数量的字典，按顺序从低优先级到高优先级传入

    Returns:
        Dict[str, Any]: 深度合并后的字典
    """
    merged = {}
    for config in configs:
        if not isinstance(config, dict):
            continue #

        for key, value in config.items():
            if isinstance(value, dict) and key in merged and isinstance(merged[key], dict):
                #
                merged[key] = _merge_configs(merged[key], value)
            else:
                #
                merged[key] = value
    return merged


def _load_default_config(hardware_env: Dict[str, Any]) -> Dict[str, Any]:
    """根据硬件类型（nvidia/ascend）加载对应的默认引擎配置文件。

    加载策略：
      1. 优先加载 vllm_default.json（新版统一配置）
      2. 若 vllm_default.json 不存在 → 回退到 <device>_default.json（旧版布局）
      3. 若 vllm_default.json 存在但缺少 model_deploy_config →
         尝试从旧版 <device>_default.json 中补充 model_deploy_config（兼容旧配置）

    兼容说明：
      旧版使用 nvidia_default.json / ascend_default.json，其中包含按模型细分的
      model_deploy_config 段落。新版统一使用 vllm_default.json，但如果部署环境
      中同时存在旧版配置文件，会自动合并其中的 model_deploy_config 到新版配置中。
    """
    device_key = 'device'
    device_type = hardware_env.get(device_key, "nvidia")
    if device_type not in SUPPORTED_DEVICE_TYPES:
        logger.warning("Unsupported device type '%s', fallback to 'nvidia'", device_type)
        device_type = "nvidia"
    default_file = DEFAULT_CONFIG_FILES.get(device_type)
    default_config_path = os.path.join(DEFAULT_CONFIG_DIR, default_file)
    if not os.path.exists(default_config_path) and default_file == "vllm_default.json":
        legacy_file = f"{device_type}_default.json"
        legacy_path = os.path.join(DEFAULT_CONFIG_DIR, legacy_file)
        if os.path.exists(legacy_path):
            logger.warning("Fallback to legacy default config: %s", legacy_path)
            default_config_path = legacy_path
    logger.info(f"Determined default config file for hardware environment '{device_type}': {default_config_path}")
    config = load_json_config(default_config_path)

    # 兼容旧版：若主配置缺少 model_deploy_config，尝试从旧版设备配置文件中补充
    if "model_deploy_config" not in config and default_file == "vllm_default.json":
        legacy_file = f"{device_type}_default.json"
        legacy_path = os.path.join(DEFAULT_CONFIG_DIR, legacy_file)
        if os.path.exists(legacy_path):
            legacy_config = load_json_config(legacy_path)
            if "model_deploy_config" in legacy_config:
                config["model_deploy_config"] = legacy_config["model_deploy_config"]
                logger.info(
                    "Supplemented model_deploy_config from legacy config: %s",
                    legacy_path,
                )
    return config


def _load_engine_fallback_defaults(engine: str) -> Dict[str, Any]:
    """加载特定引擎的兜底默认配置（sglang_default.json / mindie_default.json）。

    当 vllm_default.json 或 model_deploy_config 中没有该引擎的专属配置项时，
    从引擎专属默认文件加载参数。vllm/vllm_ascend 复用公共默认配置，无需此步骤。
    """
    fallback_file = DEFAULT_CONFIG_FILES.get(engine)
    if not fallback_file:
        logger.debug("No engine-level fallback config for engine='%s'", engine)
        return {}
    path = os.path.join(DEFAULT_CONFIG_DIR, fallback_file)
    if not os.path.exists(path):
        logger.warning(
            "Engine fallback config '%s' not found at '%s'; using empty defaults",
            fallback_file, path,
        )
        return {}
    cfg = load_json_config(path)
    logger.info("Loaded engine-level fallback defaults from '%s'", path)
    return cfg


def _load_user_config(config) -> Dict[str, Any]:
    """加载用户自定义 JSON 配置文件，合并到默认配置之上。

    Args:
        config: 配置来源，支持两种格式：
            - 文件路径字符串（指向 JSON 文件）
            - 已反序列化的 JSON 字典对象
    """
    user_config = {}
    if not config:
        return user_config

    if config.strip().startswith('{') and config.strip().endswith('}'):
        # JSON
        try:
            user_config = json.loads(config)
            logger.info("Successfully parsed config from JSON string")
            return user_config
        except json.JSONDecodeError:
            logger.info("The config-file is not JSON string, will load it as a file")
    elif os.path.exists(config):
        #
        logger.info(f"Loading user-specified config file: {config}")
        user_config = load_json_config(config)
    else:
        logger.warning(f"User-specified config not found or invalid: {config}")

    return user_config


def _process_cmd_args(known_args: argparse.Namespace) -> Dict[str, Any]:
    """将 argparse.Namespace 转为字典，过滤掉 None 值和 config_file 键。

    config_file 由 _load_user_config 单独处理，不参与引擎参数合并。
    """
    cmd_known_params = {k: v for k, v in vars(known_args).items() if v is not None and k not in ["config_file"]}
    return cmd_known_params


def _write_engine_second_line(path: str, engine: str) -> None:
    """将引擎名称写入标记文件的第 2 行。

    标记文件（如 /var/log/wings/wings.txt）用于进程间通信和运维排查。
    第 1 行预留给 PID，第 2 行记录当前使用的引擎名称。
    文件不存在时自动创建。
    """
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        lines = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()

        if len(lines) == 0:
            lines = ["", engine]
        elif len(lines) == 1:
            lines.append(engine)
        else:
            lines[1] = engine

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        logger.warning("Write engine marker file failed (%s): %s", path, e)



def _auto_select_engine(hardware_env: Dict[str, Any],
                       cmd_known_params: Dict[str, Any],
                       model_info) -> Dict[str, Any]:
    """根据硬件环境和模型特征自动选择推理引擎，并完成引擎相关的全局初始化。

    核心职责：
    1. 若用户未指定 engine → 调用 _select_engine_automatically 自动选择
    2. 若用户已指定 engine → 调用 _validate_user_engine 校验兼容性（可能降级）
    3. MindIE 引擎需确保 config.json 权限为 640
    4. 将最终引擎名写入标记文件和 WINGS_ENGINE 环境变量
    5. 在 Ascend 设备上将 vllm 自动升级为 vllm_ascend
    6. 根据 gpu_usage_mode 确定最终 device_count

    Args:
        hardware_env:     硬件探测结果（device, count, details）
        cmd_known_params: 用户 CLI 参数字典（会被原地修改）
        model_info:       模型元信息对象

    Returns:
        修改后的 cmd_known_params（加入 engine, model_type, device_count）
    """
    device_type = hardware_env['device']
    if hardware_env.get('details'):
        device_name = hardware_env.get('details')[0]['name']
    else:
        device_name = 'unknown'
    gpu_usage_mode = cmd_known_params.get("gpu_usage_mode", "full")

    # 引擎选择：用户未指定时自动选择，已指定时校验兼容性
    if not cmd_known_params.get("engine"):
        engine = _select_engine_automatically(device_type, device_name,
                                              gpu_usage_mode, model_info)
    else:
        engine = _validate_user_engine(cmd_known_params.get("engine"),
                                       device_name, gpu_usage_mode, model_info)

    # MindIE 引擎要求 config.json 权限为 640；Ascend310 还需检查 torch_dtype
    if engine == 'mindie':
        config_json_file = os.path.join(cmd_known_params.get('model_path'), 'config.json')
        if not check_permission_640(config_json_file):
            try:
                os.chmod(config_json_file, 0o640)
                logger.info("The permission setting for model config.json is not set to 640. " \
                "Since MindIE only supports the 640 permission configuration, we will adjust it to 640.")
            except Exception as e:
                logger.warning(f"Failed to set permission for model config.json to 640: {e}.")
        if "310" in device_name:
            check_torch_dtype(config_json_file)

    # 回写模型类型到参数字典，供下游引擎适配器判断
    cmd_known_params["model_type"] = model_info.identify_model_type()

    # 回写最终选定的引擎名称
    cmd_known_params["engine"] = engine
    _write_engine_second_line(os.getenv("BACKEND_PID_FILE", "/var/log/wings/wings.txt"), engine)


    # 将 engine 写入全局环境变量，供 gateway.py 等其他模块读取
    os.environ['WINGS_ENGINE'] = engine
    logger.info(f"Set global environment variable WINGS_ENGINE={engine}")

    # 在昇腾设备上将 vllm 自动升级为 vllm_ascend
    if engine == "vllm":
        _handle_ascend_vllm(device_type, cmd_known_params)

    # 分布式参数注入（distributed_executor_backend, ray/nixl/dist_port 等）
    if cmd_known_params.get("distributed"):
        _handle_distributed(engine, cmd_known_params, model_info)

    # 确定最终设备数量：full 模式使用硬件探测值，其他模式使用用户指定值
    # device_count
    if cmd_known_params.get("gpu_usage_mode") == "full":
        device_count = hardware_env.get("count", 1)
    else:
        device_count = cmd_known_params.get("device_count", 1)
    # 设备数量合法性校验
    if device_count <= 0:
        raise ValueError(f"device_count must be an integer greater than 0. Current value: {device_count}")
    cmd_known_params['device_count'] = device_count

    return cmd_known_params


def _select_engine_automatically(device_type: str,
                                 device_name: str,
                                 gpu_usage_mode: str,
                                 model_info) -> str:
    """根据设备类型、模型特征自动选择最合适的推理引擎。"""
    if device_type == "nvidia":
        return _select_nvidia_engine(gpu_usage_mode, model_info)
    elif device_type == "ascend":
        return _select_ascend_engine(device_name, model_info)
    else:
        logger.info("No engine specified, automatically selected engine: vllm")
        return 'vllm'


def _select_nvidia_engine(gpu_usage_mode: str, model_info) -> str:
    """NVIDIA GPU 场景下的引擎自动选择逻辑。

    优先级（由高到低）：
    1. 启用 KVCache Offload → vllm
    2. 启用 PD 分离 → vllm
    3. 启用 Wings Router → vllm
    4. MIG 模式 → vllm
    5. embedding/rerank/mmum 模型 → vllm
    6. mmgm 多模态 → wings（保留）
    7. wings 已验证模型 → sglang（推荐高性能路径）
    8. 其他未验证架构 → vllm（兜底）
    """
    model_architecture = model_info.model_architecture
    model_type = model_info.identify_model_type()
    is_wings_supported = model_info.is_wings_supported()
    vllm = 'vllm'
    if get_lmcache_env():
        logger.info("[KVCache Offload] KVCache Offload enabled, automatically switched to VLLM engine")
        return vllm
    elif get_pd_role_env():
        logger.info("PD enabled, automatically switched to VLLM engine")
        return vllm
    elif get_router_env():
        logger.info("Wings router enabled, automatically switched to VLLM engine")
        return vllm
    elif gpu_usage_mode == "mig":
        logger.info("Device is Mig, automatically switched to VLLM engine")
        return vllm
    elif model_type in ["embedding", "rerank", "mmum"]:
        logger.info(f"model type is {model_type}, automatically switched to VLLM engine")
        return vllm
    elif model_type == "mmgm":
        logger.info(f"model type is {model_type}, automatically switched to wings engine")
        return 'wings'
    elif is_wings_supported:
        logger.info("No engine specified, automatically selected engine: sglang")
        return 'sglang'
    else:
        logger.warning(f"This model architecture {model_architecture} has not been validated on Wings."
                       "automatically switched to VLLM engine")
        return vllm


def _select_ascend_engine(device_name: str, model_info) -> str:
    """华为昇腾 NPU 场景下的引擎自动选择逻辑。

    优先级（由高到低）：
    1. Ascend310 → 强制 mindie（vllm_ascend 不支持 310 系列）
    2. embedding / rerank 模型 → vllm_ascend
    3. mmgm 多模态模型 → wings
    4. 算子加速（USE_KUNLUN_ATB）启用 → vllm_ascend
    5. LMCache KV Offload 启用 → vllm_ascend
    6. Wings Router 启用 → vllm_ascend
    7. Soft FP8 量化启用 → vllm_ascend
    8. Wings 已验证模型 → mindie（Ascend 上的推荐引擎）
    9. 未验证架构 → vllm_ascend（兜底）

    Args:
        device_name: 设备型号名称，含 '310' 表示昇腾 310 系列
        model_info:  模型元信息对象

    Returns:
        str: 选定的引擎名称 'mindie' 或 'vllm_ascend'

    Raises:
        ValueError: 昇腾 310 不支持 embedding/rerank 模型
    """
    model_architecture = model_info.model_architecture
    model_type = model_info.identify_model_type()
    is_wings_supported = model_info.is_wings_supported()
    if "310" in device_name:
        if model_type in ["embedding", "rerank"]:
            raise ValueError(f"Ascend310 not support {model_type} model currenly")
        logger.info("Ascend310 not support vllm ascend, automatically selected engine: mindie")
        return 'mindie'
    elif model_type in ["embedding", "rerank"]:
        logger.info(f"model type is {model_type}, automatically switched to VLLM engine")
        return "vllm_ascend"
    elif model_type == "mmgm":
        logger.info(f"model type is {model_type}, automatically switched to wings engine")
        return 'wings'
    elif get_operator_acceleration_env():
        logger.warning(f"operator_acceleration is enabled,\
                        automatically switched to VLLM_Ascend engine")
        return "vllm_ascend"
    elif get_lmcache_env():
        logger.info("[KVCache Offload] KVCache Offload enabled, automatically switched to VLLM_Ascend engine")
        return "vllm_ascend"
    elif get_router_env():
        logger.info("Wings router enabled, automatically switched to VLLM engine")
        return "vllm_ascend"
    elif get_soft_fp8_env():
        logger.warning(f"soft fp8 is enabled, "
                       "automatically switched to VLLM_Ascend engine")
        return "vllm_ascend"
    elif is_wings_supported:
        logger.info("No engine specified, automatically selected engine: mindie")
        return 'mindie'
    else:
        logger.warning(f"This model architecture {model_architecture} has not been validated on Wings."
                       "automatically switched to VLLM_Ascend engine")
        return "vllm_ascend"


def _validate_user_engine(engine: str, device_name: str, gpu_usage_mode: str, model_info) -> str:
    """校验用户指定的引擎并在不兼容时自动降级到兼容引擎。

    参数:
        engine (str): 用户指定引擎，支持 'mindie', 'vllm', 'vllm_ascend', 'sglang'
        device_name (str): 设备型号名称（含 '310' 时为昇腾 310 系列）
        gpu_usage_mode (str): GPU 使用模式（'mig' 等）
        model_info: 模型信息对象

    返回:
        str: 实际使用的引擎名（可能因兼容性降级而与输入不同）

    异常:
        ValueError: 引擎名不在支持列表中时抛出
    """
    #
    if engine not in ['mindie', 'vllm', 'vllm_ascend', 'sglang', 'wings']:
        raise ValueError(f"The engine {engine} is not supported yet!Please change to 'mindie', 'vllm' or 'sglang'")

    vllm = 'vllm'
    model_type = model_info.identify_model_type()
    #
    if engine == 'mindie':
        # 310mindie
        if "310" in device_name:
            return 'mindie'
        # LMCachevllm_ascend
        elif get_lmcache_env():
            logger.warning("[KVCache Offload] KVCache Offload enabled, automatically switched to VLLM_Ascend engine")
            return "vllm_ascend"
        # embeddingrerankvllm_ascend
        elif model_type in ["embedding", "rerank"]:
            logger.warning(f"model type is {model_type}, automatically switched to VLLM_Ascend engine")
            return "vllm_ascend"
        elif get_router_env():
            logger.warning("Wings router enabled, automatically switched to VLLM engine")
            return vllm
        # QwenQwQvllm_ascend
        elif get_operator_acceleration_env():
            logger.warning(f"operator_acceleration is enabled, \
                            automatically switched to VLLM_Ascend engine")
            return "vllm_ascend"
    elif engine == 'sglang':
        if get_lmcache_env():
            logger.warning("[KVCache Offload] KVCache Offload enabled, automatically switched to VLLM engine")
            return vllm
        elif get_pd_role_env():
            logger.warning("PD enabled, automatically switched to VLLM engine")
            return vllm
        elif get_router_env():
            logger.warning("Wings router enabled, automatically switched to VLLM engine")
            return vllm
        elif gpu_usage_mode == "mig":
            logger.warning("Device is Mig, automatically switched to VLLM engine")
            return vllm
        elif model_type in ["embedding", "rerank"]:
            logger.warning(f"model type is {model_type}, automatically switched to VLLM engine")
            return vllm
    return engine


def _handle_mindie_distributed(distributed_config: Dict[str, Any], cmd_params: Dict[str, Any]):
    """注入 MindIE 多节点分布式所需的 MASTER_ADDR / MASTER_PORT。

    从分布式配置文件读取 master 通信端口，并通过 get_master_ip() 获取当前节点 IP，
    将结果写入 cmd_params，供 mindie_adapter 构建启动脚本时使用。
    """
    mindie_cfg = distributed_config.get('mindie_distributed', {})
    master_port = mindie_cfg.get('master_port', 27070)
    cmd_params.update({
        'mindie_master_addr': get_master_ip(),
        'mindie_master_port': master_port,
    })


def _handle_distributed(engine: str, cmd_params: Dict[str, Any], model_info):
    """根据引擎类型将分布式参数注入 cmd_params。

    从默认配置目录加载 distributed.json，并根据 engine 分发到对应的处理函数：
    - vllm / vllm_ascend → _handle_vllm_distributed（Ray 或 PD 模式）
    - sglang             → _handle_sglang_distributed（dist_port）
    - mindie             → _handle_mindie_distributed（MASTER_ADDR/PORT）
    """
    distributed_config_path = os.path.join(
        DEFAULT_CONFIG_DIR,
        DEFAULT_CONFIG_FILES.get("distributed")
    )
    distributed_config = load_json_config(distributed_config_path)

    # vllmvllm_ascend
    if engine in ['vllm', 'vllm_ascend']:
        _handle_vllm_distributed(distributed_config, cmd_params, model_info)

    # sglang
    elif engine == 'sglang':
        _handle_sglang_distributed(distributed_config, cmd_params)

    # mindie
    elif engine == 'mindie':
        _handle_mindie_distributed(distributed_config, cmd_params)


def _handle_vllm_distributed(distributed_config: Dict[str, Any], cmd_params: Dict[str, Any], model_info):
    """为 vLLM / vLLM-Ascend 配置分布式推理参数。

    - 若 PD 角色（Prefill/Decode）或 Ascend DeepSeek 模型：使用 NIXL 协议（dp_deployment）。
    - 否则：使用 Ray 作为分布式执行后端，设置 ray_head_ip / ray_head_port。

    端口优先来自环境变量 VLLM_DISTRIBUTED_PORT，若未设置则回退到配置文件默认值。
    """
    vllm_distributed_port = get_vllm_distributed_port()
    pd_role = get_pd_role_env()
    model_architecture = model_info.model_architecture
    is_ascend_deepseek = (model_architecture == "DeepseekV3ForCausalLM" \
                          and cmd_params.get("engine") == 'vllm_ascend')

    if pd_role in ['P', 'D'] or is_ascend_deepseek:
        # PD
        if not vllm_distributed_port:
            vllm_distributed_port = distributed_config['vllm_distributed']['nixl_port']

        rpc_port = distributed_config['vllm_distributed']['rpc_port']
        cmd_params.update({
            'distributed_executor_backend': 'dp_deployment',
            'nixl_ip': get_local_ip(),
            'nixl_port': vllm_distributed_port,
            'rpc_port': rpc_port
        })
    else:
        #
        if not vllm_distributed_port:
            vllm_distributed_port = distributed_config['vllm_distributed']['ray_head_port']

        cmd_params.update({
            'distributed_executor_backend': 'ray',
            'ray_head_ip': get_master_ip(),
            'ray_head_port': vllm_distributed_port
        })


def _handle_sglang_distributed(distributed_config: Dict[str, Any], cmd_params: Dict[str, Any]):
    """为 SGLang 配置分布式通信端口 dist_port。

    端口优先来自环境变量 SGLANG_DISTRIBUTED_PORT，
    若未设置则回退到 distributed.json 中 sglang_distributed.dist_port 的默认值。
    """
    dist_port = get_sglang_distributed_port()
    if not dist_port:
        dist_port = distributed_config['sglang_distributed']['dist_port']

    cmd_params.update({
        'dist_port': dist_port
    })


def _handle_ascend_vllm(device_type: str, cmd_params: Dict[str, Any]):
    """在 Ascend 设备上将引擎名称从 vllm 升级为 vllm_ascend。

    当硬件为昇腾 NPU 且用户指定引擎为 vllm 时，
    自动将 cmd_params['engine'] 替换为 vllm_ascend，
    以便后续使用昇腾专用的适配器和参数表。
    """
    if device_type == "ascend" and cmd_params.get("engine") == "vllm":
        cmd_params["engine"] = "vllm_ascend"


def _get_model_specific_config(hardware_env: Dict[str, Any],
                             cmd_known_params: Dict[str, Any],
                             model_info) -> Dict[str, Any]:
    """获取并合并模型专属的默认部署配置。

    查找链路（优先级从高到低）：
    1. model_deploy_config[model_type][model_architecture][model_name] —— 精确匹配
    2. model_deploy_config[model_type][model_architecture]['default']  —— 架构级默认
    3. model_deploy_config[model_type]['default']                      —— 模型类型默认
    4. _load_engine_fallback_defaults(engine)                          —— 引擎兜底默认

    对于 DeepSeek 模型在 NVIDIA+SGLang 场景，额外支持按 H20 卡型选配（H20-96G / H20-141G）。
    最终通过 _merge_cmd_params 将硬件参数、模型默认值和用户 CLI 参数三层合并。

    Args:
        hardware_env:     硬件环境信息（device, gpu_memory 等）。
        cmd_known_params: 用户 CLI 已知参数（model_name, engine, distributed 等）。
        model_info:       模型元信息对象，提供 identify_model_architecture / type 方法。

    Returns:
        合并后的最终参数字典，可直接传递给各引擎适配器。
    """
    device_key = 'device'
    sglang_name = 'sglang'
    default_key = "default"
    config_model_key = "model_deploy_config"
    model_name = cmd_known_params.get("model_name")
    model_name_lower = model_name.lower()
    engine = cmd_known_params.get("engine")
    engine_key = f"{engine}_distributed" if cmd_known_params.get("distributed") else engine
    engine_specific_defaults = {}

    model_architecture = model_info.identify_model_architecture()
    model_type = model_info.identify_model_type()

    default_config = _load_default_config(hardware_env)
    model_deploy_config = default_config.get(config_model_key, {})
    if not isinstance(model_deploy_config, dict):
        logger.warning("Invalid default config structure: %s is not a dict", config_model_key)
        model_deploy_config = {}
    models_dict = model_deploy_config.get(model_type, {})
    if not isinstance(models_dict, dict):
        logger.warning("Invalid model config structure: model_type=%s is not a dict", model_type)
        models_dict = {}
    if not models_dict:
        logger.warning(
            "No model_deploy_config found for model_type=%s (engine=%s),"
            " try engine-level fallback defaults",
            model_type,
            engine,
        )
        engine_specific_defaults = _load_engine_fallback_defaults(engine)
        return _merge_cmd_params(hardware_env, engine_specific_defaults, cmd_known_params, model_info)

    if model_architecture in models_dict:
        model_architecture_dict = models_dict[model_architecture]

        # DeepSeek
        is_deepseek_sglang_nvidia = (
            model_architecture == "DeepseekV3ForCausalLM"
            and hardware_env[device_key] == "nvidia"
            and engine == sglang_name
            and not cmd_known_params.get("distributed")
        )

        #  H20
        h20_model = _get_h20_model_hint()

        for model, config in model_architecture_dict.items():
            if model_name_lower != model.lower():
                continue

            if is_deepseek_sglang_nvidia and h20_model in ["H20-96G", "H20-141G"]:
                engine_specific_defaults = config.get(engine_key, {}).get(h20_model, {})
                logger.info(f"Using dedicated config for model '{model_name}' on {h20_model}")
            elif not is_deepseek_sglang_nvidia:
                engine_specific_defaults = config.get(engine_key, {})
                logger.info(f"The default deploy configuration "
                            f"of the model architecture {model_architecture} will be used.")
            break
        if not engine_specific_defaults:
            logger.info(f"The default deploy configuration of the "
                        f"model architecture {model_architecture} will be used.")
            engine_specific_defaults = model_architecture_dict.get(default_key, {}).get(engine_key, {})
    else:
        engine_specific_defaults = models_dict.get(default_key, {}).get(engine_key, {})
        logger.info(f"The default deploy configuration of the model type {model_type} will be used.")

    engine_specific_defaults = _merge_cmd_params(hardware_env, engine_specific_defaults, cmd_known_params, model_info)
    return engine_specific_defaults


def _merge_final_config(engine_config: Dict[str, Any],
                       cmd_known_params: Dict[str, Any]) -> Dict[str, Any]:
    """将引擎专属配置包装进最终参数字典并返回。

    把 engine_config（引擎参数子集）挂载到 cmd_known_params['engine_config'] 键下，
    作为后续引擎适配器（vllm_adapter / sglang_adapter 等）的输入。

    Args:
        engine_config:    引擎专属参数字典（由 _get_model_specific_config 生成）。
        cmd_known_params: 用户 CLI 已知参数字典（将原地修改并返回）。

    Returns:
        追加了 engine_config 字段的 cmd_known_params 字典。
    """
    cmd_known_params['engine_config'] = engine_config

    return cmd_known_params

# ==============================  MMGM/Wings  ==============================


def _autodiscover_hunyuan_paths(model_path_root: str) -> Dict[str, str]:
    """
    HunyuanVideo
    - dit_weight:   <var>/transformers/mp_rank_00_model_states.pt
                     mp_rank_00_model_states.pt  pytorch_model_*.pt
    - vae_path:     <var>/vae 'vae'
    - text_encoder_path:       <root>/text_encoder 'text_encoder*'
    - text_encoder_2_path:     <root>/clip-vit-large-patch14 'text_encoder_2'  'clip-vit-large'
    """
    base = Path(model_path_root).expanduser().resolve()
    if not base.exists():
        raise ValueError(f"[MMGM] model_path not exists: {base}")

    vardir = _find_variant_directory(base)

    return {
        "dit_weight": _find_dit_weight(base, vardir),
        "vae_path": _find_vae_path(base, vardir),
        "text_encoder_path": _find_text_encoder_path(base),
        "text_encoder_2_path": _find_text_encoder_2_path(base),
    }


def _find_variant_directory(base_path):
    """查找 HunyuanVideo 变体目录（720p/540p）。

    在模型根目录下查找预设的变体目录，用于后续加载 DIT/VAE 权重。

    Args:
        base_path: 模型根目录的 Path 对象

    Returns:
        Path | None: 找到的变体目录或 None
    """
    if (base_path / "hunyuan-video-t2v-720p").is_dir():
        return base_path / "hunyuan-video-t2v-720p"
    elif (base_path / "hunyuan-video-t2v-540p").is_dir():
        return base_path / "hunyuan-video-t2v-540p"
    return None


def _find_dit_weight(base_path, variant_dir):
    """查找 HunyuanVideo DIT 权重文件路径。

    搜索顺序:
        1. 变体目录下的 transformers/mp_rank_00_model_states.pt
        2. 递归搜索根目录下的 mp_rank_00_model_states.pt 或 pytorch_model_*.pt

    Args:
        base_path:   模型根目录
        variant_dir: 变体目录（可为 None）

    Returns:
        str: 找到的 DIT 权重文件路径，未找到时返回空字符串
    """

    #
    if variant_dir and (variant_dir / "transformers" / "mp_rank_00_model_states.pt").is_file():
        return str((variant_dir / "transformers" / "mp_rank_00_model_states.pt").resolve())

    #
    for root, _, files in os.walk(str(base_path)):
        for fn in files:
            if fn == "mp_rank_00_model_states.pt" or fn.startswith("pytorch_model_"):
                return str((Path(root) / fn).resolve())
    return ""


def _find_vae_path(base_path, variant_dir):
    """查找 HunyuanVideo VAE 目录路径。

    搜索顺序:
        1. 变体目录下的 vae/ 子目录
        2. 递归搜索根目录下的 vae/ 目录

    Args:
        base_path:   模型根目录
        variant_dir: 变体目录（可为 None）

    Returns:
        str: 找到的 VAE 目录路径，未找到时返回空字符串
    """

    #
    if variant_dir and (variant_dir / "vae").is_dir():
        return str((variant_dir / "vae").resolve())

    #
    for root, dirs, _ in os.walk(str(base_path)):
        if "vae" in dirs:
            return str((Path(root) / "vae").resolve())
    return ""


def _find_text_encoder_path(base_path):
    """查找 HunyuanVideo 文本编码器 1 目录路径。

    搜索顺序:
        1. 根目录下的 text_encoder/ 子目录
        2. 递归搜索根目录下的 text_encoder* 目录

    Args:
        base_path: 模型根目录

    Returns:
        str: 找到的文本编码器目录路径，未找到时返回空字符串
    """

    #
    if (base_path / "text_encoder").is_dir():
        return str((base_path / "text_encoder").resolve())

    #
    for root, dirs, _ in os.walk(str(base_path)):
        for d in dirs:
            if d.startswith("text_encoder"):
                return str((Path(root) / d).resolve())
    return ""


def _find_text_encoder_2_path(base_path):
    """查找 HunyuanVideo 文本编码器 2 (CLIP-ViT-Large) 目录路径。

    搜索顺序:
        1. 根目录下的 clip-vit-large-patch14/ 子目录
        2. 根目录下的 text_encoder_2/ 子目录
        3. 递归搜索根目录下的 text_encoder_2 或 clip-vit-large 目录

    Args:
        base_path: 模型根目录

    Returns:
        str: 找到的文本编码器 2 目录路径，未找到时返回空字符串
    """

    #
    if (base_path / "clip-vit-large-patch14").is_dir():
        return str((base_path / "clip-vit-large-patch14").resolve())

    #
    if (base_path / "text_encoder_2").is_dir():
        return str((base_path / "text_encoder_2").resolve())

    #
    for root, dirs, _ in os.walk(str(base_path)):
        for d in dirs:
            if d == "text_encoder_2" or "clip-vit-large" in d:
                return str((Path(root) / d).resolve())
    return ""


def _build_mmgm_engine_defaults(cmd_known_params: Dict[str, Any],
                                hardware_env: Dict[str, Any]) -> Dict[str, Any]:
    """
     mmgm + wings  engine_config
    -  device
    -  --save-path  engine_config.save_path outputs/
    - / HYV_*  HunyuanVideo
    -  flow_reverse
    """
    #  wings_adapter nvidia/ascend
    device = hardware_env.get("device")

    #  HYV_MODEL_BASE
    model_path = cmd_known_params.get("model_path")
    if not model_path:
        raise ValueError("[MMGM] model_path (or HYV_MODEL_PATH) is required for mmgm+wings.")

    #
    discovered = _autodiscover_hunyuan_paths(model_path)

    #  shell
    dit_weight = discovered["dit_weight"]
    vae_path = discovered["vae_path"]
    te_path = discovered["text_encoder_path"]
    te2_path = discovered["text_encoder_2_path"]

    #
    missing = []

    def _must_exist(path: str, kind: str, key: str):
        if not path:
            missing.append(f"{key}({kind})")
            return
        if kind == "dir" and not os.path.isdir(path):
            missing.append(f"{key}({kind}='{path}')")
        if kind == "file" and not os.path.isfile(path):
            missing.append(f"{key}({kind}='{path}')")

    _must_exist(model_path, "dir", "model_path")
    _must_exist(dit_weight, "file", "dit_weight")
    _must_exist(vae_path, "dir", "vae_path")
    _must_exist(te_path, "dir", "text_encoder_path")
    _must_exist(te2_path, "dir", "text_encoder_2_path")

    if missing:
        raise ValueError("[MMGM] Required HunyuanVideo paths not found: " + ", ".join(missing))

    #  --save-path save_path
    save_path = cmd_known_params.get("save_path")  #

    engine_cfg: Dict[str, Any] = {
        "device": device,                       #  wings_adapter nvidia/ascend
        "model_path": model_path,               #  --model-base
        "dit_weight": dit_weight,               #  --dit-weight
        "vae_path": vae_path,                   #  --vae-path
        "text_encoder_path": te_path,           #  --text-encoder-path
        "text_encoder_2_path": te2_path,        #  --text-encoder-2-path
    }
    if save_path:
        engine_cfg["save_path"] = save_path     #  wings_adapter  --save-path

    logger.info("[MMGM] Resolved HunyuanVideo engine_config: " +
                json.dumps({
                     k: v for k, v in engine_cfg.items()
                     if k not in ("dit_weight",)
                 }, indent=2))
    return engine_cfg


def _build_llm_engine_defaults(cmd_known_params: Dict[str, Any],
                                hardware_env: Dict[str, Any]) -> Dict[str, Any]:
    #  wings_adapter nvidia/ascend
    device = hardware_env.get("device")

    #  HYV_MODEL_BASE
    model_path = cmd_known_params.get("model_path")
    if not model_path:
        raise ValueError("[MMGM] model_path (or HYV_MODEL_PATH) is required for mmgm+wings.")
    engine_cfg: Dict[str, Any] = {
        "device": device,                       #  wings_adapter nvidia/ascend
        "model_path": model_path,
    }
    return engine_cfg


def load_and_merge_configs(
    hardware_env: Dict[str, Any],
    known_args: argparse.Namespace
) -> Dict[str, Any]:
    """配置加载与合并的主入口函数。

    将多层配置源从低优先级到高优先级合并：
        1. 硬件默认配置 (e.g., config/nvidia_default.json)
        2. 用户指定的配置文件 (--config-file)
        3. 用户 CLI 参数 (e.g., --model-path, --port)
        4. 引擎专属额外参数 (e.g., --tensor-parallel-size 2)

    处理流程:
        1. 提取 CLI 参数并检查 VRAM 需求
        2. 初始化 ModelIdentifier 对象获取模型元信息
        3. 自动选择/校验引擎，并处理 Ascend 专属逻辑
        4. 加载用户配置文件 (若指定)
        5. 根据引擎和模型类型选择配置构建路径：
           - mmgm + wings  → HunyuanVideo 稀疑路径，自动检测权重/VAE/编码器路径
           - llm + wings   → 通用 LLM 路径
           - 其他引擎     → 标准 _get_model_specific_config 查找链
        6. 合并所有配置层并返回最终参数字典

    Args:
        hardware_env: 硬件探测结果（device, count, details 等）
        known_args:   argparse.parse_known_args() 返回的已知参数

    Returns:
        Dict[str, Any]: 合并后的最终参数字典，包含 engine_config 子字典

    Raises:
        ValueError: 当 VRAM 不足、权重路径无效或引擎不兼容时抛出
    """
    logger.info("Starting config loading and merging...")
    # 1.
    # VRAM
    cmd_known_params = _process_cmd_args(known_args)
    if cmd_known_params.get("model_path"):
        if cmd_known_params.get("nnodes"):
            nodes_count = cmd_known_params.get("nnodes")
        else:
            nodes_count = 1
        _check_vram_requirements(cmd_known_params["model_path"], hardware_env, nodes_count)
    #
    model_info = ModelIdentifier(cmd_known_params.get("model_name"),
                                 cmd_known_params.get("model_path"),
                                 cmd_known_params.get("model_type"))



    # 2. ,mmgmd
    cmd_known_params = _auto_select_engine(hardware_env, cmd_known_params, model_info)

    # 3.
    config = known_args.config_file
    user_config = _load_user_config(config)

    # ===== MMGM/Wings  mmgm  engine_config JSON =====
    engine_is_wings_mmgm = (cmd_known_params.get("engine") == "wings" and
                            cmd_known_params.get("model_type") == "mmgm")

    engine_is_wings_llm = (cmd_known_params.get("engine") == "wings" and
                            cmd_known_params.get("model_type") == "llm")



    if user_config and get_config_force_env():
        engine_config = user_config
    else:
        if engine_is_wings_mmgm:
            # mmgm  JSON
            mmgm_defaults = _build_mmgm_engine_defaults(cmd_known_params, hardware_env)
            engine_config = _merge_configs(mmgm_defaults, user_config)
        elif engine_is_wings_llm:
            llm_defaults = _build_llm_engine_defaults(cmd_known_params, hardware_env)
            engine_config = _merge_configs(llm_defaults, user_config)
        else:
            engine_specific_defaults = _get_model_specific_config(hardware_env, cmd_known_params, model_info)
            engine_config = _merge_configs(engine_specific_defaults, user_config)

    # 4.
    final_engine_params = _merge_final_config(engine_config, cmd_known_params)


    logger.info(f"Config merging completed.")
    return final_engine_params
