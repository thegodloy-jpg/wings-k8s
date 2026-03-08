# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: core/engine_manager.py
# Purpose: Resolves engine adapter module and builds startup script body.
# Status: Active dispatcher in launcher mode.
# Responsibilities:
#   - Dynamically import the correct engine adapter by engine name.
#   - Prefer build_start_script; fall back to build_start_command + exec wrap.
#   - vllm_ascend is an alias for vllm_adapter (same command structure).
# Sidecar Contracts:
#   - Returns a script body (str) for writing to start_command.sh.
#   - Never introduces direct subprocess startup in this module.
# -----------------------------------------------------------------------------
# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
推理引擎命令管理模块

根据最终参数动态加载引擎适配器并生成 start_command.sh 脚本体。

支持引擎：
  vllm         → engines/vllm_adapter.py
  vllm_ascend  → engines/vllm_adapter.py（同 vllm，env 差异由 params["engine"] 驱动）
  sglang       → engines/sglang_adapter.py
  mindie       → engines/mindie_adapter.py
"""

import importlib
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

# 定义引擎适配器模块的基础路径
ENGINE_ADAPTER_PACKAGE = "app.engines"

# 引擎名称到适配器文件名的映射
# vllm_ascend 复用 vllm_adapter，由 params["engine"] 保留原始值供 env 构建函数判断
ENGINE_ADAPTER_ALIASES: Dict[str, str] = {
    "vllm_ascend": "vllm",
}


def start_engine_service(params: Dict[str, Any]) -> str:
    """
    根据 params["engine"] 动态加载适配器，返回 start_command.sh 的脚本体（不含 shebang）。

    执行顺序：
      1. 将 engine 名称规范化（小写，处理别名）。
      2. 动态导入 app.engines.<engine>_adapter 模块。
      3. 优先调用 build_start_script(params) 获取脚本体。
         若适配器仅实现 build_start_command，则用 exec 前缀包装。

    Args:
        params: 包含 engine、engine_config 等合并后参数的字典。

    Returns:
        str: bash 脚本体（不含 shebang，可直接追加到 #!/usr/bin/env bash 后）。

    Raises:
        ValueError:     如果 params 中缺少 engine 键。
        ImportError:    如果找不到对应的适配器模块。
        AttributeError: 如果适配器既无 build_start_script 也无 build_start_command。
    """
    engine_name = params.get("engine")
    if not engine_name:
        raise ValueError("Missing 'engine' key in params dict.")

    # 规范化：小写 + 别名解析（vllm_ascend → vllm）
    adapter_key = ENGINE_ADAPTER_ALIASES.get(engine_name, engine_name)

    logger.info("Loading adapter for engine: %s (adapter: %s)", engine_name, adapter_key)

    adapter_module_name = f"{ENGINE_ADAPTER_PACKAGE}.{adapter_key}_adapter"
    try:
        adapter_module = importlib.import_module(adapter_module_name)
    except ImportError as e:
        logger.error(
            "Failed to import adapter '%s' for engine '%s'.",
            adapter_module_name, engine_name, exc_info=True
        )
        raise ImportError(
            f"Adapter for engine '{engine_name}' not found: {adapter_module_name}.py"
        ) from e

    # 优先使用 build_start_script（返回完整脚本体）
    if hasattr(adapter_module, "build_start_script"):
        logger.info("Using build_start_script from %s", adapter_module_name)
        return adapter_module.build_start_script(params)

    # 回退：使用 build_start_command 并用 exec 包装
    if hasattr(adapter_module, "build_start_command"):
        logger.info(
            "build_start_script not found; falling back to build_start_command from %s",
            adapter_module_name,
        )
        cmd = adapter_module.build_start_command(params)
        return f"exec {cmd}\n"

    raise AttributeError(
        f"Adapter '{adapter_module_name}' implements neither "
        f"build_start_script nor build_start_command."
    )

