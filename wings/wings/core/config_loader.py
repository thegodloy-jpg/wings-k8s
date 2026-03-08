# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

from typing import Dict, Any
import argparse
import json
import logging
import os
from pathlib import Path
import math

from wings.utils.device_utils import is_h20_gpu
from wings.utils.env_utils import get_master_ip, get_node_ips, get_lmcache_env, get_pd_role_env, \
    get_config_force_env, get_soft_fp8_env, get_vllm_distributed_port, get_sglang_distributed_port, get_router_env, \
    get_router_instance_group_name_env, get_router_instance_name_env, get_router_nats_path_env, \
    get_operator_acceleration_env, get_local_ip
from wings.utils.file_utils import check_torch_dtype, get_directory_size, check_permission_640, load_json_config
from wings.utils.model_utils import ModelIdentifier

logger = logging.getLogger(__name__)

# 定义默认配置文件的基础路径 (相对于项目根目录 wings/ )
DEFAULT_CONFIG_DIR = "wings/config"
DEFAULT_CONFIG_FILES = {
    "nvidia": "nvidia_default.json",
    "ascend": "ascend_default.json",
    "distributed": "distributed_config.json",
    "engine_parameter_mapping": "engine_parameter_mapping.json"
}


def _check_vram_requirements(weight_path: str, hardware_env: Dict[str, Any], nodes_count: int) -> None:
    """
    检查VRAM是否满足模型权重加载要求
    
    Args:
        weight_path: 模型权重路径
        hardware_env: 硬件环境信息
        
    Raises:
        ValueError: 当VRAM不足时抛出
    """
    if not os.path.exists(weight_path):
        logger.warning(f"Model weight path not found: {weight_path}")
        return
        
    weight_size_bytes = get_directory_size(weight_path)
    weight_size_gb = weight_size_bytes / (1024 ** 3)
    
    if not hardware_env.get("details"):
        logger.warning("Cannot get VRAM details, skipping VRAM check")
        return
        
    # 计算总可用VRAM（考虑分布式场景）
    free_vram_per_node = sum(d["free_memory"] for d in hardware_env["details"])
    total_free_vram = free_vram_per_node * nodes_count
    
    if total_free_vram < weight_size_gb:
        logger.warning(
            f"Insufficient VRAM: Required {weight_size_gb:.2f}GB, "
            f"but only {total_free_vram:.2f}GB available "
            f"({nodes_count} nodes × {free_vram_per_node:.2f}GB each)"
        )
    elif total_free_vram < weight_size_gb * 1.5:
        logger.warning(
            f"Performance warning: Total VRAM ({total_free_vram:.2f}GB) is less than 1.5x "
            f"model weight size ({weight_size_gb:.2f}GB) "
            f"({nodes_count} nodes × {free_vram_per_node:.2f}GB each)"
        )
    else:
        logger.info(
            f"VRAM check: Total VRAM ({total_free_vram:.2f}GB) is more than 1.5x "
            f"model weight size ({weight_size_gb:.2f}GB) "
            f"({nodes_count} nodes × {free_vram_per_node:.2f}GB each)"
        )


def _merge_cmd_params(hardware_env, engine_specific_defaults, cmd_known_params, model_info):
    """主函数：根据引擎类型分发给具体处理函数"""
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
    
    # 根据引擎类型路由到对应的处理函数
    engine = common_context["engine"]
    for key, value in engine_specific_defaults.items():
        if isinstance(value, dict):  # 处理嵌套字典
            engine_specific_defaults[key] = json.dumps(value)
    if engine in ("vllm", "vllm_ascend"):
        return _merge_vllm_params(engine_specific_defaults, common_context, engine_cmd_parameter, model_info)
    elif engine == "mindie":
        return _merge_mindie_params(engine_specific_defaults, common_context, engine_cmd_parameter)
    elif engine == "sglang":
        return _merge_sglang_params(engine_specific_defaults, common_context, engine_cmd_parameter)
    return engine_specific_defaults


