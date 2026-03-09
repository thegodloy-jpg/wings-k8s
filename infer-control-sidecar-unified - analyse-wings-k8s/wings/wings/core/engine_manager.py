# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
推理引擎管理和启动模块

负责根据最终确定的参数，动态加载并调用相应推理引擎的适配器来启动服务。
"""

import importlib
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

# 定义引擎适配器模块的基础路径
ENGINE_ADAPTER_PACKAGE = "wings.engines"


def start_engine_service(params: Dict[str, Any]):
    """
    根据参数启动指定的推理引擎服务。

    Args:
        params (Dict[str, Any]): 包含所有合并后参数的字典，
                                  必须包含 'engine' 键来指定引擎类型。

    Raises:
        ValueError: 如果 'engine' 参数缺失或无效。
        ImportError: 如果找不到对应的引擎适配器模块。
        AttributeError: 如果适配器模块没有实现预期的启动函数。
        Exception: 如果引擎启动过程中发生其他错误。
    """
    engine_name = params.get("engine")
    if engine_name == 'vllm_ascend':
        engine_name = 'vllm'
    if not engine_name:
        raise ValueError("Missing 'engine' key in params dict, cannot determine which engine to start.")

    logger.info(f"Attempting to start engine: {engine_name}")

    # 构造适配器模块的完整路径，例如: wings.engines.vllm_adapter
    adapter_module_name = f"{ENGINE_ADAPTER_PACKAGE}.{engine_name}_adapter"

    try:
        # 动态导入适配器模块
        adapter_module = importlib.import_module(adapter_module_name)
        logger.info(f"Successfully imported adapter module: {adapter_module_name}")

    except ImportError as e:
        logger.error(f"Failed to import adapter module '{adapter_module_name}' \
                     for engine '{engine_name}'. Please ensure the engine is \
                     supported and adapter file exists.", exc_info=True)
        raise ImportError(f"Adapter for engine '{engine_name}' not found. \
                          Did you forget to create {adapter_module_name}.py \
                          or is the engine name incorrect?") from e
    except Exception as e:
        logger.error(f"Unknown error importing adapter module '{adapter_module_name}'.", exc_info=True)
        raise # 重新抛出原始异常

    # 假设每个适配器模块都有一个名为 'start_engine' 的函数
    start_function_name = "start_engine"
    if not hasattr(adapter_module, start_function_name):
        logger.error(f"Expected startup function '{start_function_name}' \
                     not found in engine adapter module '{adapter_module_name}'.")
        raise AttributeError(f"Adapter '{adapter_module_name}' is missing function '{start_function_name}'.")

    start_function = getattr(adapter_module, start_function_name)

    try:
        # 将整个参数字典传递给适配器的启动函数
        if start_function(params):
            logger.info(f"Engine '{engine_name}' service startup call completed.")
            
    except Exception as e:
        logger.error(f"Failed to start engine '{engine_name}' service via adapter.", exc_info=True)
        # 可以选择在这里处理特定异常或直接向上抛出
        raise # 重新抛出，让上层 (wings.py) 处理
    
    return True