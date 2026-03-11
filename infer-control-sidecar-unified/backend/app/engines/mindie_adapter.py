# =============================================================================
# 文件: engines/mindie_adapter.py
# 用途: MindIE（华为昇腾）推理引擎适配器
# 状态: 活跃适配器
#
# 功能概述:
#   本模块负责将统一的参数字典转换为 MindIE 的配置和启动命令。
#   MindIE 是华为昇腾 NPU 上的优化推理服务。
#
# 与 vLLM/SGLang 的差异:
#   - MindIE 使用 JSON 配置文件 (conf/config.json)，而非 CLI 参数
#   - 需要在运行时合并更新 config.json，保留镜像原有配置
#   - 分布式模式需要生成 HCCL rank table 文件
#
# 支持的部署模式:
#   - 单节点 TP (张量并行): 多张 NPU 卡分担模型层
#   - 多节点 DP (数据并行): 多节点分担请求负载
#
# 核心接口:
#   - build_start_script(params) : 返回完整 bash 脚本（含配置合并 + 启动命令）
#   - build_start_command(params): 返回核心启动命令（仅启动 daemon）
#   - start_engine(params)       : 已禁用，sidecar 模式不允许直接启动进程
#
# 生成的脚本结构:
#   1. source Ascend CANN 和 MindIE 环境脚本
#   2. 设置分布式环境变量 (HCCL/MASTER_ADDR等) [多节点时]
#   3. 写入 HCCL rank table 文件 [多节点时]
#   4. 通过内联 Python 合并更新 conf/config.json
#   5. 启动 mindieservice_daemon
#
# Sidecar 架构契约:
#   - build_start_script 是 launcher 唯一调用的入口
#   - 生成的脚本写入共享卷，由 engine 容器执行
#   - 不得重新引入直接进程启动逻辑
#
# =============================================================================
# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
MindIE 引擎适配器。

在 sidecar launcher 模式下，本模块执行以下工作：
  1. 组装需要应用到 MindIE config.json 的配置覆盖；
  2. 生成 bash 脚本体 (start_command.sh)，包含：
       a. 加载 Ascend CANN / MindIE 环境脚本
       b. 注入分布式环境变量 (HCCL / MASTER_ADDR等) [多节点时]
       c. 通过内联 Python 片段合并更新 conf/config.json
       d. 启动 mindieservice_daemon