def _merge_vllm_params(params, ctx, engine_cmd_parameter, model_info):
    """处理vllm/vllm_ascend引擎的参数合并"""
    # 参数映射配置路径
    engine_param_map_config_path = os.path.join(
        DEFAULT_CONFIG_DIR, 
        DEFAULT_CONFIG_FILES.get("engine_parameter_mapping")
    )
    
    # 执行各参数处理步骤
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
    "虚拟化环境下，需要计算cuda_graph_sizes，作为参数传给vllm 或vllm ascend"
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
    """设置通用参数映射"""
    if get_operator_acceleration_env() and ctx["device"] == "ascend":
        params['use_kunlun_atb'] = True
    else:
        return


def _set_soft_fp8(params, ctx, model_info):
    """FP8特性的特殊参数配置"""
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
            # 使用DP并行要禁用该功能
            params['enable_expert_parallel'] = False 
            params['tensor_parallel_size'] = 4
            params['use_kunlun_atb'] = False


def _set_common_params(params, engine_cmd_parameter, config_path):
    """设置通用参数映射"""
    vllm_param_map_config = load_json_config(config_path)['default_to_vllm_parameter_mapping']
    for key, value in vllm_param_map_config.items():
        if value and engine_cmd_parameter.get(key) is not None:
            params[value] = engine_cmd_parameter.get(key)


def _set_sequence_length(params, engine_cmd_parameter):
    """设置序列长度相关参数"""
    input_len = engine_cmd_parameter.get("input_length")
    output_len = engine_cmd_parameter.get("output_length")

    # 处理可能的 None 值
    input_len = input_len if input_len is not None else 0
    output_len = output_len if output_len is not None else 0

    max_model_len = input_len + output_len
    if max_model_len <= 0:
        return
    params['max_model_len'] = max_model_len


def _set_task(params, ctx):
    """设置序列长度相关参数"""
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
    """设置并行计算参数"""
    # 张量并行
    _adjust_tensor_parallelism(
        params, 
        ctx["device_count"], 
        'tensor_parallel_size', 
        ctx['distributed']
    )


def _get_pd_config(ctx, pd_role):
    """
    根据PD角色、上下文和参数生成最终的PD配置

    参数:
        pd_role: PD角色标识（如"P"表示Prefill，D表示Decode）
        ctx: 上下文信息，包含设备类型、设备数量等（如{'device': 'ascend', 'device_count': 2}）
        param: 额外参数

    返回:
        生成的PD配置字典
    """
    device = ctx.get('device', '')
    config = {}

    if device == "ascend":
        # Ascend设备的PD配置
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
        # 非Ascend设备的PD配置
        config = {
            "kv_connector": "NixlConnector",
            "kv_role": "kv_both"
        }
        logger.info(f"[PD Config] non-ascend device ({device}) detected, role={pd_role}")

    return config


def _set_kv_cache_config(params, ctx):
    """配置KV Cache多级存储策略"""
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
        return  # 无需配置
    
    params['kv_transfer_config'] = json.dumps(config)


def _set_router_config(params):
    """配置Wings Router相关参数"""
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
    """处理mindie引擎的参数合并"""
    # 设置通用参数
    engine_param_map_config_path = os.path.join(DEFAULT_CONFIG_DIR, 
                                            DEFAULT_CONFIG_FILES.get("engine_parameter_mapping"))
    mindie_param_map_config = load_json_config(engine_param_map_config_path)['default_to_mindie_parameter_mapping']
    for key, value in mindie_param_map_config.items():
        if not value or engine_cmd_parameter.get(key) is None:
            continue
        else:
            params[value] = engine_cmd_parameter.get(key)
            
    # 初始化新增参数
    is_mtp = False
    is_moe = False
    
    # 检查mtp.safetensors文件是否存在
    if engine_cmd_parameter["model_path"] and os.path.exists(engine_cmd_parameter["model_path"]):
        mtp_file = os.path.join(engine_cmd_parameter["model_path"], "mtp.safetensors")
        is_mtp = os.path.exists(mtp_file)
    
    # 检查是否是MOE模型
    moe_models = ["deepseek-r1-671b"]  # MOE模型列表
    if engine_cmd_parameter["model_name"].lower() in moe_models or params.get("enable_ep_moe"):
        is_moe = True
    
    # 设置通用参数
    params.update({
        'isMTP': is_mtp,
        'isMOE': is_moe
    })
    
    # 设置序列长度参数
    if engine_cmd_parameter["input_length"] and engine_cmd_parameter["output_length"]:
        params.update({
            'maxSeqLen': engine_cmd_parameter["input_length"] + engine_cmd_parameter["output_length"],
            'maxPrefillTokens': max(8192, engine_cmd_parameter["input_length"])
        })
    
    # 处理并行参数
    _adjust_tensor_parallelism(params, ctx["device_count"], 'worldSize')
    params['npuDeviceIds'] = [[i for i in range(ctx["device_count"])]]
    return params


