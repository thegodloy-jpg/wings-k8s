# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: core/engine_manager.py
# Purpose: Resolves engine adapter module and builds startup command strings.
# Status: Compatibility wrapper in launcher mode.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Adapter interaction is command-build only.
# - Do not introduce direct subprocess startup in this module.
# -----------------------------------------------------------------------------
# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
推理引擎命令管理模块

负责根据最终参数动态加载引擎适配器并生成启动命令。
"""

import importlib
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

# 定义引擎适配器模块的基础路径
ENGINE_ADAPTER_PACKAGE = "app.engines"


def start_engine_service(params: Dict[str, Any]):
    """
    兼容旧接口名: 生成引擎启动命令，不直接启动引擎。

    Args:
        params (Dict[str, Any]): 包含所有合并后参数的字典，
                                 必须包含 'engine' 键来指定引擎类型。

    Returns:
        str: 适配器构建出的启动命令。

    Raises:
        ValueError: 如果 'engine' 参数缺失或无效。
        ImportError: 如果找不到对应的引擎适配器模块。
        AttributeError: 如果适配器模块没有实现命令构建函数。
        Exception: 如果命令构建过程中发生错误。
    """
    engine_name = params.get("engine")
    if engine_name == 'vllm_ascend':
        engine_name = 'vllm'
    if not engine_name:
        raise ValueError("Missing 'engine' key in params dict, cannot determine which engine to start.")

    logger.info(f"Attempting to build command for engine: {engine_name}")

    # 构造适配器模块的完整路径，例如: app.engines.vllm_adapter
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

    build_function_name = "build_start_command"
    if not hasattr(adapter_module, build_function_name):
        logger.error(f"Expected command builder '{build_function_name}' \
                     not found in engine adapter module '{adapter_module_name}'.")
        raise AttributeError(f"Adapter '{adapter_module_name}' is missing function '{build_function_name}'.")

    build_function = getattr(adapter_module, build_function_name)

    try:
        command = build_function(params)
        logger.info(f"Engine '{engine_name}' command built successfully.")
        return command
    except Exception:
        logger.error(f"Failed to build command for engine '{engine_name}' via adapter.", exc_info=True)
        raise
