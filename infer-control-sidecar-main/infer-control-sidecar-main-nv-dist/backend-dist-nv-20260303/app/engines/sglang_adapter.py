# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
SGLang sidecar launcher

 sidecar launcher
 vllm-engine sglang-engine start_command.sh


  - build_start_command(params) -> str
  - build_start_script(params)  -> str    bash  shebang
"""

import logging
import os
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _build_base_env_commands(params: Dict[str, Any], root: str) -> List[str]:
    """ sglang """
    env_script = os.path.join(root, "wings", "config", "set_sglang_env.sh")
    if os.path.exists(env_script):
        return [f"source {env_script}"]
    return []


def _build_sglang_cmd_parts(params: Dict[str, Any]) -> str:
    """
     engine_config  sglang.launch_server


    - key tp_size -> --tp-size
    - bool True  ->  flag--disable-log-stats
    - bool False ->
    - JSON  ->
    """
    engine_config = params.get("engine_config", {})

    # python3  /usr/bin/python
    cmd_parts = ["python3", "-m", "sglang.launch_server"]

    for arg, value in engine_config.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            #  --model-path
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


def build_start_command(params: Dict[str, Any]) -> str:
    """
     launcher  SGLang  shebang/env setup



    """
    cmd = _build_sglang_cmd_parts(params)
    is_distributed = params.get("distributed", False)
    nnodes = params.get("nnodes", 1)
    node_rank = params.get("node_rank", 0)
    head_node_addr = params.get("head_node_addr", "127.0.0.1")

    if is_distributed and nnodes > 1:
        cmd += f" --nnodes {nnodes} --node-rank {node_rank}"
        if ":" in head_node_addr:
            cmd += f" --dist-init-addr {head_node_addr}"
        else:
            cmd += f" --dist-init-addr {head_node_addr}:28030"

    return cmd


def build_start_script(params: Dict[str, Any]) -> str:
    """
     bash start_command.sh  shebang

     sglang
        [env setup if applicable]
        exec python3 -m sglang.launch_server --model-path ... --host ... --port ...
    """
    env_cmds = _build_base_env_commands(params, root_dir)
    core_cmd = build_start_command(params)

    lines: List[str] = []
    lines.extend(env_cmds)
    lines.append(f"exec {core_cmd}")
    return "\n".join(lines) + "\n"


def start_engine(params: Dict[str, Any]):
    """


    sidecar launcher  adapter
    """
    raise RuntimeError(
        "start_engine is disabled in launcher mode. "
        "Use build_start_command() / build_start_script() and write to shared volume instead."
    )