def _merge_sglang_params(params, ctx, engine_cmd_parameter):
    """处理sglang引擎的参数合并"""
    # 设置通用参数
    engine_param_map_config_path = os.path.join(DEFAULT_CONFIG_DIR, 
                                            DEFAULT_CONFIG_FILES.get("engine_parameter_mapping"))
    sglang_param_map_config = load_json_config(engine_param_map_config_path)['default_to_sglang_parameter_mapping']
    for key, value in sglang_param_map_config.items():
        if not value or engine_cmd_parameter.get(key) is None:
            continue
        else:
            params[value] = engine_cmd_parameter.get(key)
            # sglang中，该参数的含义与vllm的正好相反
            if key == "enable_prefix_caching":
                params[value] = not engine_cmd_parameter.get(key)
                
    # 设置序列长度参数
    if engine_cmd_parameter["input_length"] and engine_cmd_parameter["output_length"]:
        params['context_length'] = engine_cmd_parameter["input_length"] + engine_cmd_parameter["output_length"]
    
    # 处理并行参数
    _adjust_tensor_parallelism(params, ctx["device_count"], 'tp_size', ctx['distributed'])

    # 解决sglang 4.10.0版本“--enable-ep-moe is deprecated”的问题
    if "enable_ep_moe" in params:
        params.pop("enable_ep_moe")
        params['ep_size'] = params['tp_size']
    return params


def _adjust_tensor_parallelism(params, device_count, tp_key, if_distributed=False):
    """通用TP参数调整逻辑"""
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
        # PD分离+分布式场景的特殊处理
        if get_pd_role_env():
            params[tp_key] = int(device_count)
        else:
            params[tp_key] = int(device_count) * n_nodes


def _merge_configs(*configs: Dict[str, Any]) -> Dict[str, Any]:
    """
    合并多个配置字典。后面的字典会覆盖前面字典中的同名键。
    执行深层合并（对于嵌套字典）。

    Args:
        *configs: 任意数量的配置字典。

    Returns:
        Dict[str, Any]: 合并后的字典。
    """
    merged = {}
    for config in configs:
        if not isinstance(config, dict):
            continue # 跳过非字典输入

        for key, value in config.items():
            if isinstance(value, dict) and key in merged and isinstance(merged[key], dict):
                # 如果值是字典且已存在同名键且也是字典，则递归合并
                merged[key] = _merge_configs(merged[key], value)
            else:
                # 否则直接覆盖或添加
                merged[key] = value
    return merged


def _load_default_config(hardware_env: Dict[str, Any]) -> Dict[str, Any]:
    """加载默认硬件环境配置"""
    device_key = 'device'
    device_type = hardware_env[device_key]
    default_config_path = os.path.join(DEFAULT_CONFIG_DIR,
                                     DEFAULT_CONFIG_FILES.get(device_type))
    logger.info(f"Determined default config file for hardware environment '{device_type}': {default_config_path}")
    return load_json_config(default_config_path)


def _load_user_config(config) -> Dict[str, Any]:
    """加载用户指定的配置文件或直接解析JSON字符串
    
    Args:
        known_args: 命令行参数对象，其中config_file可以是:
            - 文件路径
            - JSON格式的字符串
    """
    user_config = {}
    if not config:
        return user_config
        
    if config.strip().startswith('{') and config.strip().endswith('}'):
        # 尝试直接解析为JSON字符串
        try:
            user_config = json.loads(config)
            logger.info("Successfully parsed config from JSON string")
            return user_config
        except json.JSONDecodeError:
            logger.info("The config-file is not JSON string, will load it as a file")
    elif os.path.exists(config):
        # 作为文件路径处理
        logger.info(f"Loading user-specified config file: {config}")
        user_config = load_json_config(config)
    else:
        logger.warning(f"User-specified config not found or invalid: {config}")
    
    return user_config


