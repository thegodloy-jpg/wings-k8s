# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: core/wings_entry.py
# Purpose: Builds launcher plan by combining parsed args, hardware context, and merged config.
# Status: Active control-plane bridge into engine command generation.
# Responsibilities:
#   - Derive hardware environment (device type, memory, device count).
#   - Merge config layers (default json → user config → CLI args).
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
    构建 launcher 执行计划：合并配置 → 注入端口 → 生成 start_command.sh 内容。

    Args:
        launch_args: 解析后的启动参数（含 engine、model_name、model_path 等）。
        port_plan:   端口规划（backend_port / proxy_port / health_port）。

    Returns:
        LauncherPlan 包含完整的 start_command.sh 脚本文本。
    """
    hardware = detect_hardware()
    known_args = launch_args.to_namespace()
    merged = load_and_merge_configs(hardware_env=hardware, known_args=known_args)

    # 确保 engine 字段使用原始值（config_loader 可能自动选择，此处以 launch_args 优先）
    engine = launch_args.engine
    merged["engine"] = engine
    merged["model_name"] = launch_args.model_name
    merged["model_path"] = launch_args.model_path
    merged["host"] = "0.0.0.0"
    merged["port"] = port_plan.backend_port

    # 将端口注入 engine_config（各适配器均从 engine_config 读取 host/port）
    engine_cfg = dict(merged.get("engine_config", {}))
    engine_cfg["host"] = "0.0.0.0"
    engine_cfg["port"] = port_plan.backend_port
    merged["engine_config"] = engine_cfg

    # 通过 engine_manager 动态分发到正确的适配器
    script_body = start_engine_service(merged)

    # 拼装完整 bash 脚本（shebang + 安全选项 + 脚本体）
    command = "#!/usr/bin/env bash\nset -euo pipefail\n" + script_body
    return LauncherPlan(command=command, merged_params=merged, hardware_env=hardware)

