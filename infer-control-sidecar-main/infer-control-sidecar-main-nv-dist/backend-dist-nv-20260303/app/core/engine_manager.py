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


 start_command.sh


  vllm          engines/vllm_adapter.py
  vllm_ascend   engines/vllm_adapter.py vllmenv  params["engine"]
  sglang        engines/sglang_adapter.py
  mindie        engines/mindie_adapter.py
"""

import importlib
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

#
ENGINE_ADAPTER_PACKAGE = "app.engines"

#
# vllm_ascend  vllm_adapter params["engine"]  env
ENGINE_ADAPTER_ALIASES: Dict[str, str] = {
    "vllm_ascend": "vllm",
}


def start_engine_service(params: Dict[str, Any]) -> str:
    """
     params["engine"]  start_command.sh  shebang


      1.  engine
      2.  app.engines.<engine>_adapter
      3.  build_start_script(params)
          build_start_command exec

    Args:
        params:  engineengine_config

    Returns:
        str: bash  shebang #!/usr/bin/env bash

    Raises:
        ValueError:      params  engine
        ImportError:
        AttributeError:  build_start_script  build_start_command
    """
    engine_name = params.get("engine")
    if not engine_name:
        raise ValueError("Missing 'engine' key in params dict.")

    #  + vllm_ascend  vllm
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

    #  build_start_script
    if hasattr(adapter_module, "build_start_script"):
        logger.info("Using build_start_script from %s", adapter_module_name)
        return adapter_module.build_start_script(params)

    #  build_start_command  exec
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