def _process_cmd_args(known_args: argparse.Namespace) -> Dict[str, Any]:
    """处理命令行参数"""
    cmd_known_params = {k: v for k, v in vars(known_args).items() if v is not None and k not in ["config_file"]}
    return cmd_known_params


def _write_engine_second_line(path: str, engine: str) -> None:
    # 文件已存在的前提下，最简实现
    with open(path, "r+", encoding="utf-8") as f:
        lines = f.read().splitlines()          # 去掉各行末尾换行
        if len(lines) == 0:
            lines = ["", engine]               # 占位第1行 + 第2行写engine
        elif len(lines) == 1:
            lines.append(engine)               # 直接追加为第2行
        else:
            lines[1] = engine                  # 覆盖第2行
        f.seek(0)
        f.write("\n".join(lines) + "\n")       # 统一以换行结尾
        f.truncate()



def _auto_select_engine(hardware_env: Dict[str, Any],
                       cmd_known_params: Dict[str, Any],
                       model_info) -> Dict[str, Any]:
    """自动选择引擎并更新参数"""
    device_type = hardware_env['device']
    if hardware_env.get('details'):
        device_name = hardware_env.get('details')[0]['name']
    else:
        device_name = 'unknown'
    gpu_usage_mode = cmd_known_params.get("gpu_usage_mode", "full")    

    # 引擎选择逻辑，****
    if not cmd_known_params.get("engine"):
        engine = _select_engine_automatically(device_type, device_name, 
                                              gpu_usage_mode, model_info)
    else:
        engine = _validate_user_engine(cmd_known_params.get("engine"), 
                                       device_name, gpu_usage_mode, model_info)
    
    # 对于昇腾310，使用mindie部署时，进行前提条件检查
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

    # 更新model_type
    cmd_known_params["model_type"] = model_info.identify_model_type()
    
    # 设置最终引擎参数
    cmd_known_params["engine"] = engine
    _write_engine_second_line("/var/log/wings/wings.txt", engine)
    

    # 将 engine 设置为全局环境变量，供 gateway.py 使用
    os.environ['WINGS_ENGINE'] = engine
    logger.info(f"Set global environment variable WINGS_ENGINE={engine}")

    # 特殊场景处理
    if engine == "vllm":
        _handle_ascend_vllm(device_type, cmd_known_params)
    if cmd_known_params.get("distributed"):
        _handle_distributed(engine, cmd_known_params, model_info)


    # 更新device_count
    if cmd_known_params.get("gpu_usage_mode") == "full":
        device_count = hardware_env.get("count", 1)
    else:
        device_count = cmd_known_params.get("device_count", 1)
    # 校验device_count的值
    if device_count <= 0:
        raise ValueError(f"device_count must be an integer greater than 0. Current value: {device_count}")
    cmd_known_params['device_count'] = device_count

    return cmd_known_params


def _select_engine_automatically(device_type: str, 
                                 device_name: str, 
                                 gpu_usage_mode: str, 
                                 model_info) -> str:
    """自动选择引擎的逻辑"""
    if device_type == "nvidia":
        return _select_nvidia_engine(gpu_usage_mode, model_info)
    elif device_type == "ascend":
        return _select_ascend_engine(device_name, model_info)
    else:
        logger.info("No engine specified, automatically selected engine: vllm")
        return 'vllm'


