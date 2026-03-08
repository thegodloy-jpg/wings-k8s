# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: core/wings_entry.py
# Purpose: Builds launcher plan by combining parsed args, hardware context, and merged config.
# Status: Active control-plane bridge into engine command generation.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Generates command payload only; no engine process startup.
# - Keeps engine config fields aligned with selected backend port.
# -----------------------------------------------------------------------------
from __future__ import annotations

from dataclasses import dataclass

from app.core.config_loader import load_and_merge_configs
from app.core.hardware_detect import detect_hardware
from app.core.port_plan import PortPlan
from app.core.start_args_compat import LaunchArgs
from app.engines import vllm_adapter


@dataclass(frozen=True)
class LauncherPlan:
    command: str
    merged_params: dict
    hardware_env: dict


def build_launcher_plan(launch_args: LaunchArgs, port_plan: PortPlan) -> LauncherPlan:
    hardware = detect_hardware()
    known_args = launch_args.to_namespace()
    merged = load_and_merge_configs(hardware_env=hardware, known_args=known_args)

    merged["engine"] = "vllm"
    merged["model_name"] = launch_args.model_name
    merged["model_path"] = launch_args.model_path
    merged["host"] = "0.0.0.0"
    merged["port"] = port_plan.backend_port

    engine_cfg = dict(merged.get("engine_config", {}))
    engine_cfg["host"] = "0.0.0.0"
    engine_cfg["port"] = port_plan.backend_port
    merged["engine_config"] = engine_cfg

    command_core = vllm_adapter.build_start_command(merged)

    command = "#!/usr/bin/env bash\nset -euo pipefail\nexec " + command_core + "\n"
    return LauncherPlan(command=command, merged_params=merged, hardware_env=hardware)
