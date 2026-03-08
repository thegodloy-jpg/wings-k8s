# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: utils/env_utils.py
# Purpose: Environment helper functions used by adapter and launcher config resolution.
# Status: Active reused utility.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Centralize env parsing to avoid drift.
# - Keep defaults explicit and sidecar-safe.
# -----------------------------------------------------------------------------
# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
from csv import Error
import os
import socket
import logging

logger = logging.getLogger(__name__)


def validate_ip(ip_str):
    """IP

    Args:
        ip_str (str): IP

    Returns:
        bool: IPTrueFalse
    """
    if not ip_str:
        return False

    # IPv4
    try:
        socket.inet_aton(ip_str)
        return True
    except socket.error:
        return False


def get_master_ip():
    """master IP

    Returns:
        str: master IP
    """
    master_ip = os.getenv('MASTER_IP', None)
    return master_ip


def get_local_ip():
    """IP

    Returns:
        str: IP
    """
    host_ip = os.getenv('RANK_IP', None)
    if not host_ip:
        hostname = socket.gethostname()
        host_ip = socket.gethostbyname(hostname)
    return host_ip


def get_node_ips():
    node_ips = os.getenv('NODE_IPS')
    if node_ips and "[" in node_ips:
        node_ips = node_ips.replace("[", "").replace("]", "")
    return node_ips


def get_server_port():
    """SERVER_PORT

    Returns:
        int or None: None
    """
    port = os.getenv('SERVER_PORT')
    if port:
        return int(port)
    return None


def get_master_port():
    """MASTER_PORT

    Returns:
        int or None: None
    """
    port = os.getenv('MASTER_PORT')
    if port:
        return int(port)
    return None


def get_worker_port():
    """WORKER_PORT

    Returns:
        int or None: None
    """
    port = os.getenv('WORKER_PORT')
    if port:
        return int(port)
    return None


def get_vllm_distributed_port():
    """VLLM_DISTRIBUTED_PORT

    Returns:
        int or None: None
    """
    port = os.getenv('VLLM_DISTRIBUTED_PORT')
    if port:
        return int(port)
    return None


def get_sglang_distributed_port():
    """SGLANG_DISTRIBUTED_PORT

    Returns:
        int or None: None
    """
    port = os.getenv('SGLANG_DISTRIBUTED_PORT')
    if port:
        return int(port)
    return None


def get_lmcache_env():
    """LMCache

    Returns:
        str or bool: LMCACHE_OFFLOADFalse
    """
    lmcache_offload = os.getenv('LMCACHE_OFFLOAD', 'false')
    lmcache_offload = lmcache_offload.lower() == 'true'
    return lmcache_offload


def get_qat_env():
    """QAT

    Returns:
        bool: LMCACHE_QATFalse
    """
    qat = os.getenv('LMCACHE_QAT', 'false')
    qat = qat.lower() == 'true'
    return qat


def get_pd_role_env():
    """PD

    Returns:
        str: PD_ROLEP/D
    """
    pd_role = os.getenv('PD_ROLE', '')
    if pd_role and pd_role not in ("P", "D"):
        logger.warning(f"PD_ROLE id not P or D, PD is not enabled")
        pd_role = ''
    return pd_role


def get_router_env():
    """

    Returns:
        bool: WINGS_ROUTE_ENABLEFalse
    """
    router = os.getenv('WINGS_ROUTE_ENABLE', 'false')
    router = router.lower() == 'true'
    return router


def get_router_instance_group_name_env():
    """WINGS_ROUTE_INSTANCE_GROUP_NAME

    Returns:
        str: WINGS_ROUTE_INSTANCE_GROUP_NAME
    """
    env_name = "WINGS_ROUTE_INSTANCE_GROUP_NAME"
    router_instance_group_name = os.getenv(env_name, '')
    return router_instance_group_name


def get_router_instance_name_env():
    """WINGS_ROUTE_INSTANCE_NAME

    Returns:
        str: WINGS_ROUTE_INSTANCE_NAMEE
    """
    env_name = "WINGS_ROUTE_INSTANCE_NAME"
    router_instance_name = os.getenv(env_name, '')
    return router_instance_name


def get_router_nats_path_env():
    """WINGS_ROUTE_NATS_PATH

    Returns:
        str: WINGS_ROUTE_NATS_PATH
    """
    env_name = "WINGS_ROUTE_NATS_PATH"
    router_nats_path = os.getenv(env_name, '')
    return router_nats_path


def get_operator_acceleration_env():
    """operator_acceleration

    Returns:
        bool: operator_acceleration
    """
    operator_acceleration = os.getenv('ENABLE_OPERATOR_ACCELERATION', 'false')
    operator_acceleration = operator_acceleration.lower() == 'true'
    return operator_acceleration


def get_soft_fp8_env():
    """soft_fp8

    Returns:
        bool: soft_fp8
    """
    soft_fp8 = os.getenv('ENABLE_SOFT_FP8', 'false')
    soft_fp8 = soft_fp8.lower() == 'true'
    return soft_fp8


def get_config_force_env():
    """config_force

    Returns:
        bool: config_force
    """
    config_force = os.getenv('CONFIG_FORCE', 'false')
    config_force = config_force.lower() == 'true'
    return config_force


def log_kvcache_offload_config(lmcache_offload_enabled, qat_enabled):
    """KVCache Offload"""
    if not lmcache_offload_enabled:
        return

    logging.info(f"[KVCache Offload] KVCache Offload feature is enabled: {lmcache_offload_enabled}")
    logging.info(f"[KVCache Offload] Local memory is enabled: {os.getenv('LMCACHE_LOCAL_CPU', 'Not set')}")
    logging.info(f"[KVCache Offload] Local memory max size: {os.getenv('LMCACHE_MAX_LOCAL_CPU_SIZE', 'Not set')}")
    logging.info(f"[KVCache Offload] Local disk path: {os.getenv('LMCACHE_LOCAL_DISK', 'Not set')}")
    logging.info(f"[KVCache Offload] Local disk max size: {os.getenv('LMCACHE_MAX_LOCAL_DISK_SIZE', 'Not set')}")

    logging.info(f"[KVCache Offload] QAT Compression feature is enabled: {qat_enabled}")
    if not qat_enabled:
        return

    logging.info(f"[KVCache Offload] QAT Loss Level: {os.getenv('LMCACHE_QAT_LOSS_LEVEL', 'Not set')}")
    logging.info(f"[KVCache Offload] QAT Instance Number: {os.getenv('LMCACHE_QAT_INSTANCE_NUM', 'Not set')}")


def check_env():
    """

    Raises:
        ValueError: QATLMCache

    Returns:
        bool: truefalse
    """
    qat = get_qat_env()
    lmcache_offload = get_lmcache_env()

    log_kvcache_offload_config(lmcache_offload, qat)

    if qat:
        if not lmcache_offload:
            raise ValueError("QAT is enabled but LMCache offload is not configured")
        elif not os.getenv("LMCACHE_LOCAL_DISK") or not os.getenv("LMCACHE_MAX_LOCAL_DISK_SIZE"):
            raise ValueError("QAT is enabled but LMCACHE_LOCAL_DISK or LMCACHE_MAX_LOCAL_DISK_SIZE is not configured")

    router_instance_group_name = get_router_instance_group_name_env()
    if router_instance_group_name:
        if not get_router_instance_name_env():
            raise ValueError("Wings Router is enabled but instance name is not set")
        if not get_router_nats_path_env():
            raise ValueError("Wings Router enabled but nats path is not set")
    return True