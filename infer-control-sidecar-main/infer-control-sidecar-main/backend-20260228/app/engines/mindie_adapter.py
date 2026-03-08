# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
Mindie (华为昇腾) 推理引擎适配器（sidecar launcher 模式）

在 sidecar launcher 模式下，此模块：
  1. 根据合并后的 engine_config 构建完整的 Mindie config.json 内容；
  2. 生成包含"写 config.json + 启动 daemon"两步骤的 bash 脚本体（start_command.sh）。

引擎进程由 mindie-engine 容器读取 start_command.sh 后自行启动，launcher 不直接拉起进程。

对外接口：
  - build_start_command(params) -> str   返回核心启动命令字符串（不含 config 写入步骤）
  - build_start_script(params)  -> str   返回完整 bash 脚本体（含 config 写入 + daemon 启动）
"""

import json
import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ──────────────────────────────────────────────────────────────────────────────
# Mindie 服务默认路径和常量（可通过环境变量覆写）
# ──────────────────────────────────────────────────────────────────────────────
MINDIE_WORK_DIR: str = os.getenv(
    "MINDIE_WORK_DIR",
    "/usr/local/Ascend/mindie/latest/mindie-service"
)
MINDIE_CONFIG_PATH: str = os.getenv(
    "MINDIE_CONFIG_PATH",
    os.path.join(MINDIE_WORK_DIR, "conf/config.json")
)

CONFIG_SERVER = "ServerConfig"
CONFIG_BACKEND = "BackendConfig"
CONFIG_MODEL_DEPLOY = "ModelDeployConfig"
CONFIG_SCHEDULE = "ScheduleConfig"
DEFAULT_SERVER_PORT = 18000


# ──────────────────────────────────────────────────────────────────────────────
# 内部：从 engine_config 构建完整 Mindie config.json 字典
# ──────────────────────────────────────────────────────────────────────────────

def _build_server_config(engine_config: Dict[str, Any], is_distributed: bool = False) -> Dict[str, Any]:
    """构建 ServerConfig 节点。"""
    cfg: Dict[str, Any] = {
        "ipAddress": engine_config.get("ipAddress", "127.0.0.1"),
        "port": engine_config.get("port", DEFAULT_SERVER_PORT),
        "httpsEnabled": engine_config.get("httpsEnabled", False),
        "inferMode": engine_config.get("inferMode", "standard"),
        "openAiSupport": engine_config.get("openAiSupport", "vllm"),
        "tokenTimeout": engine_config.get("tokenTimeout", 600),
        "e2eTimeout": engine_config.get("e2eTimeout", 600),
    }
    if is_distributed:
        cfg.update({
            "interCommTLSEnabled": engine_config.get("interCommTLSEnabled", False),
        })
    else:
        cfg["allowAllZeroIpListening"] = engine_config.get("allowAllZeroIpListening", True)
    return cfg


def _build_model_config(engine_config: Dict[str, Any], is_distributed: bool = False) -> Dict[str, Any]:
    """构建 ModelConfig[0] 节点。"""
    world_size = engine_config.get("worldSize", 8 if is_distributed else 1)
    model_cfg: Dict[str, Any] = {
        "modelName": engine_config.get("modelName", "default_llm"),
        "modelWeightPath": engine_config.get("modelWeightPath", ""),
        "worldSize": world_size,
        "cpuMemSize": engine_config.get("cpuMemSize", 5),
        "npuMemSize": engine_config.get("npuMemSize", -1),
        "trustRemoteCode": engine_config.get("trustRemoteCode", True),
    }
    if engine_config.get("isMOE", False):
        model_cfg.update({
            "tp": engine_config.get("tp", world_size),
            "dp": engine_config.get("dp", -1),
            "moe_tp": engine_config.get("moe_tp", world_size),
            "moe_ep": engine_config.get("moe_ep", -1),
        })
    if engine_config.get("isMTP", False):
        model_cfg["plugin_params"] = {
            "plugin_type": "mtp",
            "num_speculative_tokens": 1,
        }
    return model_cfg


def _build_model_deploy_config(engine_config: Dict[str, Any], is_distributed: bool = False) -> Dict[str, Any]:
    """构建 ModelDeployConfig 节点。"""
    return {
        "maxSeqLen": engine_config.get("maxSeqLen", 4096),
        "maxInputTokenLen": engine_config.get("maxInputTokenLen", 2048),
        "truncation": engine_config.get("truncation", False),
        "ModelConfig": [_build_model_config(engine_config, is_distributed)],
    }


def _build_schedule_config(engine_config: Dict[str, Any]) -> Dict[str, Any]:
    """构建 ScheduleConfig 节点。"""
    return {
        "cacheBlockSize": engine_config.get("cacheBlockSize", 128),
        "maxPrefillBatchSize": engine_config.get("maxPrefillBatchSize", 50),
        "maxPrefillTokens": engine_config.get("maxPrefillTokens", 8192),
        "prefillTimeMsPerReq": engine_config.get("prefillTimeMsPerReq", 150),
        "prefillPolicyType": engine_config.get("prefillPolicyType", 0),
        "decodeTimeMsPerReq": engine_config.get("decodeTimeMsPerReq", 50),
        "decodePolicyType": engine_config.get("decodePolicyType", 0),
        "maxBatchSize": engine_config.get("maxBatchSize", 200),
        "maxIterTimes": engine_config.get("maxIterTimes", 512),
        "maxPreemptCount": engine_config.get("maxPreemptCount", 0),
        "supportSelectBatch": engine_config.get("supportSelectBatch", False),
        "maxQueueDelayMicroseconds": engine_config.get("maxQueueDelayMicroseconds", 5000),
        "bufferResponseEnabled": engine_config.get("bufferResponseEnabled", False),
        "decodeExpectedTime": engine_config.get("decodeExpectedTime", 50),
        "prefillExpectedTime": engine_config.get("prefillExpectedTime", 1500),
    }


def _build_mindie_config_dict(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    从合并后的 params（经 config_loader._merge_mindie_params 处理）
    组装完整的 Mindie config.json 字典。
    """
    engine_config = params.get("engine_config", {})

    return {
        CONFIG_SERVER: _build_server_config(engine_config),
        CONFIG_BACKEND: {
            "npuDeviceIds": engine_config.get(
                "npuDeviceIds", [[0]]
            ),
            CONFIG_MODEL_DEPLOY: _build_model_deploy_config(engine_config),
            CONFIG_SCHEDULE: _build_schedule_config(engine_config),
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# 内部：构建环境变量命令行列表
# ──────────────────────────────────────────────────────────────────────────────

def _build_env_commands(params: Dict[str, Any]) -> List[str]:
    """构建单机 Mindie 环境初始化命令。"""
    env_script = os.path.join(root_dir, "wings", "config", "set_mindie_single_env.sh")
    cmds: List[str] = []
    if os.path.exists(env_script):
        cmds.append(f"source {env_script}")

    npu_memory_fraction = (params.get("engine_config") or {}).get("npu_memory_fraction")
    if npu_memory_fraction is not None:
        cmds.append(f"export NPU_MEMORY_FRACTION={npu_memory_fraction}")

    return cmds


# ──────────────────────────────────────────────────────────────────────────────
# 对外接口
# ──────────────────────────────────────────────────────────────────────────────

def build_start_command(params: Dict[str, Any]) -> str:
    """
    返回 Mindie daemon 的核心启动命令字符串。

    注意：此命令单独无法完成完整启动（需先写好 config.json）。
    推荐调用 build_start_script 以获取完整脚本。
    """
    work_dir = MINDIE_WORK_DIR
    return f"cd {work_dir} && exec ./bin/mindieservice_daemon"


def build_start_script(params: Dict[str, Any]) -> str:
    """
    返回完整的 bash 脚本体（start_command.sh，不含 shebang），包含：
      1. 环境变量设置
      2. 内联 Python 写入 Mindie config.json
      3. 启动 mindieservice_daemon

    脚本执行时无外部依赖，config.json 全量内嵌于脚本中。
    """
    # 1. 构建 config 字典并序列化
    config_dict = _build_mindie_config_dict(params)
    config_json = json.dumps(config_dict, indent=2, ensure_ascii=False)

    # 2. 环境命令
    env_cmds = _build_env_commands(params)
    env_block = "\n".join(env_cmds) + "\n" if env_cmds else ""

    # 3. 构建内联脚本片段
    # 使用 cat heredoc 写入 JSON，避免 python3 -c 多层引号嵌套问题
    config_escaped = config_json.replace("'", "'\\''")  # shell-escape single quotes
    script = f"""{env_block}# ── Write Mindie config.json ────────────────────────────────────
cat > '{MINDIE_CONFIG_PATH}' << 'MINDIE_CONFIG_EOF'
{config_json}
MINDIE_CONFIG_EOF
chmod 640 '{MINDIE_CONFIG_PATH}'

# ── Start Mindie daemon ─────────────────────────────────────────
cd '{MINDIE_WORK_DIR}'
exec ./bin/mindieservice_daemon
"""
    return script


def start_engine(params: Dict[str, Any]):
    """
    兼容旧接口。

    sidecar launcher 模式下禁止由 adapter 直接拉起进程。
    """
    raise RuntimeError(
        "start_engine is disabled in launcher mode. "
        "Use build_start_script() and write to shared volume instead."
    )
