# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: engines/mindie_adapter.py
# Purpose: MindIE (华为昇腾) engine adapter for sidecar launcher mode.
#          Supports single-node and multi-node distributed inference.
# Status: Active adapter.
# Responsibilities:
# - Build MindIE config.json merge-update script.
# - Build bash start_command.sh body (env setup + config write + daemon start).
# - For distributed mode: inject HCCL / MASTER_ADDR / RANK / WORLD_SIZE env vars.
# Sidecar Contracts:
# - build_start_script is the only supported launcher-facing entrypoint.
# - Do not reintroduce direct process launch in sidecar launcher path.
# -----------------------------------------------------------------------------
# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
MindIE engine adapter.

In sidecar launcher mode, this module:
  1. Assembles a diff of overrides that need to be applied to MindIE config.json;
  2. Generates a bash script body (start_command.sh) that:
       a. Sources Ascend CANN / MindIE env scripts
       b. Injects distributed env vars (HCCL / MASTER_ADDR) when nnodes > 1
       c. Merge-updates conf/config.json via an inline Python snippet
       d. Starts mindieservice_daemon
"""

import json
import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ──────────────────────────────────────────────────────────────────────────────
# MindIE service paths (override-able via env vars)
# ──────────────────────────────────────────────────────────────────────────────
MINDIE_WORK_DIR: str = os.getenv(
    "MINDIE_WORK_DIR",
    "/usr/local/Ascend/mindie/latest/mindie-service"
)
MINDIE_CONFIG_PATH: str = os.getenv(
    "MINDIE_CONFIG_PATH",
    os.path.join(MINDIE_WORK_DIR, "conf/config.json")
)

DEFAULT_SERVER_PORT = 18000
DEFAULT_MINDIE_MASTER_PORT = 27070


# ──────────────────────────────────────────────────────────────────────────────
# Internal: build env-setup command list
# ──────────────────────────────────────────────────────────────────────────────

def _build_env_commands(params: Dict[str, Any]) -> List[str]:
    """
    Build MindIE environment initialisation commands (CANN toolkit + MindIE set_env.sh).

    In the MindIE container these scripts may reference unbound vars (ZSH_VERSION, etc.)
    so they must be sourced inside set +u / set -u guards.
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
        cmds.append("set -u")

    npu_memory_fraction = (params.get("engine_config") or {}).get("npu_memory_fraction")
    if npu_memory_fraction is not None:
        cmds.append(f"export NPU_MEMORY_FRACTION={npu_memory_fraction}")

    return cmds


def _build_distributed_env_commands(params: Dict[str, Any]) -> List[str]:
    """
    Build distributed env variable setup commands for multi-node MindIE.

    Required env vars for Ascend multi-node HCCL communication:
      MASTER_ADDR  – IP of rank-0 node (head_node_addr)
      MASTER_PORT  – Port for collective initialisation (default 27070)
      RANK         – This node's rank
      WORLD_SIZE   – Total number of nodes
      HCCL_WHITELIST_DISABLE – Disable HCCL whitelist check (required in containers)
      HCCL_IF_IP   – Network interface IP for HCCL (auto-detected from hostname -i)
      RANK_TABLE_FILE – Path to HCCL rank table JSON (required by MindIE for multi-node)
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
        "export HCCL_SOCKET_IFNAME=eth0",
        "export GLOO_SOCKET_IFNAME=eth0",
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
    """
    Generate shell commands that write the HCCL rank table JSON file.

    For distributed MindIE (DP mode), this generates a SINGLE-NODE rank table
    containing only the current node's devices.  MindIE uses the rank table
    to determine n_nodes and validates worldSize % n_nodes == 0.  By keeping
    server_count=1, worldSize=device_count always passes validation.

    node_offset: index of this node within the global HCCL_DEVICE_IPS list,
                 used to look up the correct device IPs for this node.

    device_ip is read from HCCL_DEVICE_IPS env var (format: "ip0,ip1;ip2,ip3"
    where semicolons separate nodes and commas separate devices).
    Falls back to node host IP if HCCL_DEVICE_IPS is not set.
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


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def build_start_command(params: Dict[str, Any]) -> str:
    """
    Return the core MindIE daemon start command (without config write step).

    Note: This alone is insufficient for startup; use build_start_script() instead.
    """
    return f"cd {MINDIE_WORK_DIR} && exec ./bin/mindieservice_daemon"


def build_start_script(params: Dict[str, Any]) -> str:
    """
    Return the complete bash script body (start_command.sh content, without shebang).

    The generated script:
      1. Sources Ascend CANN toolkit and MindIE set_env.sh
      2. Exports distributed env vars (only when nnodes > 1)
      3. Merge-updates MindIE conf/config.json via inline Python
         (reads original config, applies overrides, writes back — preserving
          all untouched fields such as LogConfig / ScheduleConfig.templateType)
      4. Starts mindieservice_daemon

    For multi-node distributed mode:
      - rank-0 and all worker ranks all start mindieservice_daemon.
      - rank-0 exposes the HTTP API (ipAddress=0.0.0.0).
      - rank>0 nodes listen on 127.0.0.1 (no external HTTP needed).
      - MindIE handles inter-node coordination via HCCL through MASTER_ADDR/RANK/WORLD_SIZE.
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
        "maxSeqLen": engine_config.get("maxSeqLen", 4096),
        "maxInputTokenLen": engine_config.get("maxInputTokenLen", 2048),
        "truncation": engine_config.get("truncation", False),
    }

    # ── 5. ModelConfig[0] overrides ────────────────────────────────────────
    world_size = engine_config.get("worldSize", nnodes if is_distributed else 1)
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
    script = f"""{env_block}# ── Merge-update MindIE config.json (preserve original fields, override only what changed) ──
cat > /tmp/_mindie_overrides.json << 'OVERRIDES_EOF'
{overrides_json}
OVERRIDES_EOF

python3 << 'MERGE_SCRIPT_EOF'
import json, os, sys

CONFIG_PATH = '{MINDIE_CONFIG_PATH}'
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

# ── Start MindIE daemon ─────────────────────────────────────────────────────
cd '{MINDIE_WORK_DIR}'
exec ./bin/mindieservice_daemon
"""
    return script


def start_engine(params: Dict[str, Any]):
    """Legacy compatibility stub — direct process launch is disabled in launcher mode."""
    raise RuntimeError(
        "start_engine is disabled in launcher mode. "
        "Use build_start_script() and write to shared volume instead."
    )
