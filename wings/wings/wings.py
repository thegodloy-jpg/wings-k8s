# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights.
# -*- coding: utf-8 -*-

"""
Wings 大模型推理服务启动器

该脚本负责：
1. 解析命令行参数。
2. 检测硬件环境。
3. 加载并合并配置文件参数与命令行参数。
4. 根据指定的引擎类型，启动相应的推理服务。
"""
import argparse
import sys
import logging
import json
import os
import time
from typing import List, Tuple, Dict, Any
import requests

from wings.core.engine_manager import start_engine_service
from wings.core.hardware_detect import detect_hardware
from wings.core.config_loader import load_and_merge_configs
from wings.utils.env_utils import get_local_ip, get_server_port, check_env, validate_ip, \
    get_master_ip, get_master_port, get_master_ip, get_node_ips
from wings.utils.noise_filter import install_noise_filters

install_noise_filters()

# 配置日志记录器 (可选，但推荐)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)




def get_master_url():
    """从distributed_config.json获取master配置"""
    config_path = os.path.join(os.path.dirname(__file__), "config/distributed_config.json")
    try:
        with open(config_path) as f:
            config = json.load(f)
            master_ip = get_master_ip()
            master_ip = master_ip if master_ip else config["master"]["host"]
            master_port = get_master_port()
            master_port = master_port if master_port else config["master"]["port"]
            return f"http://{master_ip}:{master_port}"
    except Exception as e:
        logger.warning(f"Failed to read distributed_config.json: {e}")
        return "http://0.0.0.0:16000 "


def check_all_nodes_registered(master_url: str, expected_nodes: List[str]) -> bool:
    """
    检查所有节点是否已注册到master
    
    Args:
        master_url: master节点URL
        expected_nodes: 期望注册的节点IP列表
        
    Returns:
        bool: 是否所有节点都已注册
    """
    timeout = 10
    try:
        response = requests.get(f"{master_url}/api/nodes", timeout=timeout)
        response.raise_for_status()
        registered_nodes = [node["ip"] for node in response.json()["nodes"]]
        return all(node_ip in registered_nodes for node_ip in expected_nodes)
    except Exception as e:
        logger.warning(f"Failed to check node registration status: {str(e)}")
        return False


