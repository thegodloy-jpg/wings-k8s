# =============================================================================
# 文件: utils/env_utils.py
# 用途: 环境变量解析辅助函数，供 adapter 和 launcher 配置解析使用
# 状态: 活跃，复用自 wings 项目的环境工具模块
#
# 功能概述:
#   本模块集中管理环境变量的解析，避免各模块重复实现。
#   主要分类:
#   - IP 地址获取  : get_master_ip(), get_local_ip(), get_node_ips()
#   - 端口获取     : get_server_port(), get_master_port(), get_vllm_distributed_port() 等
#   - 特性开关     : get_lmcache_env(), get_pd_role_env(), get_router_env() 等
#   - 配置路径     : get_router_nats_path_env() 等
#
# Sidecar 架构契约:
#   - 集中环境变量解析，避免漂移
#   - 默认值明确且对 sidecar 场景安全
#
# =============================================================================
# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
import os
import socket
import logging

logger = logging.getLogger(__name__)


def validate_ip(ip_str):
    """校验 IP 地址格式是否合法（仅支持 IPv4）。

    Args:
        ip_str (str): IP 地址字符串

    Returns:
        bool: 合法的 IPv4 地址返回 True，否则返回 False
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
    """获取分布式集群的 master IP 地址。

    从 MASTER_IP 环境变量读取，用于 Ray/HCCL 等分布式通信初始化。

    Returns:
        str | None: master IP 地址，未设置时返回 None
    """
    master_ip = os.getenv('MASTER_IP', None)
    return master_ip


def get_local_ip():
    """获取本节点的 IP 地址。

    优先从 RANK_IP 环境变量读取，若未设置则调用 socket 获取主机名对应的 IP。

    Returns:
        str: 本机 IP 地址
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
        try:
            return int(port)
        except ValueError:
            logger.warning("Invalid SERVER_PORT value %r, ignoring", port)
    return None


def get_master_port():
    """MASTER_PORT

    Returns:
        int or None: None
    """
    port = os.getenv('MASTER_PORT')
    if port:
        try:
            return int(port)
        except ValueError:
            logger.warning("Invalid MASTER_PORT value %r, ignoring", port)
    return None


def get_worker_port():
    """WORKER_PORT

    Returns:
        int or None: None
    """
    port = os.getenv('WORKER_PORT')
    if port:
        try:
            return int(port)
        except ValueError:
            logger.warning("Invalid WORKER_PORT value %r, ignoring", port)
    return None


def get_vllm_distributed_port():
    """VLLM_DISTRIBUTED_PORT

    Returns:
        int or None: None
    """
    port = os.getenv('VLLM_DISTRIBUTED_PORT')
    if port:
        try:
            return int(port)
        except ValueError:
            logger.warning("Invalid VLLM_DISTRIBUTED_PORT value %r, ignoring", port)
    return None


def get_sglang_distributed_port():
    """SGLANG_DISTRIBUTED_PORT

    Returns:
        int or None: None
    """
    port = os.getenv('SGLANG_DISTRIBUTED_PORT')
    if port:
        try:
            return int(port)
        except ValueError:
            logger.warning("Invalid SGLANG_DISTRIBUTED_PORT value %r, ignoring", port)
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