def _select_nvidia_engine(gpu_usage_mode: str, model_info) -> str:
    """为NVIDIA设备选择引擎"""
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
    """为Ascend设备选择合适的推理引擎
    
    根据模型名称、设备类型和模型类型，自动选择最适合的Ascend推理引擎。
    选择优先级：
    1. Ascend310设备：强制使用mindie引擎（不支持vllm_ascend）
    2. embedding/rerank模型：使用vllm_ascend引擎
    3. 启用算子加速且为Qwen/QwQ模型：使用vllm_ascend引擎
    4. 启用KVCache卸载：使用vllm_ascend引擎
    5. 模型支持Mindie：使用mindie引擎
    6. 其他情况：使用vllm_ascend引擎
    
    Args:
        device_name: 设备名称（如Ascend310、Ascend910等）
        model_info: 模型信息
        
    Returns:
        str: 选定的引擎名称（'mindie'或'vllm_ascend'）
        
    Raises:
        ValueError: 当Ascend310设备尝试使用embedding或rerank模型时抛出异常
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
    验证用户指定的推理引擎是否有效，并根据特定条件自动切换到合适的引擎。
    
    参数:
        engine (str): 用户指定的推理引擎名称，支持 'mindie', 'vllm', 'vllm_ascend', 'sglang'
        device_name (str): 设备名称
        gpu_usage_mode (str): GPU使用模式
        model_info : 模型信息
        
    返回:
        str: 验证后或自动切换后的引擎名称
        
    异常:
        ValueError: 当指定的引擎不被支持时抛出异常
    """
    # 检查引擎是否在支持列表中
    if engine not in ['mindie', 'vllm', 'vllm_ascend', 'sglang', 'wings']:
        raise ValueError(f"The engine {engine} is not supported yet!Please change to 'mindie', 'vllm' or 'sglang'")

    vllm = 'vllm'
    model_type = model_info.identify_model_type()
    # 根据不同引擎进行特殊处理和自动切换逻辑
    if engine == 'mindie':
        # 如果设备是310系列，则保持使用mindie引擎
        if "310" in device_name:
            return 'mindie'
        # 如果启用了LMCache，则自动切换到vllm_ascend引擎
        elif get_lmcache_env():
            logger.warning("[KVCache Offload] KVCache Offload enabled, automatically switched to VLLM_Ascend engine")
            return "vllm_ascend"  
        # 对于embedding和rerank类型的模型，自动切换到vllm_ascend引擎
        elif model_type in ["embedding", "rerank"]:
            logger.warning(f"model type is {model_type}, automatically switched to VLLM_Ascend engine")
            return vllm
        elif get_router_env():
            logger.warning("Wings router enabled, automatically switched to VLLM engine")
            return vllm
        # 如果启用了算子加速且模型为Qwen或QwQ系列，则自动切换到vllm_ascend引擎
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
    """处理分布式VLLM的配置"""
    distributed_config_path = os.path.join(
        DEFAULT_CONFIG_DIR, 
        DEFAULT_CONFIG_FILES.get("distributed")
    )
    distributed_config = load_json_config(distributed_config_path)
    
    # 处理vllm和vllm_ascend引擎
    if engine in ['vllm', 'vllm_ascend']:
        _handle_vllm_distributed(distributed_config, cmd_params, model_info)
    
    # 处理sglang引擎
    elif engine == 'sglang':
        _handle_sglang_distributed(distributed_config, cmd_params)


def _handle_vllm_distributed(distributed_config: Dict[str, Any], cmd_params: Dict[str, Any], model_info):
    """处理vllm分布式配置"""
    vllm_distributed_port = get_vllm_distributed_port()
    pd_role = get_pd_role_env()
    model_architecture = model_info.model_architecture
    is_ascend_deepseek = (model_architecture == "DeepseekV3ForCausalLM" \
                          and cmd_params.get("engine") == 'vllm_ascend')
    
    if pd_role in ['P', 'D'] or is_ascend_deepseek:
        # 处理P和D角色
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
        # 处理其他角色
        if not vllm_distributed_port:
            vllm_distributed_port = distributed_config['vllm_distributed']['ray_head_port']
        
        cmd_params.update({
            'distributed_executor_backend': 'ray',
            'ray_head_ip': get_master_ip(),
            'ray_head_port': vllm_distributed_port
        })


def _handle_sglang_distributed(distributed_config: Dict[str, Any], cmd_params: Dict[str, Any]):
    """处理sglang分布式配置"""
    dist_port = get_sglang_distributed_port()
    if not dist_port:
        dist_port = distributed_config['sglang_distributed']['dist_port']
    
    cmd_params.update({
        'dist_port': dist_port
    })


