# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: core/config_loader.py
# Purpose: Loads and merges configuration layers (defaults, mappings, CLI/env overrides).
# Status: Active control-plane helper.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Preserve parameter merge precedence semantics.
# - Keep mapping behavior compatible with wings config conventions.
# -----------------------------------------------------------------------------
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

#  ( wings/ )
def _resolve_default_config_dir() -> str:
    """Resolve default config directory with robust fallback order."""
    env_dir = os.getenv("WINGS_CONFIG_DIR", "").strip()
    if env_dir:
        return env_dir
    bundled_dir = Path(__file__).resolve().parents[1] / "config"
    if bundled_dir.exists():
        return str(bundled_dir)
    return "wings/config"


DEFAULT_CONFIG_DIR = _resolve_default_config_dir()
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
    """Load mapping dict safely and return empty dict on missing/invalid content."""
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
    """
     H20
    H20-96G / H20-141G
    """
    hint = os.getenv("WINGS_H20_MODEL", "").strip()
    if not hint:
        return ""
    if hint in ("H20-96G", "H20-141G"):
        return hint
    logger.warning("Invalid WINGS_H20_MODEL=%s, expected H20-96G or H20-141G", hint)
    return ""


def _check_vram_requirements(weight_path: str, hardware_env: Dict[str, Any], nodes_count: int) -> None:
    """
    VRAM

    Args:
        weight_path:
        hardware_env:

    Raises:
        ValueError: VRAM
    """
    if not os.path.exists(weight_path):
        logger.warning(f"Model weight path not found: {weight_path}")
        return

    weight_size_bytes = get_directory_size(weight_path)
    weight_size_gb = weight_size_bytes / (1024 ** 3)

    if not hardware_env.get("details"):
        logger.warning("Cannot get VRAM details, skipping VRAM check")
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
    """(no description)"""
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

    #
    engine = common_context["engine"]
    for key, value in engine_specific_defaults.items():
        if isinstance(value, dict):  #
            engine_specific_defaults[key] = json.dumps(value)
    if engine in ("vllm", "vllm_ascend"):
        return _merge_vllm_params(engine_specific_defaults, common_context, engine_cmd_parameter, model_info)
    elif engine == "mindie":
        return _merge_mindie_params(engine_specific_defaults, common_context, engine_cmd_parameter)
    elif engine == "sglang":
        return _merge_sglang_params(engine_specific_defaults, common_context, engine_cmd_parameter)
    return engine_specific_defaults


def _merge_vllm_params(params, ctx, engine_cmd_parameter, model_info):
    """vllm/vllm_ascend"""
    #
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
    "cuda_graph_sizesvllm vllm ascend"
    if ctx["gpu_usage_mode"] != "full" and ctx["model_type"] == "llm":
        if ctx["device_details"] and ctx["device_details"][0]:
            total_memory = ctx["device_details"][0]["total_memory"]
        else:
            total_memory = 12
            logger.warning("Can't get device details, will set total_memroy to 12G")
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
    """(no description)"""
    if get_operator_acceleration_env() and ctx["device"] == "ascend":
        params['use_kunlun_atb'] = True
    else:
        return


def _set_soft_fp8(params, ctx, model_info):
    """FP8"""
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
                    "enabled": True},
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
    """(no description)"""
    vllm_param_map_config = _load_mapping(config_path, 'default_to_vllm_parameter_mapping')
    for key, value in vllm_param_map_config.items():
        if value and engine_cmd_parameter.get(key) is not None:
            params[value] = engine_cmd_parameter.get(key)


def _set_sequence_length(params, engine_cmd_parameter):
    """(no description)"""
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
    """(no description)"""
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
    """(no description)"""
    #
    _adjust_tensor_parallelism(
        params,
        ctx["device_count"],
        'tensor_parallel_size',
        ctx['distributed']
    )


