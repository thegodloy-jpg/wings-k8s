# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: core/wings_entry.py
# Purpose: Builds launcher plan by combining parsed args, hardware context, and merged config.
# Status: Active control-plane bridge into engine command generation.
# Responsibilities:
#   - Derive hardware environment (device type, memory, device count).
#   - Merge config layers (default json  user config  CLI args).
#   - Inject port plan (backend / proxy / health ports) into engine_config.
#   - Dispatch to the correct engine adapter via engine_manager.
# Sidecar Contracts:
#   - Generates command payload only; no engine process startup.
#   - Keeps engine config fields aligned with selected backend port.
#   - Supports engines: vllm, vllm_ascend, sglang, mindie.
# -----------------------------------------------------------------------------
from __future__ import annotations

from dataclasses import dataclass

from app.core.config_loader import load_and_merge_configs
from app.core.engine_manager import start_engine_service
from app.core.hardware_detect import detect_hardware
from app.core.port_plan import PortPlan
from app.core.start_args_compat import LaunchArgs


@dataclass(frozen=True)
class LauncherPlan:
    command: str
    merged_params: dict
    hardware_env: dict


def build_launcher_plan(launch_args: LaunchArgs, port_plan: PortPlan) -> LauncherPlan:
    """
     launcher      start_command.sh

    Args:
        launch_args:  enginemodel_namemodel_path
        port_plan:   backend_port / proxy_port / health_port

    Returns:
        LauncherPlan  start_command.sh
    """
    hardware = detect_hardware()
    known_args = launch_args.to_namespace()
    merged = load_and_merge_configs(hardware_env=hardware, known_args=known_args)

    #  engine config_loader  launch_args
    engine = launch_args.engine
    merged["engine"] = engine
    merged["model_name"] = launch_args.model_name
    merged["model_path"] = launch_args.model_path

    # Check distributed args
    is_distributed = getattr(launch_args, "distributed", False)
    node_rank = getattr(launch_args, "node_rank", 0)
    merged["distributed"] = is_distributed
    merged["nnodes"] = getattr(launch_args, "nnodes", 1)
    merged["node_rank"] = node_rank
    merged["head_node_addr"] = getattr(launch_args, "head_node_addr", "127.0.0.1")
    merged["distributed_executor_backend"] = getattr(launch_args, "distributed_executor_backend", "ray")

    engine_cfg = dict(merged.get("engine_config", {}))

    if not is_distributed or node_rank == 0:
        merged["host"] = "0.0.0.0"
        merged["port"] = port_plan.backend_port
        engine_cfg["host"] = "0.0.0.0"
        engine_cfg["port"] = port_plan.backend_port
    else:
        # rank > 0 skips host/port injection
        merged.pop("host", None)
        merged.pop("port", None)
        engine_cfg.pop("host", None)
        engine_cfg.pop("port", None)

    merged["engine_config"] = engine_cfg

    #  engine_manager
    script_body = start_engine_service(merged)

    #  bash shebang +  +
    command = "#!/usr/bin/env bash\nset -euo pipefail\n" + script_body
    return LauncherPlan(command=command, merged_params=merged, hardware_env=hardware)