def _handle_ascend_vllm(device_type: str, cmd_params: Dict[str, Any]):
    """处理Ascend设备上的VLLM特殊重命名"""
    if device_type == "ascend" and cmd_params.get("engine") == "vllm":
        cmd_params["engine"] = "vllm_ascend"


def _get_model_specific_config(hardware_env: Dict[str, Any],
                             cmd_known_params: Dict[str, Any],
                             model_info) -> Dict[str, Any]:
    """获取模型特定配置"""
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
    models_dict = default_config[config_model_key][model_type]

    if model_architecture in models_dict:
        model_architecture_dict = models_dict[model_architecture]

        # 特殊处理DeepSeek满血版系列模型
        is_deepseek_sglang_nvidia = (
            model_architecture == "DeepseekV3ForCausalLM" 
            and hardware_env[device_key] == "nvidia" 
            and engine == sglang_name 
            and not cmd_known_params.get("distributed")
        )

        # 对虚拟化环境检测不到details的情况，进行特殊处理
        if hardware_env.get("details"):
            main_gpu = hardware_env.get("details")[0]
            h20_model = is_h20_gpu(main_gpu.get("total_memory", 0), 10.0)
        else:
            main_gpu = ""
            h20_model = False
            logger.warning("No hardware details detected, setting main_gpu to empty string and h20_model to False")

        for model, config in model_architecture_dict.items():
            if model_name_lower != model.lower():
                continue
                
            if is_deepseek_sglang_nvidia and h20_model in ["H20-96G", "H20-141G"]:
                engine_specific_defaults = config[engine_key][h20_model]
                logger.info(f"Using dedicated config for model '{model_name}' on {h20_model}")
            elif not is_deepseek_sglang_nvidia:
                engine_specific_defaults = config[engine_key]
                logger.info(f"The default deploy configuration "
                            f"of the model architecture {model_architecture} will be used.")
            break
        if not engine_specific_defaults:
            logger.info(f"The default deploy configuration of the "
                        f"model architecture {model_architecture} will be used.")
            engine_specific_defaults = model_architecture_dict[default_key][engine_key]
    else:
        engine_specific_defaults = models_dict[default_key][engine_key]
        logger.info(f"The default deploy configuration of the model type {model_type} will be used.")        

    engine_specific_defaults = _merge_cmd_params(hardware_env, engine_specific_defaults, cmd_known_params, model_info)
    return engine_specific_defaults


def _merge_final_config(engine_config: Dict[str, Any],
                       cmd_known_params: Dict[str, Any]) -> Dict[str, Any]:
    """合并最终配置"""
    cmd_known_params['engine_config'] = engine_config
    
    return cmd_known_params

# ============================== 以下为 MMGM/Wings 新增的辅助函数 ==============================