def _get_pd_config(ctx, pd_role):
    """
    PDPD

    :
        pd_role: PD"P"PrefillDDecode
        ctx: {'device': 'ascend', 'device_count': 2}
        param:

    :
        PD
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
            "kv_port": "20001",
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
    """KV Cache"""
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
    """Wings Router"""
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
    """mindie"""
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

    #
    _adjust_tensor_parallelism(params, ctx["device_count"], 'worldSize')
    params['npuDeviceIds'] = [[i for i in range(ctx["device_count"])]]
    return params


def _merge_sglang_params(params, ctx, engine_cmd_parameter):
    """sglang"""
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
    """TP"""
    default_tp = params.get(tp_key)
    if default_tp:
        return
    if not if_distributed:
        if default_tp is not None and default_tp != device_count:
            logging.warning(
                f"Detected {device_count} devices in current environment, "
                f"while default recommended TP is {default_tp}, "
                f"will use device count as final TP value"
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
    """



    Args:
        *configs:

    Returns:
        Dict[str, Any]:
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
    """(no description)"""
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
    return load_json_config(default_config_path)


def _load_engine_fallback_defaults(engine: str) -> Dict[str, Any]:
    """
     fallback sglang_default.json / mindie_default.json

     vllm_default.json / model_deploy_config
     vllm/vllm_ascend
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
    """JSON

    Args:
        known_args: config_file:
            -
            - JSON
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
    """(no description)"""
    cmd_known_params = {k: v for k, v in vars(known_args).items() if v is not None and k not in ["config_file"]}
    return cmd_known_params