def start_engine_service_with_api(params: Dict[str, Any]):
    """
    通过Master API启动推理引擎服务
    
    Args:
        params (Dict[str, Any]): 包含所有合并后参数的字典，
                                  必须包含 'engine' 键来指定引擎类型。
    """
    # 分布式模式下检查所有节点是否已注册
    if params.get("distributed") and params.get("nodes"):
        master_url = get_master_url()
        expected_nodes = params["nodes"].split(",")
        max_retries = 10
        retry_interval = 5
        
        for attempt in range(max_retries):
            if check_all_nodes_registered(master_url, expected_nodes):
                break
                
            if attempt == max_retries - 1:
                raise RuntimeError(
                    f"Failed to start engine: within {max_retries*retry_interval} seconds"
                    f"Not all nodes ({expected_nodes}) registered with master"
                )
                
            logger.info(
                f"Waiting for all nodes to register ({attempt+1}/{max_retries}), "
                f"will retry in {retry_interval} seconds..."
            )
            time.sleep(retry_interval)
    
    api_url = f"{master_url}/api/start_engine"
    logger.info(f"Calling Master API: {api_url}")
    
    try:
        response = requests.post(
            api_url,
            json={
                "engine": params["engine"],
                "params": params
            }
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to call Master API: {str(e)}")
        raise RuntimeError(f"Failed to start engine service via Master: {str(e)}") from e


def validate_distributed_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """验证分布式模式参数"""
    # 检查环境变量
    rank_ip = os.getenv('RANK_IP')
    if not rank_ip or not validate_ip(rank_ip):
        parser.error("Environment variable RANK_IP not set or invalid format")
        
    # 检查节点IP列表
    node_ips = get_node_ips()
    if not node_ips:
        parser.error("Environment variable NODE_IPS not set")
    
    try:
        node_list = [ip.strip() for ip in node_ips.split(',')]
        if len(node_list) < 2:
            parser.error("Distributed mode requires at least 2 nodes")
        for ip in node_list:
            if not validate_ip(ip):
                parser.error(f"Invalid node IP format: {ip}")
    except Exception as e:
        parser.error(f"Failed to parse NODE_IPS: {str(e)}")
    
    args.nodes = node_ips
    
    # 检查Master IP
    master_ip = get_master_ip()
    if not master_ip or not validate_ip(master_ip):
        parser.error("Environment variable MASTER_IP not set or invalid format")
    args.master_ip = master_ip


def _add_core_arguments(parser: argparse.ArgumentParser) -> None:
    """添加核心服务启动参数"""
    parser.add_argument("--engine", type=str, default=None,
                      choices=["sglang", "vllm", "mindie", "wings", "transformers", "xllm"],
                      help="Specify inference engine backend (sglang/vllm/mindie/wings)")
    parser.add_argument("--distributed", action="store_true",
                      help="Enable distributed mode")
    parser.add_argument("--config-file", type=str, default=None,
                      help="(Optional) Specify custom JSON config or JSON config file path")
    parser.add_argument("--gpu-usage-mode", type=str, default='full',
                      help="gpu useage mode")
    parser.add_argument("--device-count", type=int, default=1,
                      help="device count")
    parser.add_argument("--model-type", type=str, default='auto',
                        choices=['auto', 'llm', 'embedding', 'rerank', 'mmum', 'mmgm'],
                      help="The model type must be one of 'auto', 'llm', 'embedding', 'rerank', 'mmum', 'mmgm'." \
                      " If it is 'auto', the model type will be automatically identified.")
    parser.add_argument("--enable-speculative-decode", action="store_true", default=None,
                      help="Enable speculative decoding feature")
    parser.add_argument("--speculative-decode-model-path", type=str, default=None,
                      help="Path to auxiliary model for speculative decoding")
    parser.add_argument("--enable-rag-acc", action="store_true", default=None,
                      help="Enable RAG acceleration feature")


def _add_engine_common_arguments(parser: argparse.ArgumentParser) -> None:
    """添加推理引擎公共参数"""
    engine_args = parser.add_argument_group("Engine Common Arguments", "Parameters shared by inference engines")
    parser.add_argument("--host", type=str, default=get_local_ip(),
                      help="Inference service IP address, defaults to env_utils.get_local_ip()")
    parser.add_argument("--port", type=int, default=get_server_port() or 18000,
                      help="Inference service port, defaults to SERVER_PORT env var or 18000 if not set")
    parser.add_argument("--model-name", type=str, default=None,
                      help="Specify model name to use. If provided, will use model-specific configuration.")
    parser.add_argument("--model-path", type=str, default=None,
                      help="Specify model path to load. If provided, overrides default path in config.")
    # 顶层保存目录（对 mmgm/wings 生效，会传入适配器作为 --save-path）
    parser.add_argument("--save-path", type=str, default="/opt/wings/outputs",
                      help="Top-level save directory for generated outputs (effective for mmgm/wings)")

    engine_args.add_argument("--input-length", type=int, default=None,
                    help="Model max input length")
    engine_args.add_argument("--output-length", type=int, default=None,
                    help="Model max output length")
    engine_args.add_argument("--trust-remote-code", action="store_true", default=None,
                        help="Trust remote code execution")
    engine_args.add_argument("--dtype", type=str, default=None,
                        help="Specify data type for model parameters")
    engine_args.add_argument("--kv-cache-dtype", type=str, default=None,
                        help="Specify data type for key-value cache")
    engine_args.add_argument("--quantization", type=str, default=None,
                        help="Specify quantization method")
    engine_args.add_argument("--quantization-param-path", type=str, default=None,
                        help="Specify path to quantization parameters")
    engine_args.add_argument("--gpu-memory-utilization", type=float, default=None,
                        help="Specify GPU memory utilization percentage")
    engine_args.add_argument("--enable-chunked-prefill", action="store_true", default=None,
                        help="Enable chunked prefill")
    engine_args.add_argument("--block-size", type=int, default=None,
                        help="Specify block size for processing")
    engine_args.add_argument("--max-num-seqs", type=int, default=None,
                        help="Specify maximum number of sequences")
    engine_args.add_argument("--seed", type=int, default=None,
                        help="Specify random seed for reproducibility")
    engine_args.add_argument("--enable-expert-parallel", action="store_true", default=None,
                        help="Enable expert parallelism and model parallelism")
    engine_args.add_argument("--max-num-batched-tokens", type=int, default=None,
                        help="max batch tokens for prefill")
    engine_args.add_argument("--enable-prefix-caching", action="store_true", default=None,
                        help="Enable prefix caching")
    engine_args.add_argument("--enable-auto-tool-choice", action="store_true", default=None,
                        help="Enable function call/tool use")


def parse_arguments() -> Tuple[argparse.Namespace, List[str]]:
    """
    解析命令行参数。

    Returns:
        Tuple[argparse.Namespace, List[str]]: 已知参数的命名空间和未知的额外参数列表。
    """
    parser = argparse.ArgumentParser(
        description="Wings Large Model Inference Service Launcher",
        formatter_class=argparse.RawTextHelpFormatter
    )

    # 分模块添加参数
    _add_core_arguments(parser)
    _add_engine_common_arguments(parser)
    
    # 解析参数
    args, unknown_args = parser.parse_known_args()

    # 参数验证（仅在分布式时需要）
    if args.distributed:
        validate_distributed_args(args, parser)

    logger.info(f"Parsed core arguments: {args}")
    if unknown_args:
        logger.info(f"Detected extra unknown arguments passed to engine: {unknown_args}")

    return args, unknown_args


def main():
    """
    主执行函数
    """
    known_args, unknown_engine_args = parse_arguments()
    logger.info(f"known_args: {known_args}")
    logger.info(f"unknown_engine_args: {unknown_engine_args}")
    # 检查环境变量
    if check_env():
        logger.info(f"ENV check pass")
    try:
        # --- 1. 硬件检测 ---
        hardware_env = detect_hardware()
        logger.info(f"Detected hardware environment: {hardware_env}")

        # --- 2. 配置加载与合并 ---
        final_params = load_and_merge_configs(hardware_env, known_args)

        # --- 3. 启动服务 ---
        if final_params.get("distributed"):
            # Distributed mode - start all worker nodes
            response = start_engine_service_with_api(final_params)
            logger.info(f"Engine '{final_params.get('engine')}' startup process completed.")
        else:
            # 单机模式 - 启动单个worker节点
            response = start_engine_service(final_params)

        # --- 4. 检查服务是否成功启动 ---
        if final_params.get("distributed"):
            # 分布式模式 - 检查所有worker节点的返回结果
            failed_nodes = []
            for node_ip, node_result in response.get("results", {}).items():
                if node_result.get("status") != "started":
                    logger.error(f"Node {node_ip} failed to start: {node_result.get('detail', 'Unknown error')}")
                    failed_nodes.append(node_ip)
            
            if failed_nodes:
                logger.error(f"Service startup failed: {len(failed_nodes)} nodes failed to start")
                sys.exit(1)
            else:
                logger.info("All node services started successfully")
        else:
            # 单机模式 - 检查单个worker节点的返回结果
            if response:
                logger.info("Service started successfully")
            else:
                logger.error(f"Service startup failed: {response.get('message', 'Unknown error')}")
                sys.exit(1)

    except Exception as e:
        logger.error(f"Error occurred during startup: {e}", exc_info=True)  # exc_info=True logs stack trace
        sys.exit(1)

if __name__ == "__main__":
    main()