"""

import json
import logging
import os
import shlex
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

import re

# 模块根目录：用于定位本地开发环境的环境脚本
root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _sanitize_shell_path(path: str) -> str:
    """对路径进行 shell 安全转义，防止命令注入攻击。

    使用 shlex.quote() 进行标准 POSIX shell 转义，
    相比简单的正则过滤更安全且不会破坏包含空格的合法路径。

    注意：返回值已包含外部引号（如 '/path/to/file'），
    嵌入 shell 脚本时不需要再加引号。

    Args:
        path: 原始文件路径字符串

    Returns:
        str: 经过 shell 安全转义的路径
    """
    return shlex.quote(path)


# =============================================================================
# MindIE 服务路径常量（可通过环境变量覆盖）
#
# MINDIE_WORK_DIR:    MindIE 服务工作目录，包含 bin/、conf/ 等子目录
# MINDIE_CONFIG_PATH: MindIE 配置文件路径，会在启动前被合并更新
#
# 环境变量覆盖：
#   - MINDIE_WORK_DIR:   自定义工作目录
#   - MINDIE_CONFIG_PATH: 自定义配置文件路径
# =============================================================================
MINDIE_WORK_DIR: str = os.getenv(
    "MINDIE_WORK_DIR",
    "/usr/local/Ascend/mindie/latest/mindie-service"
)
MINDIE_CONFIG_PATH: str = os.getenv(
    "MINDIE_CONFIG_PATH",
    os.path.join(MINDIE_WORK_DIR, "conf/config.json")
)

# 默认端口配置
DEFAULT_SERVER_PORT = 18000              # MindIE HTTP API 端口
DEFAULT_MINDIE_MASTER_PORT = int(os.getenv("MINDIE_MASTER_PORT", "27070"))  # 分布式主节点端口


# =============================================================================
# 内部函数：构建环境设置命令列表
# =============================================================================

def _build_env_commands(params: Dict[str, Any]) -> List[str]:
    """构建 MindIE 环境初始化命令列表（CANN 工具包 + MindIE set_env.sh）。

    MindIE 容器中的环境脚本可能引用未绑定变量（如 ZSH_VERSION），
    因此必须在 set +u / set -u 守卫块内加载。

    环境加载顺序：
    1. 本地开发环境：优先加载 wings/config/set_mindie_single_env.sh
    2. 容器环境：加载标准路径的 ascend-toolkit 和 mindie 环境脚本

    Args:
        params: 参数字典，可包含 engine_config.npu_memory_fraction

    Returns:
        List[str]: shell 命令列表，每个元素是一条环境设置命令

    生成的命令示例:
        set +u
        [ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ] && source ... || echo 'WARN: ...'
        [ -f /usr/local/Ascend/mindie/set_env.sh ] && source ... || echo 'WARN: ...'
        set -u
        export NPU_MEMORY_FRACTION=0.9
    """
    cmds: List[str] = []

    # Dev environment: prefer local wings project env script if available
    env_script = os.path.join(root_dir, "wings", "config", "set_mindie_single_env.sh")
    if os.path.exists(env_script):
        cmds.append(f"source {env_script}")
    else:
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
        # Additional env scripts required by MindIE (per official boot.sh)
        cmds.append(
            "[ -f /usr/local/Ascend/atb-models/set_env.sh ] "
            "&& source /usr/local/Ascend/atb-models/set_env.sh "
            "|| echo 'WARN: atb-models/set_env.sh not found'"
        )
        cmds.append(
            "[ -f /usr/local/Ascend/nnal/atb/set_env.sh ] "
            "&& source /usr/local/Ascend/nnal/atb/set_env.sh "
            "|| echo 'WARN: nnal/atb/set_env.sh not found'"
        )
        cmds.append("set -u")
        # Driver library paths required for NPU device access
        cmds.append(
            "export LD_LIBRARY_PATH=\"/usr/local/Ascend/driver/lib64/driver"
            ":/usr/local/Ascend/driver/lib64/common:${LD_LIBRARY_PATH:-}\""
        )
        cmds.append("export GRPC_POLL_STRATEGY=poll")

    npu_memory_fraction = (params.get("engine_config") or {}).get("npu_memory_fraction")
    if npu_memory_fraction is not None:
        cmds.append(f"export NPU_MEMORY_FRACTION={npu_memory_fraction}")

    return cmds


def _build_distributed_env_commands(params: Dict[str, Any]) -> List[str]:
    """构建多节点 MindIE 分布式环境变量设置命令。

    昇腾多节点 HCCL 通信所需的环境变量：
      MASTER_ADDR           - rank-0 节点的 IP 地址 (head_node_addr)
      MASTER_PORT           - 集合通信初始化端口 (默认 27070)
      RANK                  - 当前节点的编号
      WORLD_SIZE            - 总节点数
      HCCL_WHITELIST_DISABLE - 禁用 HCCL 白名单检查 (容器环境必需)
      HCCL_IF_IP            - HCCL 网络接口 IP (自动检测或由 node_ips 提供)
      RANK_TABLE_FILE       - HCCL rank table JSON 文件路径 (MindIE 多节点必需)

    单节点或非分布式模式时返回空列表。

    Args:
        params: 参数字典，包含以下关键字段:
            - distributed:        是否分布式模式
            - nnodes:             总节点数
            - node_rank:          当前节点编号
            - mindie_master_addr: 主节点地址 (可缺省，回退到 head_node_addr)
            - mindie_master_port: 主节点端口
            - device_count:       每节点设备数
            - node_ips:           所有节点 IP 列表 (逗号分隔)

    Returns:
        List[str]: 分布式环境变量设置命令列表

    注意:
        - MindIE 的 worldSize 设为本地 TP 并行度 (设备数)
        - 跨节点 DP 并行由 wings-infer sidecar 外部协调
        - rank table 仅包含本节点信息，使 worldSize % n_nodes == 0 校验通过
    """
    is_distributed = params.get("distributed", False)
    nnodes = params.get("nnodes", 1)
    if not is_distributed or nnodes <= 1:
        return []

    node_rank = params.get("node_rank", 0)
    master_addr = params.get("mindie_master_addr") or params.get("head_node_addr", "127.0.0.1")
    master_port = params.get("mindie_master_port", DEFAULT_MINDIE_MASTER_PORT)
    device_count = params.get("device_count", 1)

    # Determine HCCL_IF_IP: use node_ips list if available, else fallback
    node_ips_str = params.get("node_ips", "")
    node_ips_list = [ip.strip() for ip in node_ips_str.split(",") if ip.strip()] if node_ips_str else []
    if node_rank < len(node_ips_list):
        hccl_if_ip_cmd = f'export HCCL_IF_IP={node_ips_list[node_rank]}'
    else:
        # Fallback: try hostname -i, then python3 socket, then master_addr
        hccl_if_ip_cmd = (
            f"export HCCL_IF_IP=$(hostname -i 2>/dev/null "
            f"|| python3 -c 'import socket; print(socket.gethostbyname(socket.gethostname()))' 2>/dev/null "
            f"|| echo {master_addr})"
        )

    # Determine this node's container IP for MIES_CONTAINER_IP
    if node_rank < len(node_ips_list):
        container_ip = node_ips_list[node_rank]
    else:
        container_ip = master_addr

    # ── Generate HCCL rank table file ────────────────────────────────────
    # MindIE uses the rank table to determine n_nodes and validates
    # worldSize % n_nodes == 0.  Since worldSize = device_count (local TP),
    # and n_nodes = server_count from rank table, we generate a SINGLE-NODE
    # rank table containing only THIS node's entry.  This gives n_nodes=1
    # so worldSize(1) % 1 == 0 passes validation.
    # For cross-node DP, coordination is handled by wings-infer sidecar.
    ranktable_path = "/tmp/hccl_ranktable.json"
    # Use only the current node's IP for the single-node rank table
    local_node_ip = node_ips_list[node_rank] if node_rank < len(node_ips_list) else master_addr
    rank_table_cmds = _build_rank_table_commands(
        [local_node_ip],        # Single-node: only this node
        device_count,
        ranktable_path,
        node_offset=node_rank,  # Pass node index for HCCL device IPs lookup
    )

    return [
        "# ── Ascend HCCL distributed env vars (single-node TP, cross-node DP) ──",
        f"export MASTER_ADDR={master_addr}",
        f"export MASTER_PORT={master_port}",
        f"export RANK={node_rank}",
        f"export WORLD_SIZE={nnodes}",
        "export HCCL_WHITELIST_DISABLE=1",
        hccl_if_ip_cmd,
        f"export HCCL_SOCKET_IFNAME={os.getenv('HCCL_SOCKET_IFNAME', 'eth0')}",
        f"export GLOO_SOCKET_IFNAME={os.getenv('GLOO_SOCKET_IFNAME', 'eth0')}",
        f"export MIES_CONTAINER_IP={container_ip}",
    ] + rank_table_cmds + [
        f"export RANK_TABLE_FILE={ranktable_path}",
    ]


def _build_rank_table_commands(
    node_ips: List[str],
    device_count: int,
    output_path: str,
    node_offset: int = 0,
) -> List[str]:
    """生成写入 HCCL rank table JSON 文件的 shell 命令。

    MindIE 分布式模式 (DP) 需要 HCCL rank table 来确定节点数量。
    本函数生成一个仅包含当前节点设备的单节点 rank table。

    设计原因：
      - MindIE 使用 rank table 中的 server_count 确定 n_nodes
      - MindIE 会校验 worldSize % n_nodes == 0
      - 通过设置 server_count=1，使 worldSize(设备数) % 1 == 0 始终通过
      - 跨节点 DP 协调由 wings-infer sidecar 外部处理

    Args:
        node_ips:     节点 IP 列表（当前实现仅使用第一个）
        device_count: 每节点的 NPU 设备数量
        output_path:  rank table JSON 文件输出路径
        node_offset:  本节点在全局 HCCL_DEVICE_IPS 列表中的索引，
                      用于查找正确的设备 IP

    Returns:
        List[str]: 写入 rank table 文件的 shell 命令列表

    HCCL_DEVICE_IPS 环境变量格式：
        "ip0,ip1;ip2,ip3"  (分号分隔节点，逗号分隔设备)
        若未设置，则回退使用节点主机 IP

    生成的 rank table 结构：
        {
          "version": "1.0",
          "server_count": "1",
          "server_list": [
            {
              "server_id": "<node_ip>",
              "device": [
                {"device_id": "0", "device_ip": "<ip>", "rank_id": "0"},
                ...
              ],
              "container_ip": "<node_ip>",
              "host_nic_ip": "<node_ip>"
            }
          ],
          "status": "completed"
        }
    """
    # ── Parse HCCL device IPs from env var ──────────────────────────────────
    hccl_device_ips_str = os.environ.get("HCCL_DEVICE_IPS", "")
    node_device_ips: List[List[str]] = []
    if hccl_device_ips_str:
        for node_part in hccl_device_ips_str.split(";"):
            ips = [ip.strip() for ip in node_part.split(",") if ip.strip()]
            if ips:
                node_device_ips.append(ips)
        logger.info("[mindie] HCCL_DEVICE_IPS parsed: %s", node_device_ips)

    server_list = []
    global_rank = 0
    for local_idx, ip in enumerate(node_ips):
        devices = []
        # Use node_offset to look up this node's HCCL device IPs
        hccl_node_idx = node_offset + local_idx
        for dev_id in range(device_count):
            if hccl_node_idx < len(node_device_ips) and dev_id < len(node_device_ips[hccl_node_idx]):
                device_ip = node_device_ips[hccl_node_idx][dev_id]
            else:
                device_ip = ip
                logger.warning(
                    "[mindie] No HCCL device IP for node=%d dev=%d, fallback to host IP %s",
                    hccl_node_idx, dev_id, ip,
                )
            devices.append({
                "device_id": str(dev_id),
                "device_ip": device_ip,
                "rank_id": str(global_rank),
            })
            global_rank += 1
        server_list.append({
            "server_id": ip,
            "device": devices,
            "container_ip": ip,
            "host_nic_ip": ip,
        })

    rank_table = {
        "version": "1.0",
        "server_count": str(len(node_ips)),
        "server_list": server_list,
        "status": "completed",
    }
    rank_table_json = json.dumps(rank_table, indent=2, ensure_ascii=False)

    return [
        f"# ── HCCL rank table ({len(node_ips)} nodes, {device_count} devices/node) ──",
        f"cat > {output_path} << 'RANK_TABLE_EOF'",
        rank_table_json,
        "RANK_TABLE_EOF",
        f"chmod 640 {output_path}",
    ]


# =============================================================================
# 公开 API 函数
#
# 这些函数是 launcher 模块调用的入口点。
# =============================================================================

def build_start_command(params: Dict[str, Any]) -> str:
    """返回 MindIE 守护进程的核心启动命令（不含配置写入步骤）。

    警告：
        此命令单独使用不足以启动 MindIE！
        - MindIE 需要预先配置好的 config.json
        - 分布式模式需要环境变量和 rank table

        请使用 build_start_script() 获取完整启动脚本。

    Args:
        params: 参数字典（当前未使用，为接口一致性保留）

    Returns:
        str: 切换到工作目录并启动守护进程的命令
    """
    return f"cd {shlex.quote(MINDIE_WORK_DIR)} && exec ./bin/mindieservice_daemon"


def build_start_script(params: Dict[str, Any]) -> str:
    """返回完整的 bash 启动脚本内容（不含 shebang 行）。

    这是 sidecar launcher 调用的主入口函数。
    生成的脚本将写入共享卷，由 engine 容器执行。

    生成脚本的执行流程：
      1. 加载 Ascend CANN 工具包和 MindIE 环境脚本
      2. 导出分布式环境变量（仅当 nnodes > 1 时）
      3. 通过内联 Python 脚本合并更新 MindIE conf/config.json
         - 读取镜像原始配置
         - 应用覆盖参数
         - 写回配置（保留所有未修改字段如 LogConfig / ScheduleConfig.templateType）
      4. 启动 mindieservice_daemon

    多节点分布式模式说明：
      - rank-0 和所有 worker 节点都启动 mindieservice_daemon
      - rank-0 暴露 HTTP API（ipAddress=0.0.0.0）供外部访问
      - rank>0 节点监听 127.0.0.1（不暴露外部 HTTP）
      - MindIE 通过 HCCL 使用 MASTER_ADDR/RANK/WORLD_SIZE 进行节点间协调

    配置覆盖区块：
      1. ServerConfig   - HTTP 服务器配置（端口、超时、TLS 等）
      2. BackendConfig  - 后端配置（设备 ID、多节点开关等）
      3. ModelDeployConfig - 模型部署配置（序列长度、截断等）
      4. ModelConfig[0] - 模型配置（权重路径、worldSize 等）
      5. ScheduleConfig - 调度配置（批处理大小、缓存块等）

    Args:
        params: 参数字典，包含以下关键字段:
            - engine_config: MindIE 引擎配置（嵌套字典）
            - distributed:   是否分布式模式
            - nnodes:        总节点数
            - node_rank:     当前节点编号

    Returns:
        str: 完整的 bash 脚本内容（不含 #!/bin/bash）

    注意：
      - 脚本使用 'exec' 启动守护进程，替换 shell 进程
      - config.json 合并保留镜像原有配置，仅覆盖指定字段
      - MOE 模型会额外设置 tp/dp/moe_tp/moe_ep 参数
      - MTP 模型会设置 plugin_params 参数
    """
    engine_config = params.get("engine_config", {})
    is_distributed = params.get("distributed", False)
    nnodes = params.get("nnodes", 1)
    node_rank = params.get("node_rank", 0)

    # Propagate keys from engine_config to params top-level (they were set in
    # _merge_mindie_params but nested under engine_config in the final dict).
    for key in ("node_ips", "device_count"):
        if not params.get(key) and engine_config.get(key):
            params[key] = engine_config[key]

    # ── 1. npuDeviceIds ────────────────────────────────────────────────────
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

    # ── 2. ServerConfig overrides ──────────────────────────────────────────
    main_port: int = engine_config.get("port", DEFAULT_SERVER_PORT)
    # rank > 0 workers don't expose external HTTP
    ip_address = "0.0.0.0" if (not is_distributed or node_rank == 0) else "127.0.0.1"
    server_overrides: Dict[str, Any] = {
        "ipAddress": engine_config.get("ipAddress", ip_address),
        "port": main_port,
        "httpsEnabled": engine_config.get("httpsEnabled", False),
        "inferMode": engine_config.get("inferMode", "standard"),
        "openAiSupport": engine_config.get("openAiSupport", "vllm"),
        "tokenTimeout": engine_config.get("tokenTimeout", 600),
        "e2eTimeout": engine_config.get("e2eTimeout", 600),
        "allowAllZeroIpListening": engine_config.get("allowAllZeroIpListening", True),
    }
    if is_distributed and nnodes > 1:
        # Multi-node requires explicit inter-node comm TLS setting
        server_overrides["interCommTLSEnabled"] = engine_config.get("interCommTLSEnabled", False)

    # ── 3. BackendConfig overrides ─────────────────────────────────────────
    # NOTE: multiNodesInferEnabled must be false for individual MindIE daemon
    # instances.  MindIE's ConfigManager auto-updates worldSize when this is
    # true, setting it to total_ranks (from rank table), which causes
    # "Invalid DP number per node: 0" when local_devices < total_ranks.
    # Multi-node coordination is handled by ms_coordinator/ms_controller
    # at a higher level, not by individual daemon processes.
    backend_overrides: Dict[str, Any] = {
        "npuDeviceIds": npu_device_ids,
        "multiNodesInferEnabled": engine_config.get("multiNodesInferEnabled", False),
    }
    if is_distributed and nnodes > 1:
        # Disable inter-node TLS for test/dev environments (no certs provisioned)
        backend_overrides["interNodeTLSEnabled"] = engine_config.get("interNodeTLSEnabled", False)

    # ── 4. ModelDeployConfig overrides ────────────────────────────────────
    model_deploy_overrides: Dict[str, Any] = {
        "maxSeqLen": engine_config.get("maxSeqLen", 2560),
        "maxInputTokenLen": engine_config.get("maxInputTokenLen", 2048),
        "truncation": engine_config.get("truncation", False),
    }

    # ── 5. ModelConfig[0] overrides ────────────────────────────────────────
    world_size = engine_config.get("worldSize", 8 if is_distributed else 1)
    model_config_overrides: Dict[str, Any] = {
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

    # ── 6. ScheduleConfig overrides ────────────────────────────────────────
    schedule_overrides: Dict[str, Any] = {
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

    overrides_dict = {
        "server": server_overrides,
        "backend": backend_overrides,
        "model_deploy": model_deploy_overrides,
        "model_config": model_config_overrides,
        "schedule": schedule_overrides,
    }
    overrides_json = json.dumps(overrides_dict, indent=2, ensure_ascii=False)

    # ── 7. Assemble script parts ────────────────────────────────────────────
    env_cmds = _build_env_commands(params)
    dist_cmds = _build_distributed_env_commands(params)

    all_cmds = env_cmds + ([""] + dist_cmds if dist_cmds else [])
    env_block = "\n".join(all_cmds) + "\n" if all_cmds else ""

    dist_label = f"distributed rank={node_rank}/{nnodes}" if (is_distributed and nnodes > 1) else "single-node"
    logger.info("[mindie] Generating start_command.sh: %s, worldSize=%d", dist_label, world_size)

    # ── 8. Config merge-update + daemon start ──────────────────────────────
    # 通过环境变量传递路径给内联 Python，避免字符串注入风险
    safe_config_path = shlex.quote(MINDIE_CONFIG_PATH)
    safe_work_dir = shlex.quote(MINDIE_WORK_DIR)
    script = f"""{env_block}# ── Merge-update MindIE config.json (preserve original fields, override only what changed) ──