def _write_engine_second_line(path: str, engine: str) -> None:
    """
     2

    /

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
    """(no description)"""
    device_type = hardware_env['device']
    if hardware_env.get('details'):
        device_name = hardware_env.get('details')[0]['name']
    else:
        device_name = 'unknown'
    gpu_usage_mode = cmd_known_params.get("gpu_usage_mode", "full")

    # ****
    if not cmd_known_params.get("engine"):
        engine = _select_engine_automatically(device_type, device_name,
                                              gpu_usage_mode, model_info)
    else:
        engine = _validate_user_engine(cmd_known_params.get("engine"),
                                       device_name, gpu_usage_mode, model_info)

    # 310mindie
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

    # model_type
    cmd_known_params["model_type"] = model_info.identify_model_type()

    #
    cmd_known_params["engine"] = engine
    _write_engine_second_line("/var/log/wings/wings.txt", engine)


    #  engine  gateway.py
    os.environ['WINGS_ENGINE'] = engine
    logger.info(f"Set global environment variable WINGS_ENGINE={engine}")

    #
    if engine == "vllm":
        _handle_ascend_vllm(device_type, cmd_known_params)
    #  backend-dist-nv


    # device_count
    if cmd_known_params.get("gpu_usage_mode") == "full":
        device_count = hardware_env.get("count", 1)
    else:
        device_count = cmd_known_params.get("device_count", 1)
    # device_count
    if device_count <= 0:
        raise ValueError(f"device_count must be an integer greater than 0. Current value: {device_count}")
    cmd_known_params['device_count'] = device_count

    return cmd_known_params


def _select_engine_automatically(device_type: str,
                                 device_name: str,
                                 gpu_usage_mode: str,
                                 model_info) -> str:
    """(no description)"""
    if device_type == "nvidia":
        return _select_nvidia_engine(gpu_usage_mode, model_info)
    elif device_type == "ascend":
        return _select_ascend_engine(device_name, model_info)
    else:
        logger.info("No engine specified, automatically selected engine: vllm")
        return 'vllm'


def _select_nvidia_engine(gpu_usage_mode: str, model_info) -> str:
    """NVIDIA"""
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
    """Ascend

    Ascend

    1. Ascend310mindievllm_ascend
    2. embedding/rerankvllm_ascend
    3. Qwen/QwQvllm_ascend
    4. KVCachevllm_ascend
    5. Mindiemindie
    6. vllm_ascend

    Args:
        device_name: Ascend310Ascend910
        model_info:

    Returns:
        str: 'mindie''vllm_ascend'

    Raises:
        ValueError: Ascend310embeddingrerank
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
    """


    :
        engine (str):  'mindie', 'vllm', 'vllm_ascend', 'sglang'
        device_name (str):
        gpu_usage_mode (str): GPU
        model_info :

    :
        str:

    :
        ValueError:
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
            return vllm
        elif get_router_env():
            logger.warning("Wings router enabled, automatically switched to VLLM engine")
            return vllm
        # QwenQwQvllm_ascend
        elif get_operator_acceleration_env():
            logger.warning(f"operator_acceleration is enabled, \
                            automatically switched to VLLM_Ascend engine")
            return vllm
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


def _handle_distributed(engine: str, cmd_params: Dict[str, Any], model_info):
    """VLLM"""
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


def _handle_vllm_distributed(distributed_config: Dict[str, Any], cmd_params: Dict[str, Any], model_info):
    """vllm"""
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
    """sglang"""
    dist_port = get_sglang_distributed_port()
    if not dist_port:
        dist_port = distributed_config['sglang_distributed']['dist_port']

    cmd_params.update({
        'dist_port': dist_port
    })


def _handle_ascend_vllm(device_type: str, cmd_params: Dict[str, Any]):
    """AscendVLLM"""
    if device_type == "ascend" and cmd_params.get("engine") == "vllm":
        cmd_params["engine"] = "vllm_ascend"


def _get_model_specific_config(hardware_env: Dict[str, Any],
                             cmd_known_params: Dict[str, Any],
                             model_info) -> Dict[str, Any]:
    """(no description)"""
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
    """(no description)"""
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
    """(no description)"""
    if (base_path / "hunyuan-video-t2v-720p").is_dir():
        return base_path / "hunyuan-video-t2v-720p"
    elif (base_path / "hunyuan-video-t2v-540p").is_dir():
        return base_path / "hunyuan-video-t2v-540p"
    return None


def _find_dit_weight(base_path, variant_dir):
    """DIT"""
    from pathlib import Path as _P

    #
    if variant_dir and (variant_dir / "transformers" / "mp_rank_00_model_states.pt").is_file():
        return str((variant_dir / "transformers" / "mp_rank_00_model_states.pt").resolve())

    #
    for root, _, files in os.walk(str(base_path)):
        for fn in files:
            if fn == "mp_rank_00_model_states.pt" or fn.startswith("pytorch_model_"):
                return str((_P(root) / fn).resolve())
    return ""


def _find_vae_path(base_path, variant_dir):
    """VAE"""
    from pathlib import Path as _P

    #
    if variant_dir and (variant_dir / "vae").is_dir():
        return str((variant_dir / "vae").resolve())

    #
    for root, dirs, _ in os.walk(str(base_path)):
        if "vae" in dirs:
            return str((_P(root) / "vae").resolve())
    return ""


def _find_text_encoder_path(base_path):
    """1"""
    from pathlib import Path as _P

    #
    if (base_path / "text_encoder").is_dir():
        return str((base_path / "text_encoder").resolve())

    #
    for root, dirs, _ in os.walk(str(base_path)):
        for d in dirs:
            if d.startswith("text_encoder"):
                return str((_P(root) / d).resolve())
    return ""


def _find_text_encoder_2_path(base_path):
    """2/CLIP-L"""
    from pathlib import Path as _P

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
                return str((_P(root) / d).resolve())
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
    """


     ():
    1.  (e.g., config/nvidia_default.json)
    2.  --config-file
    3.  (e.g., --model-path, --port)
    4.  (, e.g., --tensor-parallel-size 2)

    Args:
        hardware_env (str):  ('nvidia', 'ascend', 'cpu')
        known_args (argparse.Namespace): `argparse.parse_known_args()`
        unknown_engine_args (List[str]): `argparse.parse_known_args()`

    Returns:
        Dict[str, Any]:
    """
    logger.info("Starting config loading and merging...")
    # 1.
    # VRAM
    cmd_known_params = _process_cmd_args(known_args)
    if cmd_known_params.get("model_path"):
        if cmd_known_params.get("nodes"):
            nodes_count = len(cmd_known_params.get("nodes").split(','))
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
