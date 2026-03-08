# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
Mindie (华为昇腾) 推理引擎适配器

负责启动 Mindie 推理服务。
支持分布式推理架构。
实现方式依赖于 Mindie 提供的命令行工具。
"""
from typing import Dict, Any, List
import json
import logging
import os
import subprocess

from wings.utils.env_utils import get_local_ip
from wings.utils.process_utils import log_process_pid, wait_for_process_startup, log_stream
from wings.utils.file_utils import safe_write_file

logger = logging.getLogger(__name__)

# 获取项目的绝对路径
root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 公共常量定义
CONFIG_SERVER = 'ServerConfig'
CONFIG_BACKEND = 'BackendConfig'
CONFIG_MODEL_DEPLOY = 'ModelDeployConfig'
CONFIG_SCHEDULE = 'ScheduleConfig'
DEFAULT_SERTER_PORT = 18000


def _setup_mindie_environment(params: Dict[str, Any]) -> List[str]:
    """设置Mindie环境变量"""
    if params.get('distributed', False):
        rank_table_path = os.getenv('RANK_TABLE_PATH')
        if not rank_table_path:
            raise ValueError("RANK_TABLE_PATH environment variable not set")
        os.chmod(rank_table_path, 0o640)
        mies_container_ip = get_local_ip()
        logger.info(f"Setting MIES container IP: {mies_container_ip}")
        env_commands = [
            f"export MIES_CONTAINER_IP={mies_container_ip}",
            f"export MASTER_IP={params.get('master_ip')}",
            f"export RANK_TABLE_FILE={rank_table_path}",
            f"source {root_dir}/wings/config/set_mindie_multi_env.sh"
        ]
    else:
        env_commands = [f"source {root_dir}/wings/config/set_mindie_single_env.sh"]
    
    # 设置NPU使用率
    npu_memory_fraction = params.get("engine_config").get("npu_memory_fraction")
    if npu_memory_fraction:
        env_commands += [f"export NPU_MEMORY_FRACTION={npu_memory_fraction}"]
    
    return env_commands


def _update_mindie_config(params: Dict[str, Any], work_dir: str) -> None:
    """更新Mindie服务配置文件"""
    config_path = f"{work_dir}/conf/config.json"
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        
        if params.get('distributed', False):
            _update_distributed_config(config, params)
        else:
            _update_single_config(config, params)
        
        safe_write_file(config_path, config, is_json=True)
        os.chmod(config_path, 0o640)
        logger.info(f"Successfully updated config file: {config_path}")
    except Exception as e:
        logger.error(f"Failed to update config file: {e}", exc_info=True)
        raise


def _update_server_config(config: Dict[str, Any], engine_config: Dict[str, Any], 
                         is_distributed: bool = False, master_ip: str = None) -> None:
    """更新ServerConfig配置"""
    server_config = {
        'port': engine_config.get('port', DEFAULT_SERTER_PORT),
        'httpsEnabled': engine_config.get('httpsEnabled', False),  # 官方默认值为True
        'inferMode': engine_config.get('inferMode', 'standard'),
        'openAiSupport': engine_config.get('openAiSupport', 'vllm'),
        'tokenTimeout': engine_config.get('tokenTimeout', 600),
        'e2eTimeout': engine_config.get('e2eTimeout', 600)
    }
    
    if is_distributed:
        server_config.update({
            'ipAddress': master_ip,
            'interCommTLSEnabled': engine_config.get('interCommTLSEnabled', False)
        })
    else:
        server_config.update({
            'ipAddress': engine_config.get('ipAddress', '127.0.0.1'),
            'allowAllZeroIpListening': engine_config.get('allowAllZeroIpListening', True)  # 官方默认值为False
        })
    
    if CONFIG_SERVER in config:
        config[CONFIG_SERVER].update(server_config)


def _update_model_deploy_config(model_deploy_config: Dict[str, Any], 
                               engine_config: Dict[str, Any], 
                               is_distributed: bool = False) -> None:
    """更新ModelDeployConfig配置"""
    model_deploy_config.update({
        "maxSeqLen": engine_config.get('maxSeqLen', 2560),
        "maxInputTokenLen": engine_config.get('maxInputTokenLen', 2048),
        "truncation": engine_config.get('truncation', False)
    })
    
    if 'ModelConfig' in model_deploy_config and model_deploy_config['ModelConfig']:
        model_config = model_deploy_config['ModelConfig'][0]
        update_data = {
            'modelName': engine_config.get('modelName', 'default_llm'),  # 官方默认值为llama-65b
            'modelWeightPath': engine_config.get('modelWeightPath'),
            'worldSize': engine_config.get('worldSize', 8 if is_distributed else 1),  # 官方默认值为4
            'cpuMemSize': engine_config.get('cpuMemSize', 5),
            'npuMemSize': engine_config.get('npuMemSize', -1),
            "trustRemoteCode": engine_config.get('trustRemoteCode', True)  # 官方默认值为False
        }

        if engine_config.get('isMOE', False):
            update_data.update({
                "tp": engine_config.get('tp', engine_config.get('worldSize', 1)),
                "dp": engine_config.get('dp', -1),
                "moe_tp": engine_config.get('moe_tp', engine_config.get('worldSize', 1)),
                "moe_ep": engine_config.get('moe_ep', -1)
            })
        if engine_config.get('isMTP', False):
            update_data.update({
                "plugin_params": {
                    "plugin_type": "mtp",
                    "num_speculative_tokens": 1
                }
            })
        model_config.update(update_data)


def _update_schedule_config(schedule_config: Dict[str, Any], 
                           engine_config: Dict[str, Any]) -> None:
    """更新ScheduleConfig配置（分布式和单机通用）"""
    schedule_config.update({
        'cacheBlockSize': engine_config.get('cacheBlockSize', 128),
        'maxPrefillBatchSize': engine_config.get('maxPrefillBatchSize', 50),
        'maxPrefillTokens': engine_config.get('maxPrefillTokens', 8192),
        'prefillTimeMsPerReq': engine_config.get('prefillTimeMsPerReq', 150),
        'prefillPolicyType': engine_config.get('prefillPolicyType', 0),
        'decodeTimeMsPerReq': engine_config.get('decodeTimeMsPerReq', 50),
        'decodePolicyType': engine_config.get('decodePolicyType', 0),
        'maxBatchSize': engine_config.get('maxBatchSize', 200),
        'maxIterTimes': engine_config.get('maxIterTimes', 512),
        'maxPreemptCount': engine_config.get('maxPreemptCount', 0),
        'supportSelectBatch': engine_config.get('supportSelectBatch', False),
        'maxQueueDelayMicroseconds': engine_config.get('maxQueueDelayMicroseconds', 5000),
        'bufferResponseEnabled': engine_config.get('bufferResponseEnabled', False),
        'decodeExpectedTime': engine_config.get('decodeExpectedTime', 50),
        'prefillExpectedTime': engine_config.get('prefillExpectedTime', 1500)
    })


def _update_distributed_config(config: Dict[str, Any], params: Dict[str, Any]) -> None:
    """更新分布式模式配置"""
    master_ip = params.get('master_ip')
    engine_config = params.get("engine_config")
    
    # 更新ServerConfig
    _update_server_config(config, engine_config, is_distributed=True, master_ip=master_ip)
    
    # 更新BackendConfig
    if CONFIG_BACKEND in config:
        backend_config = config[CONFIG_BACKEND]
        backend_config.update({
            'npuDeviceIds': engine_config.get('npuDeviceIds', [[0, 1, 2, 3, 4, 5, 6, 7]]),
            'multiNodesInferEnabled': True,
            'interNodeTLSEnabled': False
        })
        
        # 更新ModelDeployConfig
        if CONFIG_MODEL_DEPLOY in backend_config:
            _update_model_deploy_config(
                backend_config[CONFIG_MODEL_DEPLOY], 
                engine_config, 
                is_distributed=True
            )
        
        # 更新ScheduleConfig
        if CONFIG_SCHEDULE in backend_config:
            _update_schedule_config(backend_config[CONFIG_SCHEDULE], engine_config)


def _update_single_config(config: Dict[str, Any], params: Dict[str, Any]) -> None:
    """更新单机模式配置"""
    engine_config = params.get("engine_config")
    
    # 更新ServerConfig
    _update_server_config(config, engine_config, is_distributed=False)
    
    # 更新BackendConfig
    if CONFIG_BACKEND in config:
        backend_config = config[CONFIG_BACKEND]
        backend_config.update({
            'npuDeviceIds': engine_config.get('npuDeviceIds', [[0]])
        })
        
        # 更新ModelDeployConfig
        if CONFIG_MODEL_DEPLOY in backend_config:
            _update_model_deploy_config(
                backend_config[CONFIG_MODEL_DEPLOY], 
                engine_config, 
                is_distributed=False
            )
        
        # 更新ScheduleConfig
        if CONFIG_SCHEDULE in backend_config:
            _update_schedule_config(backend_config[CONFIG_SCHEDULE], engine_config)


def _start_mindie_process(env_commands: List[str], work_dir: str) -> subprocess.Popen:
    """启动Mindie服务进程"""
    cd_command = f"cd {work_dir}"
    start_command = "./bin/mindieservice_daemon"
    full_command = " && ".join(env_commands + [cd_command] + [start_command])

    logger.info("........ Starting mindie service ........")
    
    process = subprocess.Popen(
        ["/bin/bash", "-c", full_command],
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    log_process_pid(
        name="mindie",
        parent_pid=os.getpid(),
        child_pid=process.pid
    )
    return process


def _start_mindie_with_cmd(params: Dict[str, Any]):
    """
    使用 Mindie 命令行工具启动服务。
    协调各子函数完成环境设置、配置更新和服务启动。
    """
    try:
        env_commands = _setup_mindie_environment(params)
        
        work_dir = "/usr/local/Ascend/mindie/latest/mindie-service"
        _update_mindie_config(params, work_dir)
        
        process = _start_mindie_process(env_commands, work_dir)
        return process
        
    except Exception as e:
        logger.error(f"Error occurred while starting Mindie service: {e}", exc_info=True)
        raise


def start_engine(params: Dict[str, Any]):
    """
    启动 Mindie 推理服务的入口函数。

    会尝试优先使用 Python API (如果可用且已实现)，否则回退到命令行。

    Args:
        params (Dict[str, Any]): 合并后的参数字典。
    """
    logger.info("Mindie adapter: Preparing to start Mindie service...")

    # 如果 API 不可用或启动失败，尝试命令行
    try:
        logger.info("Attempting to start using Mindie command line tool...")
        process = _start_mindie_with_cmd(params)
        
        # 使用公共函数等待启动成功
        wait_for_process_startup(
            process=process,
            success_message="Daemon start success",
            _logger=logger
        )

        # 启动独立线程持续输出日志
        log_stream(process)

    except Exception as cmd_err:
        logger.error(f"Failed to start using Mindie command line: {cmd_err}", exc_info=True)
        raise ValueError("Failed to start Mindie service via API or command line") from cmd_err

    return True