export _MINDIE_CONFIG_PATH={safe_config_path}

cat > /tmp/_mindie_overrides.json << 'OVERRIDES_EOF'
{overrides_json}
OVERRIDES_EOF

python3 << 'MERGE_SCRIPT_EOF'
import json, os, sys

CONFIG_PATH = os.environ['_MINDIE_CONFIG_PATH']
OVERRIDES_PATH = '/tmp/_mindie_overrides.json'

# 1. Load original config.json from image
try:
    with open(CONFIG_PATH, 'r') as f:
        config = json.load(f)
    print(f'[mindie] Loaded original config.json ({{len(json.dumps(config))}} chars)')
except Exception as e:
    print(f'[mindie] ERROR: Cannot read {{CONFIG_PATH}}: {{e}}', file=sys.stderr)
    sys.exit(1)

# 2. Load overrides
with open(OVERRIDES_PATH, 'r') as f:
    ov = json.load(f)

# 3. Merge (update only specified keys; keep all other original fields intact)
if 'ServerConfig' in config:
    config['ServerConfig'].update(ov['server'])

if 'BackendConfig' in config:
    bc = config['BackendConfig']
    bc.update(ov['backend'])

    if 'ModelDeployConfig' in bc:
        bc['ModelDeployConfig'].update(ov['model_deploy'])
        if 'ModelConfig' in bc['ModelDeployConfig'] and bc['ModelDeployConfig']['ModelConfig']:
            bc['ModelDeployConfig']['ModelConfig'][0].update(ov['model_config'])

    if 'ScheduleConfig' in bc:
        bc['ScheduleConfig'].update(ov['schedule'])

