# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
VLLM 推理引擎适配器

负责使用 VLLM 的 Python API 来启动推理服务。
"""

import logging
import os
import time
from typing import Dict, Any, List
import subprocess

from wings.utils.env_utils import get_local_ip, get_lmcache_env, \
    get_pd_role_env, get_qat_env, get_master_ip
from wings.utils.model_utils import ModelIdentifier
from wings.utils.process_utils import log_process_pid, wait_for_process_startup, log_stream

logger = logging.getLogger(__name__)
root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _start_vllm_single(params: Dict[str, Any]) -> subprocess.Popen:
    """
    启动单机模式 vLLM 服务
    
    Args:
        params (Dict[str, Any]): 参数字典
        
    Returns:
        subprocess.Popen: 启动的进程对象
        
    Raises:
        Exception: 如果启动过程中发生错误
    """
    try:
        cmd = _build_vllm_command(params)

        process = subprocess.Popen(
            ["/bin/bash", "-c", cmd],
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        # 记录进程PID
        log_process_pid(
            name="vllm",
            parent_pid=os.getpid(),
            child_pid=process.pid
        )
        
        return process
        
    except Exception as e:
        logger.error(f"Error starting VLLM service: {e}", exc_info=True)
        raise


def _build_base_env_commands(params, engine: str, root: str) -> List[str]:
    """构建基础环境设置命令"""
    env_commands = []
    if engine == "vllm":
        env_commands.append(f"source {root}/wings/config/set_vllm_env.sh")
    elif engine == "vllm_ascend":
        env_commands.append(f"source {root}/wings/config/set_vllm_ascend_env.sh")
        if params.get("engine_config", {}).get("use_kunlun_atb"):
            env_commands.append(f"export USE_KUNLUN_ATB=1")
            logger.info("kunlun atb is used")
    return env_commands


def _build_cache_env_commands(engine: str) -> List[str]:
    """构建 KVCache Offload 相关环境变量命令
    
    根据不同的推理引擎类型，设置相应的库路径到 LD_LIBRARY_PATH 环境变量中，
    以支持 KVCache Offload 功能的正常运行。
    
    Args:
        engine (str): 推理引擎类型，支持 "vllm" 和 "vllm_ascend"
        
    Returns:
        List[str]: 包含环境变量设置命令的列表
    """
    env_commands = []
    if not get_lmcache_env():
        return env_commands
    
    if engine == "vllm":
        # 获取 kv_agent 模块的库路径
        lib_path = "/opt/vllm_env/lib/python3.10/site-packages/kv_agent/lib"
        env_commands.append(f'export LD_LIBRARY_PATH="{lib_path}:$LD_LIBRARY_PATH"')
        logger.info(f"[KVCache Offload] Added LD_LIBRARY_PATH for vllm: {lib_path}")
    elif engine == "vllm_ascend":
        # 获取 lmcache 模块的库路径
        lib_path = "/opt/ascend_env/lib/python3.11/site-packages/lmcache"
        env_commands.append(f'export LD_LIBRARY_PATH="{lib_path}:$LD_LIBRARY_PATH"')
        logger.info(f"[KVCache Offload] Added LD_LIBRARY_PATH for vllm_ascend: {lib_path}")
    
    return env_commands


def _build_qat_env_commands(engine) -> List[str]:
    """
    构建KVCache QAT 压缩环境变量命令
    
    Args:
        engine (str): 推理引擎类型
        
    Returns:
        List[str]: QAT相关的环境变量导出命令列表
    """
    env_commands = []
    if not get_qat_env():
        return env_commands

    if engine == "vllm":
        env_commands.append('export LMCACHE_QAT_ENABLED=True')
    else:
        env_commands.append('export LMCACHE_QAT_ENABLED=False')
        logger.warning(f"[KVCache Offload] QAT compression feature is not supported by the current engine {engine}, "
                       "it has been automatically disabled")
    return env_commands


def _build_pd_role_env_commands(engine: str, current_ip: str, network_interface: str) -> List[str]:
    """构建PD角色环境变量命令"""
    env_commands = []
    if get_pd_role_env():
        if engine == "vllm":
            env_commands.append(f'export VLLM_NIXL_SIDE_CHANNEL_HOST={current_ip}')
        elif engine == "vllm_ascend":
            rpc_port = os.getenv('VLLM_LLMDD_RPC_PORT', "5569")
            env_commands.extend([
                f"source /usr/local/Ascend/ascend-toolkit/set_env.sh",
                f"source /usr/local/Ascend/nnal/atb/set_env.sh",
                f"export HCCL_IF_IP={current_ip}",
                f"export GLOO_SOCKET_IFNAME={network_interface}",
                f"export TP_SOCKET_IFNAME={network_interface}",
                f"export HCCL_SOCKET_IFNAME={network_interface}",
                f"export OMP_PROC_BIND=false",
                f"export OMP_NUM_THREADS=100",
                f"export VLLM_USE_V1=1",
                f"export LCCL_DETERMINISTIC=1",
                f"export HCCL_DETERMINISTIC=true",
                f"export CLOSE_MATMUL_K_SHIFT=1",
                f"export VLLM_LLMDD_RPC_PORT={rpc_port}",
                "export PYTORCH_NPU_ALLOC_CONF=max_split_size_mb:256"
            ])
    return env_commands


def _build_distributed_env_commands(params: Dict[str, Any], current_ip: str, 
                                    network_interface: str, engine: str) -> List[str]:
    """构建分布式环境配置命令"""
    env_commands = []
    if params.get("distributed", False):
        backend = params.get("distributed_executor_backend")
        if backend == "ray":
            if engine == "vllm":
                env_commands.extend([
                    f"export VLLM_HOST_IP={current_ip}",
                    f"export GLOO_SOCKET_IFNAME={network_interface}",
                    f"export TP_SOCKET_IFNAME={network_interface}",
                    f"export NCCL_SOCKET_IFNAME={network_interface}"
                ])
            elif engine == "vllm_ascend":
                env_commands.extend([
                    f"export HCCL_IF_IP={current_ip}",
                    f"export GLOO_SOCKET_IFNAME={network_interface}",
                    f"export TP_SOCKET_IFNAME={network_interface}",
                    "export RAY_EXPERIMENTAL_NOSET_ASCEND_RT_VISIBLE_DEVICES=1",
                    "export ASCEND_PROCESS_LOG_PATH=/tmp/ray_vllm010"
                ])
        elif backend == "dp_deployment":
            if engine == "vllm":
                env_commands.extend([
                    f"export GLOO_SOCKET_IFNAME={network_interface}",
                    f"export TP_SOCKET_IFNAME={network_interface}",
                    f"export NCCL_SOCKET_IFNAME={network_interface}",
                    f'export VLLM_NIXL_SIDE_CHANNEL_PORT={params.get("nixl_port")}',
                    "export NCCL_IB_DISABLE=0",
                    "export NCCL_CUMEM_ENABLE=0",
                    "NCCL_NET_GDR_LEVEL=SYS",
                ])
            elif engine == "vllm_ascend":
                env_commands.extend([
                    f"export HCCL_IF_IP={current_ip}",
                    f"export GLOO_SOCKET_IFNAME={network_interface}",
                    f"export TP_SOCKET_IFNAME={network_interface}",
                    f"export HCCL_SOCKET_IFNAME={network_interface}",
                    "export OMP_PROC_BIND=false",
                    "export OMP_NUM_THREADS=100",
                    "export HCCL_BUFFSIZE=1024"
                ])
    return env_commands


def _build_env_commands(params: Dict[str, Any], current_ip: str, network_interface: str, root: str) -> List[str]:
    """构建环境变量设置命令列表"""
    engine = params.get("engine")
    env_commands = []
    
    # 依次调用各个模块的函数
    env_commands.extend(_build_base_env_commands(params, engine, root))
    env_commands.extend(_build_cache_env_commands(engine))
    env_commands.extend(_build_qat_env_commands(engine))
    env_commands.extend(_build_pd_role_env_commands(engine, current_ip, network_interface))
    env_commands.extend(_build_distributed_env_commands(params, current_ip, network_interface, engine))
    
    return env_commands


def _build_vllm_cmd_parts(params: Dict[str, Any]) -> str:
    """构建 vllm 命令部分"""
    engine_config = params.get("engine_config", {})
    # 删除自定义的非vllm参数
    if "use_kunlun_atb" in engine_config:
        engine_config.pop("use_kunlun_atb")
    cmd_parts = ["python", "-m", "vllm.entrypoints.openai.api_server"]    

    if params.get("distributed"):
        model_info = ModelIdentifier(params.get("model_name"),
                                    params.get("model_path"),
                                    params.get("model_type"))
        if params.get("distributed_executor_backend") == "dp_deployment":
            model_path = engine_config.pop("model")
            cmd_parts = ["vllm", "serve", model_path]
            nodes = params["nodes"].split(',')
            rpc_port = params['rpc_port']
            master_ip = get_master_ip()
            current_ip = get_local_ip()
            nnodes = len(nodes)
            node_rank = nodes.index(current_ip)

            if model_info.model_architecture == "DeepseekV3ForCausalLM" \
                and params.get("engine") == "vllm_ascend":
                data_parallel_size = "4"
                data_parallel_size_local = "2"
                data_parallel_start_rank = "2"
            else:
                data_parallel_size = str(nnodes)
                data_parallel_size_local = "1"
                data_parallel_start_rank = str(node_rank)
            cmd_parts.extend(["--data-parallel-address", f"{master_ip}"])
            cmd_parts.extend(["--data-parallel-rpc-port", f"{rpc_port}"])
            cmd_parts.extend(["--data-parallel-size", data_parallel_size])
            cmd_parts.extend(["--data-parallel-size-local", data_parallel_size_local])
            if node_rank != 0:
                cmd_parts.extend(["--headless "])
                cmd_parts.extend(["--data-parallel-start-rank", data_parallel_start_rank])
                engine_config.pop('host')
                engine_config.pop('port')
        elif params.get("distributed_executor_backend") == "ray":
            cmd_parts.extend(["--distributed-executor-backend", "ray"])
    
    for arg, value in engine_config.items():
        if value is None:
            continue
            
        arg_name = f"--{arg.replace('_', '-')}"
        if isinstance(value, bool):
            if value:
                cmd_parts.append(arg_name)
        elif isinstance(value, str) and value.strip().startswith('{') and value.strip().endswith('}'):
            cmd_parts.extend([arg_name, f"'{value}'"])
        else:
            cmd_parts.extend([arg_name, str(value)])
    
    return " ".join(cmd_parts)


def _build_vllm_command(params: Dict[str, Any]) -> str:
    """
    构建 vllm serve 命令行

    Args:
        params: 服务器参数

    Returns:
        str: 完整的 vllm serve 命令
    """
    current_ip = get_local_ip()
    network_interface = detect_network_interface(current_ip)
    
    # 构建环境变量命令
    env_commands = _build_env_commands(
        params, current_ip, network_interface, root_dir
    )
    
    # 构建主命令
    command_str = _build_vllm_cmd_parts(params)
    
    # 组合完整命令
    if env_commands:
        return " && ".join(env_commands) + " && " + command_str
    return command_str


def detect_network_interface(ip: str) -> str:
    """自动检测与指定IP匹配的物理网络接口"""
    import netifaces
    for iface in netifaces.interfaces():
        addrs = netifaces.ifaddresses(iface)
        if netifaces.AF_INET not in addrs:
            continue
        for addr_info in addrs[netifaces.AF_INET]:
            if addr_info.get('addr') == ip:
                return iface
    raise ValueError(f"Cannot find network interface for IP {ip}")


def wait_until_ray_head_ready(ray_head_ip: str, ray_head_port: int, max_retries=30, interval=5):
    import ray
    """等待 Ray Head 节点启动并能够被连接"""
    for _ in range(max_retries):
        try:
            logger.info("--- Waiting for Ray Head to be ready ---")
            ray.init(address=f"{ray_head_ip}:{ray_head_port}")
            nodes = ray.nodes()
            if len(nodes) >= 1:
                logger.info("Ray Head node started successfully")
                ray.shutdown()
                return True
            ray.shutdown()
        except Exception as e:
            logger.warning(f"Error waiting for Ray Head readiness: {e}")
        time.sleep(interval)
    raise RuntimeError("Ray Head node failed to become ready within timeout")


def wait_until_all_workers_joined(ray_head_ip: str, ray_head_port: int, expected_nodes,
                                  max_retries=60, interval=5):
    import ray
    """等待所有 Worker 节点加入集群"""
    for _ in range(max_retries):
        try:
            ray.init(address=f"{ray_head_ip}:{ray_head_port}")
            nodes = ray.nodes()
            if len(nodes) == expected_nodes:
                ray.shutdown()
                logger.info("All Worker nodes joined cluster successfully")
                return True
            ray.shutdown()
        except Exception as e:
            logger.warning(f"Error waiting for Workers to join: {e}")
        time.sleep(interval)
    raise RuntimeError(f"Failed to wait for all Worker nodes to join cluster within {max_retries * interval} seconds")


def check_node_joined(ray_head_ip: str, ray_head_port: int, node_ip, max_retries=10, interval=5):
    import ray
    """检查节点是否加入集群"""
    for _ in range(max_retries):
        try:
            ray.init(address=f"{ray_head_ip}:{ray_head_port}")
            nodes = ray.nodes()
            for node in nodes:
                if node.get("NodeManagerAddress") == node_ip and node.get("alive", False):
                    logger.info(f"Worker node({node_ip}) joined cluster successfully")
                    ray.shutdown()
                    return True
            ray.shutdown()
        except Exception as e:
            logger.warning(f"Error waiting for Worker node({node_ip}) to join: {e}")
        time.sleep(interval)
    raise RuntimeError(f"Failed to wait for Worker node({node_ip}) to \
                       join cluster within {max_retries * interval} seconds")


def _start_ray_node(params: Dict, is_head: bool) -> subprocess.Popen:
    # 提取公共参数
    current_ip = params["current_ip"]
    network_interface = detect_network_interface(current_ip)
    
    # 统一节点启动日志
    node_type = "head" if is_head else "worker"
    logger.info(f" ------ Starting Ray {node_type} node: {current_ip} ------")
    
    # 非头节点统一等待
    if not is_head:
        time.sleep(20)

    # 获取环境变量命令
    env_commands = _build_env_commands(
        params, 
        current_ip, 
        network_interface,
        root_dir
    )
    
    # 获取Ray启动命令
    ray_cmd = _build_ray_command(
        params,
        is_head
    )
    
    # 组合完整命令
    full_command = " && ".join(env_commands + [ray_cmd])
    
    # 启动进程
    return subprocess.Popen(
        ["/bin/bash", "-c", full_command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )


def _build_ray_command(
    params: Dict,
    is_head: bool
) -> str:
    """构建Ray启动命令"""
    engine = params["engine"]
    current_ip = params["current_ip"]
    head_ip = params["ray_head_ip"]
    head_port = params["ray_head_port"]
    tensor_parallel_size = params.get("engine_config").get("tensor_parallel_size")

    if is_head:
        if engine == "vllm":
            return f"ray start --block --head --port={head_port}"
        else:  # vllm_ascend
            return (
                f"ray start --head --num-gpus {tensor_parallel_size} "
                f"--node-ip-address {current_ip} --port {head_port}"
            )
    else:
        if engine == "vllm":
            return f"ray start --block --address={head_ip}:{head_port}"
        else:  # vllm_ascend
            return (
                f"ray start --address={head_ip}:{head_port} "
                f"--num-gpus={tensor_parallel_size} "
                f"--node-ip-address {current_ip}"
            )


def _start_vllm_api_server(params: Dict) -> subprocess.Popen:
    full_command = _build_vllm_command(params)
    logger.info("........ Starting vLLM service ........")

    # 使用subprocess运行命令
    process = subprocess.Popen(
        ["/bin/bash", "-c", full_command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    # 使用公共函数等待启动成功
    wait_for_process_startup(
        process=process,
        success_message="Application startup complete",
        _logger=logger
    )
    
    # 启动独立线程持续输出日志
    log_stream(process)

    return process


def start_vllm_distributed(params: Dict):
    """vLLM分布式模式启动入口"""
    logger.info("Starting VLLM distributed mode...")
    
    distributed_executor_backend = params.get('distributed_executor_backend')
    if distributed_executor_backend == 'ray':
        _start_vllm_with_ray(params)
    elif distributed_executor_backend == 'dp_deployment':
        _start_vllm_with_dp_deployment(params)
    else:
        raise ValueError("Distributed executor backend must be ray or dp_deployment!")


def _start_vllm_with_ray(params: Dict):
    """使用Ray后端启动vLLM分布式模式"""
    # 解析节点列表
    nodes = params["nodes"].split(',')
    current_ip = get_local_ip()
    
    # 优先使用 --ray_head_ip 指定的 head
    ray_head_ip = params.get("ray_head_ip")
    ray_head_port = params.get("ray_head_port")
    total_nodes = len(nodes)

    # 参数更新
    params.update({
        "current_ip": current_ip,
        "ray_head_ip": ray_head_ip,
        "ray_head_port": ray_head_port
    })

    # 增强节点角色判断
    is_head = current_ip == ray_head_ip

    # 记录Ray进程PID
    log_process_pid(
        name="ray_head" if is_head else "ray_worker",
        parent_pid=os.getpid(),
        child_pid=None  # Ray进程PID将在_start_ray_node中记录
    )

    if is_head:
        _start_ray_head_node(params, total_nodes)
    else:
        _start_ray_worker_node(params, ray_head_ip, ray_head_port, current_ip)


def _start_ray_head_node(params: Dict, total_nodes: int):
    """启动Ray头节点并初始化vLLM服务"""
    logger.info(" --- Starting Ray Head ---")
    processes = []
    get_ray_status = ['ray', 'status']
    
    # 启动Ray头节点
    processes.append(("Ray head", _start_ray_node(params, True)))
    
    ray_head_ip = params.get("ray_head_ip")
    ray_head_port = params.get("ray_head_port")

    # 动态等待 Head 节点就绪
    try:
        wait_until_ray_head_ready(ray_head_ip, ray_head_port)
        subprocess.run(get_ray_status, shell=False)
    except Exception as e:
        logger.error(f"Failed waiting for Ray Head readiness: {str(e)}")
        raise

    # 动态等待所有 Worker 节点加入集群
    try:
        wait_until_all_workers_joined(ray_head_ip, ray_head_port, total_nodes)
        subprocess.run(get_ray_status, shell=False)
    except Exception as e:
        logger.error(f"Failed waiting for Worker nodes to join cluster: {str(e)}")
        raise

    logger.info("Starting VLLM API service...")

    # 启动vLLM并记录PID
    vllm_process = _start_vllm_api_server(params)
    log_process_pid(
        name="vllm_distributed",
        parent_pid=os.getpid(),
        child_pid=vllm_process.pid
    )
    processes.append(("vLLM distributed", vllm_process))


def _start_ray_worker_node(params: Dict, ray_head_ip: str, ray_head_port: str, current_ip: str):
    """启动Ray工作节点"""
    processes = []
    get_ray_status = ['ray', 'status']
    
    # 等待ray集群初始化
    time.sleep(10)
    logger.info("Starting Ray Worker node...")

    # 启动 Ray Worker 节点
    processes.append(("Ray Worker", _start_ray_node(params, False)))
    time.sleep(20)
    subprocess.run(get_ray_status, shell=False)
    
    # 检查节点是否加入ray集群
    try:
        check_node_joined(ray_head_ip, ray_head_port, current_ip)
    except Exception as e:
        logger.error(f"Failed checking node cluster join status: {str(e)}")
        raise


def _start_vllm_with_dp_deployment(params: Dict):
    """使用dp_deployment后端启动vLLM分布式模式"""
    full_command = _build_vllm_command(params)
    logger.info("........ Starting vLLM service ........")

    # 使用subprocess运行命令
    process = subprocess.Popen(
        ["/bin/bash", "-c", full_command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    log_process_pid(
        name="vllm_distributed",
        parent_pid=os.getpid(),
        child_pid=process.pid
    )

    # 使用公共函数等待启动成功
    wait_for_process_startup(
        process=process,
        success_message="Application startup complete",
        _logger=logger
    )

    # 启动独立线程持续输出日志
    log_stream(process)


def start_engine(params: Dict[str, Any]):
    """启动入口统一分发"""
    logger.info("VLLM adapter: Preparing to start VLLM service...")
    logger.info('-- Initial parameters logged --')

    try:
        if params.get("distributed", False):
            start_vllm_distributed(params)
        else:
            # 1. 构建单机 vllm serve 命令
            process = _start_vllm_single(params)

            # 使用公共函数等待启动成功
            wait_for_process_startup(
                process=process,
                success_message="Application startup complete",
                _logger=logger
            )

            # 启动独立线程持续输出日志
            log_stream(process)
    except Exception as e:
        logger.error(f"Critical error starting VLLM service: {e}", exc_info=True)
        raise
    return True
