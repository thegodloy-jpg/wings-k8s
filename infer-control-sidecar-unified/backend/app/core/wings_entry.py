# =============================================================================
# File: core/wings_entry.py
# Purpose: Launcher 控制链路的中枢桥接层。
#          上游拿到的是 CLI/环境变量，下游需要的是一段可执行的 shell 脚本，
#          中间还要结合硬件探测、默认配置、用户配置和端口规划。
# Data Flow:
#   LaunchArgs + PortPlan
#     → detect_hardware()        获取硬件信息
#     → load_and_merge_configs() 多层配置合并
#     → start_engine_service()   生成 shell 脚本
#     → LauncherPlan.command     写入共享卷
# =============================================================================

"""将 launcher 参数转换成 engine 启动计划。

它是 launcher 控制链路里的中枢桥接层：
- 上游拿到的是 CLI/环境变量；
- 下游需要的是一段可执行的 shell 脚本；
- 中间还要结合硬件探测、默认配置、用户配置和端口规划。

最终产物 `LauncherPlan.command` 会被写入共享卷，供 engine 容器执行。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from app.config.settings import settings
from app.core.config_loader import load_and_merge_configs
from app.core.engine_manager import start_engine_service
from app.core.hardware_detect import detect_hardware
from app.core.port_plan import PortPlan
from app.core.start_args_compat import LaunchArgs

logger = logging.getLogger(__name__)

# ── Accel 加速包补丁选项默认值 ──────────────────────────────────────────────
# 当 ENABLE_ACCEL=true 时，sidecar 会向 start_command.sh 注入
# export WINGS_ENGINE_PATCH_OPTIONS='{"<engine>": [<features>]}'
# 告诉 wings_engine_patch 包要激活哪些补丁。
#
# 默认值按引擎名映射：
#   vllm / vllm_ascend → {"vllm": ["test_patch"]}
#   sglang             → {"sglang": ["test_patch"]}
#   mindie             → {"mindie": ["test_patch"]}
#
# 可通过 WINGS_ENGINE_PATCH_OPTIONS 环境变量覆盖（JSON 字符串），
# 此时直接使用用户提供的值，不再按引擎名自动生成。
# ────────────────────────────────────────────────────────────────────────────

# 引擎名到 patch options key 的映射（vllm_ascend 复用 vllm 的补丁体系）
_ENGINE_PATCH_KEY_MAP = {
    "vllm": "vllm",
    "vllm_ascend": "vllm",
    "sglang": "sglang",
    "mindie": "mindie",
}

# 默认的功能补丁列表（与 Main 项目 command_builder.py 保持一致）
_DEFAULT_PATCH_FEATURES = ["test_patch"]


def _shell_escape_single_quote(value: str) -> str:
    """对字符串中的单引号进行 shell 安全转义。"""
    return value.replace("'", "'\"'\"'")


def _build_accel_env_line(engine: str) -> str:
    """生成 WINGS_ENGINE_PATCH_OPTIONS 的 export 语句。

    优先使用 WINGS_ENGINE_PATCH_OPTIONS 环境变量中用户提供的值；
    若未设置，则根据引擎名自动生成默认值。
    """
    user_override = os.getenv("WINGS_ENGINE_PATCH_OPTIONS", "").strip()
    if user_override:
        try:
            json.loads(user_override)
        except json.JSONDecodeError:
            logger.warning(
                "WINGS_ENGINE_PATCH_OPTIONS is not valid JSON: %s. "
                "Falling back to auto-generated value.",
                user_override,
            )
            user_override = ""

    if user_override:
        safe_value = _shell_escape_single_quote(user_override)
        logger.info("Using user-provided WINGS_ENGINE_PATCH_OPTIONS: %s", user_override)
        return f"export WINGS_ENGINE_PATCH_OPTIONS='{safe_value}'\n"

    patch_key = _ENGINE_PATCH_KEY_MAP.get(engine)
    if not patch_key:
        logger.warning(
            "Engine '%s' has no known accel patch mapping; "
            "skipping WINGS_ENGINE_PATCH_OPTIONS injection.",
            engine,
        )
        return f"# WINGS_ENGINE_PATCH_OPTIONS: no mapping for engine '{engine}'\n"

    options = json.dumps({patch_key: _DEFAULT_PATCH_FEATURES})
    logger.info("Injecting WINGS_ENGINE_PATCH_OPTIONS for engine '%s': %s", engine, options)
    return f"export WINGS_ENGINE_PATCH_OPTIONS='{options}'\n"


@dataclass(frozen=True)
class LauncherPlan:
    """launcher 生成的最终计划。

    Attributes:
        command:       完整的 bash 启动脚本内容（含 shebang + set -euo pipefail），
                       将被写入 /shared-volume/start_command.sh 供 engine 容器执行。
        merged_params: 多层合并后的完整参数字典，便于日志审计和调试。
        hardware_env:  硬件探测结果（device/count/details），便于下游判断。
    """

    command: str
    merged_params: dict
    hardware_env: dict


def build_launcher_plan(launch_args: LaunchArgs, port_plan: PortPlan) -> LauncherPlan:
    """根据启动参数、硬件信息和端口规划生成完整启动脚本。

    执行流程：
    1. 调用 detect_hardware() 获取硬件环境（设备类型、数量、型号）
    2. 调用 load_and_merge_configs() 多层配置合并
    3. 用显式参数覆盖合并结果（engine/model_name/model_path 等）
    4. 注入分布式信息（nnodes/node_rank/head_node_addr）
    5. 根据 node_rank 决定是否注入 host/port
    6. 调用 start_engine_service() 分发给具体 adapter 生成脚本
    7. 添加 shebang + set -euo pipefail 包装成安全脚本

    Args:
        launch_args: 标准化的启动参数（来自 parse_launch_args）
        port_plan:   三层端口分配方案（来自 derive_port_plan）

    Returns:
        LauncherPlan: 包含完整 shell 脚本、合并参数和硬件信息
    """
    hardware = detect_hardware()
    known_args = launch_args.to_namespace()
    merged = load_and_merge_configs(hardware_env=hardware, known_args=known_args)

    # engine 已在 load_and_merge_configs 中经过 _auto_select_engine 的
    # 自动选择、校验和升级（如 vllm → vllm_ascend），不可用原始值覆盖。
    engine = merged.get("engine", launch_args.engine)
    merged["model_name"] = launch_args.model_name
    merged["model_path"] = launch_args.model_path

    # 分布式信息会影响后续 engine adapter 如何拼命令。
    is_distributed = getattr(launch_args, "distributed", False)
    node_rank = getattr(launch_args, "node_rank", 0)
    merged["distributed"] = is_distributed
    merged["nnodes"] = getattr(launch_args, "nnodes", 1)
    merged["node_rank"] = node_rank
    merged["head_node_addr"] = getattr(launch_args, "head_node_addr", "127.0.0.1")
    merged["distributed_executor_backend"] = getattr(
        launch_args,
        "distributed_executor_backend",
        "ray",
    )

    engine_cfg = dict(merged.get("engine_config", {}))

    # rank0 或单机场景需要显式注入 host/port，让 backend engine 真正提供服务。
    if not is_distributed or node_rank == 0:
        merged["host"] = "0.0.0.0"
        merged["port"] = port_plan.backend_port
        engine_cfg["host"] = "0.0.0.0"
        engine_cfg["port"] = port_plan.backend_port
    else:
        # 非 0 号节点一般只承担计算，不直接对外提供 engine 监听地址。
        merged.pop("host", None)
        merged.pop("port", None)
        engine_cfg.pop("host", None)
        engine_cfg.pop("port", None)

    merged["engine_config"] = engine_cfg

    # 分发给具体 adapter，生成真正的 shell 启动脚本。
    script_body = start_engine_service(merged)

    # ── Accel 加速包环境注入 ──
    accel_preamble = ""
    if settings.ENABLE_ACCEL:
        accel_preamble = _build_accel_env_line(engine)
        logger.info("Accel enabled: injecting WINGS_ENGINE_PATCH_OPTIONS into start script")
    else:
        logger.debug("Accel disabled: skipping WINGS_ENGINE_PATCH_OPTIONS injection")

    command = "#!/usr/bin/env bash\nset -euo pipefail\n" + accel_preamble + script_body
    return LauncherPlan(command=command, merged_params=merged, hardware_env=hardware)
