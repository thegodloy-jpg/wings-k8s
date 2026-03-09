# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
from csv import Error
import os
import socket
import logging

logger = logging.getLogger(__name__)


def validate_ip(ip_str):
    """验证IP地址格式是否合法
    
    Args:
        ip_str (str): 待验证的IP地址字符串
        
    Returns:
        bool: 如果IP地址格式合法返回True，否则返回False
    """
    if not ip_str:
        return False
        
    # 简单验证IPv4格式
    try:
        socket.inet_aton(ip_str)
        return True
    except socket.error:
        return False


def get_master_ip():
    """获取master IP地址
    
    Returns:
        str: master IP地址
    """
    master_ip = os.getenv('MASTER_IP', None) 
    return master_ip


def get_local_ip():
    """获取本机IP地址
    
    Returns:
        str: 本机IP地址
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
    """从环境变量获取SERVER_PORT端口号
    
    Returns:
        int or None: 如果环境变量存在且有效则返回端口号，否则返回None
    """
    port = os.getenv('SERVER_PORT')
    if port:
        return int(port)
    return None


def get_master_port():
    """从环境变量获取MASTER_PORT端口号
    
    Returns:
        int or None: 如果环境变量存在且有效则返回端口号，否则返回None
    """
    port = os.getenv('MASTER_PORT')
    if port:
        return int(port)
    return None


def get_worker_port():
    """从环境变量获取WORKER_PORT端口号
    
    Returns:
        int or None: 如果环境变量存在且有效则返回端口号，否则返回None
    """
    port = os.getenv('WORKER_PORT')
    if port:
        return int(port)
    return None


def get_vllm_distributed_port():
    """从环境变量获取VLLM_DISTRIBUTED_PORT端口号
    
    Returns:
        int or None: 如果环境变量存在且有效则返回端口号，否则返回None
    """
    port = os.getenv('VLLM_DISTRIBUTED_PORT')
    if port:
        return int(port)
    return None


def get_sglang_distributed_port():
    """从环境变量获取SGLANG_DISTRIBUTED_PORT端口号
    
    Returns:
        int or None: 如果环境变量存在且有效则返回端口号，否则返回None
    """
    port = os.getenv('SGLANG_DISTRIBUTED_PORT')
    if port:
        return int(port)
    return None


def get_lmcache_env():
    """获取LMCache卸载配置环境变量
    
    Returns:
        str or bool: 返回LMCACHE_OFFLOAD环境变量的值，如果未设置则返回False
    """
    lmcache_offload = os.getenv('LMCACHE_OFFLOAD', 'false')
    lmcache_offload = lmcache_offload.lower() == 'true'
    return lmcache_offload


def get_qat_env():
    """获取QAT配置环境变量
    
    Returns:
        bool: 返回LMCACHE_QAT环境变量的值，如果未设置则返回False
    """
    qat = os.getenv('LMCACHE_QAT', 'false')
    qat = qat.lower() == 'true'
    return qat


def get_pd_role_env():
    """获取PD角色环境变量
    
    Returns:
        str: 返回PD_ROLE环境变量的值，如果未设置或不是P/D则返回空字符串
    """
    pd_role = os.getenv('PD_ROLE', '')
    if pd_role and pd_role not in ("P", "D"):
        logger.warning(f"PD_ROLE id not P or D, PD is not enabled")
        pd_role = ''
    return pd_role


def get_router_env():
    """获取高性能路由是否开启环境变量
    
    Returns:
        bool: 返回WINGS_ROUTE_ENABLE环境变量的值，如果未设置则返回False
    """
    router = os.getenv('WINGS_ROUTE_ENABLE', 'false')
    router = router.lower() == 'true'
    return router


def get_router_instance_group_name_env():
    """获取WINGS_ROUTE_INSTANCE_GROUP_NAME环境变量
    
    Returns:
        str: 返回WINGS_ROUTE_INSTANCE_GROUP_NAME环境变量的值，如果未设置返回空字符串
    """
    env_name = "WINGS_ROUTE_INSTANCE_GROUP_NAME"
    router_instance_group_name = os.getenv(env_name, '')
    return router_instance_group_name


def get_router_instance_name_env():
    """获取WINGS_ROUTE_INSTANCE_NAME环境变量
    
    Returns:
        str: 返回WINGS_ROUTE_INSTANCE_NAMEE环境变量的值，如果未设置则返回空字符串
    """
    env_name = "WINGS_ROUTE_INSTANCE_NAME"
    router_instance_name = os.getenv(env_name, '')
    return router_instance_name


def get_router_nats_path_env():
    """获取WINGS_ROUTE_NATS_PATH环境变量
    
    Returns:
        str: 返回WINGS_ROUTE_NATS_PATH环境变量的值，如果未设置空字符串
    """
    env_name = "WINGS_ROUTE_NATS_PATH"
    router_nats_path = os.getenv(env_name, '')
    return router_nats_path


def get_operator_acceleration_env():
    """获取operator_acceleration环境变量
    
    Returns:
        bool: 返回operator_acceleration环境变量的值
    """
    operator_acceleration = os.getenv('ENABLE_OPERATOR_ACCELERATION', 'false')
    operator_acceleration = operator_acceleration.lower() == 'true'
    return operator_acceleration


def get_soft_fp8_env():
    """获取soft_fp8环境变量
    
    Returns:
        bool: 返回soft_fp8环境变量的值
    """
    soft_fp8 = os.getenv('ENABLE_SOFT_FP8', 'false')
    soft_fp8 = soft_fp8.lower() == 'true'
    return soft_fp8


def get_config_force_env():
    """获取config_force环境变量
    
    Returns:
        bool: 返回config_force环境变量的值
    """
    config_force = os.getenv('CONFIG_FORCE', 'false')
    config_force = config_force.lower() == 'true'
    return config_force


def log_kvcache_offload_config(lmcache_offload_enabled, qat_enabled):
    """记录KVCache Offload配置信息"""
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
    """检查环境变量是否冲突
    
    Raises:
        ValueError: 当QAT存在但LMCache卸载为空时抛出异常
    
    Returns:
        bool: 如果冲突，则返回true，否则返回false
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