# 4. Write back
with open(CONFIG_PATH, 'w') as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
os.chmod(CONFIG_PATH, 0o640)

print('[mindie] config.json merge-updated successfully')
print(json.dumps(config, indent=2, ensure_ascii=False))
MERGE_SCRIPT_EOF

# ── Start MindIE daemon (background + wait, per official boot.sh) ────────────
cd {safe_work_dir}
./bin/mindieservice_daemon &
MINDIE_PID=$!
echo "[mindie] Daemon started as PID $MINDIE_PID"
wait $MINDIE_PID
exit_code=$?
if [ $exit_code -ne 0 ]; then
    echo "[mindie] ERROR: daemon exited with code $exit_code"
fi
exit $exit_code
"""
    return script


def start_engine(params: Dict[str, Any]):
    """直接启动引擎存根函数 - sidecar launcher 模式下已禁用。

    此函数为 sidecar 架构契约的一部分：
      - launcher 容器永远不直接启动引擎进程
      - 启动逻辑通过 build_start_script() 生成脚本
      - 脚本写入共享卷由 engine 容器执行

    调用此函数会抛出 RuntimeError，阻止意外的直接启动。

    Args:
        params: 参数字典（未使用）

    Raises:
        RuntimeError: 总是抛出，说明应使用 build_start_script()
    """
    raise RuntimeError(
        "start_engine is disabled in launcher mode. "
        "Use build_start_script() and write to shared volume instead."
    )
