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
    """构建 ServerConfig 节点（兼容 MindIE 2.2.RC1）。"""
    main_port: int = engine_config.get("port", DEFAULT_SERVER_PORT)
    cfg: Dict[str, Any] = {
        "ipAddress": engine_config.get("ipAddress", "127.0.0.1"),
        "managementIpAddress": engine_config.get("managementIpAddress", "127.0.0.1"),
        "port": main_port,
        "managementPort": engine_config.get("managementPort", main_port + 1),
        "metricsPort": engine_config.get("metricsPort", main_port + 2),
        "maxLinkNum": engine_config.get("maxLinkNum", 1000),
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
    """构建 ModelConfig[0] 节点（兼容 MindIE 2.2.RC1）。"""
    world_size = engine_config.get("worldSize", 8 if is_distributed else 1)
    model_cfg: Dict[str, Any] = {
        "modelInstanceType": engine_config.get("modelInstanceType", "Standard"),
        "modelName": engine_config.get("modelName", "default_llm"),
        "modelWeightPath": engine_config.get("modelWeightPath", ""),
        "worldSize": world_size,
        "cpuMemSize": engine_config.get("cpuMemSize", 5),
        "npuMemSize": engine_config.get("npuMemSize", -1),
        "backendType": engine_config.get("backendType", "atb"),
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
    """构建 ScheduleConfig 节点（字段名对齐 MindIE 2.2.RC1 原始 config.json）。"""
    return {
        # MindIE 2.2.RC1 原始必须字段，缺少则 daemon 自动插入 null → 崩溃
        "templateType": engine_config.get("templateType", "Standard"),
        "templateName": engine_config.get("templateName", "Standard_LLM"),
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
        "maxFirstTokenWaitTime": engine_config.get("maxFirstTokenWaitTime", 2500),
    }


def _build_mindie_config_dict(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    从合并后的 params（经 config_loader._merge_mindie_params 处理）
    组装完整的 Mindie config.json 字典。
    """
    engine_config = params.get("engine_config", {})

    # npuDeviceIds: 支持 MINDIE_NPU_DEVICE_IDS 环境变量覆盖（格式: JSON，例如 "[[1]]"）
    npu_device_ids = engine_config.get("npuDeviceIds", None)
    if npu_device_ids is None:
        npu_ids_env = os.getenv("MINDIE_NPU_DEVICE_IDS", "")
        if npu_ids_env:
            try:
                npu_device_ids = json.loads(npu_ids_env)
            except (json.JSONDecodeError, ValueError):
                logger.warning("MINDIE_NPU_DEVICE_IDS parse error: %s, fallback to [[0]]", npu_ids_env)
                npu_device_ids = [[0]]
        else:
            npu_device_ids = [[0]]

    return {
        "Version": "1.0.0",
        # LogConfig: MindIE 2.2.RC1 原始格式只有这3个字段。
        # ‼ dynamicLogLevel 必须是字符串 ""，不能是布尔值；
        #   缺少任一字段时 daemon 会自动插入 null → JSON 类型错误崩溃。
        "LogConfig": {
            "dynamicLogLevel": engine_config.get("dynamicLogLevel", ""),
            "dynamicLogLevelValidHours": engine_config.get("dynamicLogLevelValidHours", 2),
            "dynamicLogLevelValidTime": engine_config.get("dynamicLogLevelValidTime", ""),
        },
        CONFIG_SERVER: _build_server_config(engine_config),
        CONFIG_BACKEND: {
            "backendName": engine_config.get("backendName", "mindieservice_llm_engine"),
            "modelInstanceNumber": engine_config.get("modelInstanceNumber", 1),
            "npuDeviceIds": npu_device_ids,
            "tokenizerProcessNumber": engine_config.get("tokenizerProcessNumber", 1),
            "multiNodesInferEnabled": engine_config.get("multiNodesInferEnabled", False),
            CONFIG_MODEL_DEPLOY: _build_model_deploy_config(engine_config),
            CONFIG_SCHEDULE: _build_schedule_config(engine_config),
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# 内部：构建环境变量命令行列表
# ──────────────────────────────────────────────────────────────────────────────

def _build_env_commands(params: Dict[str, Any]) -> List[str]:
    """
    构建单机 MindIE 环境初始化命令。

    在容器内（MindIE 2.2.RC1 镜像）需要 source:
      1. /usr/local/Ascend/ascend-toolkit/set_env.sh  — CANN toolkit 环境
      2. /usr/local/Ascend/mindie/set_env.sh           — MindIE 服务 + LLM 环境
    两者均可能引用未定义变量（ZSH_VERSION 等），需在 set +u / set -u 中包裹。
    """
    cmds: List[str] = []

    # 先尝试本地 wings 工程的 set_env（开发环境/测试用，容器内通常不存在）
    env_script = os.path.join(root_dir, "wings", "config", "set_mindie_single_env.sh")
    if os.path.exists(env_script):
        cmds.append(f"source {env_script}")
    else:
        # 容器内标准路径：CANN toolkit + MindIE 环境
        # set +u / set -u 包裹：防止 env 脚本引用 ZSH_VERSION 等未绑定变量
        cmds.append("# set +u: Ascend env scripts may reference unbound vars (e.g. ZSH_VERSION)")
        cmds.append("set +u")
        cmds.append(
            "[ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ] "
            "&& source /usr/local/Ascend/ascend-toolkit/set_env.sh "
            "|| echo 'WARN: ascend-toolkit/set_env.sh not found'"
        )
        cmds.append(
            "[ -f /usr/local/Ascend/mindie/set_env.sh ] "
            "&& source /usr/local/Ascend/mindie/set_env.sh "
            "|| echo 'WARN: mindie/set_env.sh not found'"
        )
        cmds.append("set -u")

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
      2. **读取镜像内原始 config.json → merge 覆盖** → 写回
         （与原始 wings `_update_mindie_config` / `_update_single_config` 逻辑一致：
          保留 LogConfig、ScheduleConfig.templateType 等原始字段，仅覆盖需要
          修改的字段，避免 daemon 自动插入 null → JSON 类型错误崩溃）
      3. 启动 mindieservice_daemon

    脚本执行时无外部依赖，覆盖参数全量内嵌于脚本中。
    """
    # 1. 构建需要覆盖的参数字典
    engine_config = params.get("engine_config", {})

    # npuDeviceIds: 支持 MINDIE_NPU_DEVICE_IDS 环境变量覆盖
    npu_device_ids = engine_config.get("npuDeviceIds", None)
    if npu_device_ids is None:
        npu_ids_env = os.getenv("MINDIE_NPU_DEVICE_IDS", "")
        if npu_ids_env:
            try:
                npu_device_ids = json.loads(npu_ids_env)
            except (json.JSONDecodeError, ValueError):
                logger.warning("MINDIE_NPU_DEVICE_IDS parse error: %s, fallback to [[0]]", npu_ids_env)
                npu_device_ids = [[0]]
        else:
            npu_device_ids = [[0]]

    # ServerConfig 覆盖参数
    main_port: int = engine_config.get("port", DEFAULT_SERVER_PORT)
    server_overrides = {
        "ipAddress": engine_config.get("ipAddress", "0.0.0.0"),
        "port": main_port,
        "httpsEnabled": engine_config.get("httpsEnabled", False),
        "inferMode": engine_config.get("inferMode", "standard"),
        "openAiSupport": engine_config.get("openAiSupport", "vllm"),
        "tokenTimeout": engine_config.get("tokenTimeout", 600),
        "e2eTimeout": engine_config.get("e2eTimeout", 600),
        "allowAllZeroIpListening": engine_config.get("allowAllZeroIpListening", True),
    }

    # BackendConfig 覆盖参数
    backend_overrides = {
        "npuDeviceIds": npu_device_ids,
    }

    # ModelDeployConfig 覆盖参数
    model_deploy_overrides = {
        "maxSeqLen": engine_config.get("maxSeqLen", 4096),
        "maxInputTokenLen": engine_config.get("maxInputTokenLen", 2048),
        "truncation": engine_config.get("truncation", False),
    }

    # ModelConfig[0] 覆盖参数
    world_size = engine_config.get("worldSize", 1)
    model_config_overrides = {
        "modelName": engine_config.get("modelName", "default_llm"),
        "modelWeightPath": engine_config.get("modelWeightPath", ""),
        "worldSize": world_size,
        "cpuMemSize": engine_config.get("cpuMemSize", 5),
        "npuMemSize": engine_config.get("npuMemSize", -1),
        "trustRemoteCode": engine_config.get("trustRemoteCode", True),
    }
    if engine_config.get("isMOE", False):
        model_config_overrides.update({
            "tp": engine_config.get("tp", world_size),
            "dp": engine_config.get("dp", -1),
            "moe_tp": engine_config.get("moe_tp", world_size),
            "moe_ep": engine_config.get("moe_ep", -1),
        })
    if engine_config.get("isMTP", False):
        model_config_overrides["plugin_params"] = {
            "plugin_type": "mtp",
            "num_speculative_tokens": 1,
        }

    # ScheduleConfig 覆盖参数
    schedule_overrides = {
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
    }

    # 将所有覆盖参数序列化为 JSON（用于嵌入 bash heredoc）
    overrides_dict = {
        "server": server_overrides,
        "backend": backend_overrides,
        "model_deploy": model_deploy_overrides,
        "model_config": model_config_overrides,
        "schedule": schedule_overrides,
    }
    overrides_json = json.dumps(overrides_dict, indent=2, ensure_ascii=False)

    # 2. 环境命令
    env_cmds = _build_env_commands(params)
    env_block = "\n".join(env_cmds) + "\n" if env_cmds else ""

    # 3. 构建内联脚本片段
    # 策略：读取镜像内原始 config.json → merge 覆盖参数 → 写回
    # （与原始 wings _update_single_config 逻辑完全一致，保留 LogConfig 等原始字段）
    script = f"""{env_block}# ── Merge-update Mindie config.json（保留原始字段，仅覆盖需修改项）──
# 读取镜像内默认 config.json，merge 覆盖参数后写回
# 这确保 LogConfig / ScheduleConfig.templateType 等原始字段不丢失
cat > /tmp/_mindie_overrides.json << 'OVERRIDES_EOF'
{overrides_json}
OVERRIDES_EOF

python3 << 'MERGE_SCRIPT_EOF'
import json, os, sys

CONFIG_PATH = '{MINDIE_CONFIG_PATH}'
OVERRIDES_PATH = '/tmp/_mindie_overrides.json'

# 1. 读取镜像内原始 config.json
try:
    with open(CONFIG_PATH, 'r') as f:
        config = json.load(f)
    print(f'[mindie] Loaded original config.json ({{len(json.dumps(config))}} bytes)')
except Exception as e:
    print(f'[mindie] ERROR: Cannot read {{CONFIG_PATH}}: {{e}}', file=sys.stderr)
    sys.exit(1)

# 2. 读取覆盖参数
with open(OVERRIDES_PATH, 'r') as f:
    ov = json.load(f)

# 3. Merge 覆盖（与 wings _update_single_config 策略一致）
# ServerConfig: .update() 保留原始字段
if 'ServerConfig' in config:
    config['ServerConfig'].update(ov['server'])

# BackendConfig
if 'BackendConfig' in config:
    bc = config['BackendConfig']
    bc.update(ov['backend'])

    # ModelDeployConfig
    if 'ModelDeployConfig' in bc:
        bc['ModelDeployConfig'].update(ov['model_deploy'])
        # ModelConfig[0]
        if 'ModelConfig' in bc['ModelDeployConfig'] and bc['ModelDeployConfig']['ModelConfig']:
            bc['ModelDeployConfig']['ModelConfig'][0].update(ov['model_config'])

    # ScheduleConfig
    if 'ScheduleConfig' in bc:
        bc['ScheduleConfig'].update(ov['schedule'])

# 4. 写回
with open(CONFIG_PATH, 'w') as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
os.chmod(CONFIG_PATH, 0o640)

print('[mindie] config.json merge-updated successfully')
print(json.dumps(config, indent=2, ensure_ascii=False))
MERGE_SCRIPT_EOF

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
