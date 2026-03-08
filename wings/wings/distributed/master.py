# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.

from typing import Dict, Optional, Any
import logging
import time
import concurrent.futures
from pydantic import BaseModel

import requests
from fastapi import FastAPI, HTTPException

from wings.distributed.monitor import MonitorService
from wings.distributed.scheduler import TaskScheduler
from wings.distributed.worker import HeartbeatRequest
from wings.utils.env_utils import get_master_port
from wings.utils.noise_filter import install_noise_filters

install_noise_filters()
# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


app = FastAPI(title="Wings Distributed Inference Master Node")


class NodeInfo(BaseModel):
    node_id: str
    ip: str
    port: int
    status: str = "active"
    last_heartbeat: float = time.time()
    workload: float = 0.0


class RegisterRequest(BaseModel):
    node_id: str
    ip: str
    port: int


class InferenceRequest(BaseModel):
    model_name: str
    input_data: str
    parameters: Optional[Dict] = None


class StartEngineRequest(BaseModel):
    engine: str
    params: Dict[str, Any]

# 全局服务实例
monitor_service = MonitorService()
task_scheduler = TaskScheduler(monitor_service)

monitor_service.start()
task_scheduler.start()


@app.post("/api/nodes/register")
async def register_node(request: RegisterRequest):
    """工作节点注册接口"""
    monitor_service.register_node(
        request.node_id,
        request.ip,
        request.port
    )
    monitor_service.update_heartbeat(
        request.node_id,
        0.0  # 初始负载为0
    )
    logging.info(f"Node registered successfully: {request.node_id} ({request.ip}:{request.port})")
    return {"status": "success"}


@app.get("/api/nodes")
async def get_nodes():
    """获取所有节点状态"""
    return {"nodes": list(monitor_service.get_active_nodes().values())}


@app.post("/api/start_engine")
async def start_engine(request: StartEngineRequest):
    """启动引擎服务"""
    try:
        params = request.model_dump()
        logger.info('Received engine start request')
        
        if params["params"]["distributed"]:
            return await _handle_distributed_mode(params)
        else:
            return await _handle_single_mode(params)
            
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


# ===== 分布式模式处理 =====
async def _handle_distributed_mode(params: dict):
    """处理分布式启动逻辑"""
    _validate_distributed_params(params)
    
    nodes = params["params"]["nodes"].split(",")
    active_nodes = monitor_service.get_active_nodes()
    nodes_to_ip = _map_active_nodes(active_nodes)
    
    return {
        "results": await _distribute_requests(
            nodes, 
            nodes_to_ip, 
            params
        )
    }


def _validate_distributed_params(params):
    """验证分布式模式必要参数"""
    if not params["params"]["nodes"]:
        raise HTTPException(
            status_code=400, 
            detail="Distributed mode requires nodes parameter"
        )


def _map_active_nodes(active_nodes: dict) -> dict:
    """构建节点IP到端口的映射表"""
    return {
        node['ip']: node['port'] 
        for node in active_nodes.values()
    }


async def _distribute_requests(nodes: list, node_map: dict, params: dict) -> dict:
    """向所有节点分发启动请求"""
    results = {}
    
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_to_node = {
            executor.submit(
                _send_single_request,
                node_ip,
                node_map.get(node_ip),
                params
            ): node_ip
            for node_ip in nodes
        }
        
        for future in concurrent.futures.as_completed(future_to_node):
            node_ip = future_to_node[future]
            results[node_ip] = _process_node_response(future, node_ip)
    
    return results


def _send_single_request(node_ip: str, port: int, params: dict):
    """向单个节点发送启动请求"""
    worker_url = f"http://{node_ip}:{port}"
    return requests.post(
        f"{worker_url}/api/start_engine",
        json={
            "engine": params["engine"],
            "params": params["params"],
        }
    )


def _process_node_response(future, node_ip: str) -> dict:
    """处理单个节点的响应结果"""
    try:
        response = future.result()
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f"Node {node_ip} startup failed: {str(e)}")
        return {"status": "error", "detail": str(e)}


# ===== 单机模式处理 =====
async def _handle_single_mode(params: dict):
    """处理单机模式启动逻辑"""
    worker_node = task_scheduler.select_worker()
    if not worker_node:
        raise HTTPException(
            status_code=503, 
            detail="No available worker nodes"
        )
        
    return _send_single_request(
        worker_node['ip'],
        worker_node['port'],
        params
    )


@app.post("/api/inference")
async def distribute_inference(request: InferenceRequest):
    """分发推理任务"""
    try:
        result = task_scheduler.schedule(
            "/api/inference",
            request.dict()
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.post("/api/heartbeat")
async def receive_heartbeat(request: HeartbeatRequest):
    """接收工作节点心跳"""
    monitor_service.update_heartbeat(
        request.node_id,
        request.workload
    )
    return {"status": "success"}


def start_master():
    """启动主节点服务"""
    import uvicorn
    import json
    from pathlib import Path
    
    # 从配置文件加载端口配置
    config_path = Path(__file__).parent.parent.parent / "wings" / "config" / "distributed_config.json"
    with open(config_path) as f:
        config = json.load(f)
    
    master_port = get_master_port()
    if master_port:
        port = master_port
    else:
        port = config["master"]["port"]
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
    )


if __name__ == "__main__":
    start_master()