def _autodiscover_hunyuan_paths(model_path_root: str) -> Dict[str, str]:
    """
    在给定的模型根目录下自动探测混元（HunyuanVideo）关键路径：
    - dit_weight:  优先 <var>/transformers/mp_rank_00_model_states.pt；
                    否则全局搜 mp_rank_00_model_states.pt 或 pytorch_model_*.pt
    - vae_path:    优先 <var>/vae；否则全局搜名为 'vae' 的目录
    - text_encoder_path:      默认 <root>/text_encoder；找不到再全局搜第一个 'text_encoder*' 目录
    - text_encoder_2_path:    默认 <root>/clip-vit-large-patch14；否则找 'text_encoder_2' 或包含 'clip-vit-large' 的目录
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
    """查找变体目录"""
    if (base_path / "hunyuan-video-t2v-720p").is_dir():
        return base_path / "hunyuan-video-t2v-720p"
    elif (base_path / "hunyuan-video-t2v-540p").is_dir():
        return base_path / "hunyuan-video-t2v-540p"
    return None


def _find_dit_weight(base_path, variant_dir):
    """查找DIT权重文件"""
    from pathlib import Path as _P
    
    # 优先在变体目录中查找
    if variant_dir and (variant_dir / "transformers" / "mp_rank_00_model_states.pt").is_file():
        return str((variant_dir / "transformers" / "mp_rank_00_model_states.pt").resolve())
    
    # 全局搜索
    for root, _, files in os.walk(str(base_path)):
        for fn in files:
            if fn == "mp_rank_00_model_states.pt" or fn.startswith("pytorch_model_"):
                return str((_P(root) / fn).resolve())
    return ""


def _find_vae_path(base_path, variant_dir):
    """查找VAE目录"""
    from pathlib import Path as _P
    
    # 优先在变体目录中查找
    if variant_dir and (variant_dir / "vae").is_dir():
        return str((variant_dir / "vae").resolve())
    
    # 全局搜索
    for root, dirs, _ in os.walk(str(base_path)):
        if "vae" in dirs:
            return str((_P(root) / "vae").resolve())
    return ""


def _find_text_encoder_path(base_path):
    """查找文本编码器1目录"""
    from pathlib import Path as _P
    
    # 默认位置
    if (base_path / "text_encoder").is_dir():
        return str((base_path / "text_encoder").resolve())
    
    # 全局搜索
    for root, dirs, _ in os.walk(str(base_path)):
        for d in dirs:
            if d.startswith("text_encoder"):
                return str((_P(root) / d).resolve())
    return ""


def _find_text_encoder_2_path(base_path):
    """查找文本编码器2/CLIP-L目录"""
    from pathlib import Path as _P
    
    # 默认位置
    if (base_path / "clip-vit-large-patch14").is_dir():
        return str((base_path / "clip-vit-large-patch14").resolve())
    
    # 次选位置
    if (base_path / "text_encoder_2").is_dir():
        return str((base_path / "text_encoder_2").resolve())
    
    # 全局搜索
    for root, dirs, _ in os.walk(str(base_path)):
        for d in dirs:
            if d == "text_encoder_2" or "clip-vit-large" in d:
                return str((_P(root) / d).resolve())
    return ""


def _build_mmgm_engine_defaults(cmd_known_params: Dict[str, Any],
                                hardware_env: Dict[str, Any]) -> Dict[str, Any]:
    """
    为 mmgm + wings 构建最小可用的 engine_config：
    - 统一注入 device（由硬件环境决定，不在服务端重复检测）
    - 将顶层 --save-path 透传（存在则写入 engine_config.save_path；不存在由服务端落到默认 outputs/）
    - 自动探测/或从 HYV_* 环境变量覆盖 HunyuanVideo 的关键路径
    - 默认开启 flow_reverse（与已有启动脚本保持一致）
    """
    # 设备（与 wings_adapter 的脚本选择一致：nvidia/ascend）
    device = hardware_env.get("device")

    # 基础模型根（允许 HYV_MODEL_BASE 覆盖）
    model_path = cmd_known_params.get("model_path")
    if not model_path:
        raise ValueError("[MMGM] model_path (or HYV_MODEL_PATH) is required for mmgm+wings.")

    # 自动探测
    discovered = _autodiscover_hunyuan_paths(model_path)

    # 允许通过环境变量覆盖（与 shell 逻辑对齐）
    dit_weight = discovered["dit_weight"]
    vae_path = discovered["vae_path"]
    te_path = discovered["text_encoder_path"]
    te2_path = discovered["text_encoder_2_path"]

    # 基础存在性校验
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

    # 顶层 --save-path（注意：顶层参数名为 save_path）
    save_path = cmd_known_params.get("save_path")  # 可为空

    engine_cfg: Dict[str, Any] = {
        "device": device,                       # 供 wings_adapter 选择环境脚本（nvidia/ascend）
        "model_path": model_path,               # 服务端消费 --model-base
        "dit_weight": dit_weight,               # 服务端消费 --dit-weight
        "vae_path": vae_path,                   # 服务端消费 --vae-path
        "text_encoder_path": te_path,           # 服务端消费 --text-encoder-path
        "text_encoder_2_path": te2_path,        # 服务端消费 --text-encoder-2-path
    }
    if save_path:
        engine_cfg["save_path"] = save_path     # 由 wings_adapter 映射为 --save-path

    logger.info("[MMGM] Resolved HunyuanVideo engine_config: " +
                json.dumps({
                     k: v for k, v in engine_cfg.items()
                     if k not in ("dit_weight",)
                 }, indent=2))
    return engine_cfg


def _build_llm_engine_defaults(cmd_known_params: Dict[str, Any],
                                hardware_env: Dict[str, Any]) -> Dict[str, Any]:
    # 设备（与 wings_adapter 的脚本选择一致：nvidia/ascend）
    device = hardware_env.get("device")

    # 基础模型根（允许 HYV_MODEL_BASE 覆盖）
    model_path = cmd_known_params.get("model_path")
    if not model_path:
        raise ValueError("[MMGM] model_path (or HYV_MODEL_PATH) is required for mmgm+wings.")
    engine_cfg: Dict[str, Any] = {
        "device": device,                       # 供 wings_adapter 选择环境脚本（nvidia/ascend）
        "model_path": model_path,               
    }
    return engine_cfg


def load_and_merge_configs(
    hardware_env: Dict[str, Any],
    known_args: argparse.Namespace
) -> Dict[str, Any]:
    """
    加载并合并配置的主函数。

    合并优先级 (由低到高):
    1. 默认硬件环境配置文件 (e.g., config/nvidia_default.json)
    2. 用户通过 --config-file 指定的配置文件
    3. 命令行已知参数 (e.g., --model-path, --port)
    4. 命令行未知参数 (解析为字典, e.g., --tensor-parallel-size 2)

    Args:
        hardware_env (str): 检测到的硬件环境 ('nvidia', 'ascend', 'cpu')。
        known_args (argparse.Namespace): `argparse.parse_known_args()` 返回的已知参数。
        unknown_engine_args (List[str]): `argparse.parse_known_args()` 返回的未知参数列表。

    Returns:
        Dict[str, Any]: 最终合并后的参数字典。
    """
    logger.info("Starting config loading and merging...")
    # 1. 前置处理
    # 检查VRAM要求
    cmd_known_params = _process_cmd_args(known_args)
    if cmd_known_params.get("model_path"):
        if cmd_known_params.get("nodes"):
            nodes_count = len(cmd_known_params.get("nodes").split(','))
        else:
            nodes_count = 1
        _check_vram_requirements(cmd_known_params["model_path"], hardware_env, nodes_count)
    # 获取模型信息
    model_info = ModelIdentifier(cmd_known_params.get("model_name"),
                                 cmd_known_params.get("model_path"),
                                 cmd_known_params.get("model_type"))

    
  
    # 2. 自动选择引擎,mmgmd的也融入这里面
    cmd_known_params = _auto_select_engine(hardware_env, cmd_known_params, model_info)

    # 3. 加载默认配置和用户配置
    config = known_args.config_file
    user_config = _load_user_config(config)

    # ===== MMGM/Wings 新增：构建 mmgm 的 engine_config（跳过默认 JSON 模板）=====
    engine_is_wings_mmgm = (cmd_known_params.get("engine") == "wings" and
                            cmd_known_params.get("model_type") == "mmgm")

    engine_is_wings_llm = (cmd_known_params.get("engine") == "wings" and
                            cmd_known_params.get("model_type") == "llm")

    

    if user_config and get_config_force_env():
        engine_config = user_config
    else:
        if engine_is_wings_mmgm:
            # mmgm 不依赖通用 JSON 模板，从模型路径自动探测，合成最小可用配置
            mmgm_defaults = _build_mmgm_engine_defaults(cmd_known_params, hardware_env)
            engine_config = _merge_configs(mmgm_defaults, user_config)
        elif engine_is_wings_llm:
            llm_defaults = _build_llm_engine_defaults(cmd_known_params, hardware_env)
            engine_config = _merge_configs(llm_defaults, user_config)
        else:
            engine_specific_defaults = _get_model_specific_config(hardware_env, cmd_known_params, model_info)
            engine_config = _merge_configs(engine_specific_defaults, user_config)
    
    # 4. 合并最终配置
    final_engine_params = _merge_final_config(engine_config, cmd_known_params)


    logger.info(f"Config merging completed.")
    return final_engine_params