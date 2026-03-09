# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.

from typing import Dict, Optional, Any
import logging
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
import asyncio
import socket
import json
from pathlib import Path

from pydantic import BaseModel
from fastapi import FastAPI
import requests

from wings.core.engine_manager import start_engine_service
from wings.utils.env_utils import get_local_ip, get_master_ip, get_master_port, get_worker_port
from wings.utils.noise_filter import install_noise_filters

install_noise_filters()

app = FastAPI(title="Wings Distributed Inference Worker Node")
global config


class HeartbeatRequest(BaseModel):
    node_id: str
    workload: float


class InferenceRequest(BaseModel):
    model_name: str
    input_data: str
    parameters: Optional[Dict] = None


class EngineStartRequest(BaseModel):
    engine: str
    params: Dict[str, Any]


class WorkerConfig:
    def __init__(self, master_ip=None):
        # 配置日志
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        
        self.node_id = "worker_" + str(uuid.uuid4())
        self.master_ip = master_ip if master_ip else get_master_ip()
        self._load_config(self.master_ip)
        self.heartbeat_interval = 30  # 心跳间隔(秒)

    def _load_config(self, master_ip=None):
        """从配置文件加载配置"""
        config_path = Path(__file__).parent.parent.parent / "wings" / "config" / "distributed_config.json"
        with open(config_path) as f:
            _config = json.load(f)
        
        # 获取本机IP
        self.ip = get_local_ip()
        
        # 设置master URL
        master_port = get_master_port()
        if not master_port:
            master_port = _config["master"]["port"]
        self.master_url = f"http://{master_ip or _config['master']['host']}:{master_port}"
        
        # 设置worker端口(使用配置中第一个worker的端口)
        worker_port = get_worker_port()
        if not worker_port:
            worker_port = _config["workers"]["port"]
        self.port = worker_port


@app.post("/api/start_engine")
async def start_engine_service_api(request: EngineStartRequest):
    """启动推理引擎服务接口(异步版本)"""
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    
    try:
        # 在线程池中执行阻塞操作
        await loop.run_in_executor(
            executor,
            start_engine_service,
            {"engine": request.engine, **request.params}
        )
        return {"status": "started", "message": "Engine service started successfully"}
    except asyncio.TimeoutError:
        return {"status": "timeout", "message": "Engine service startup timeout"}
    except Exception as e:
        logging.error(f"引擎服务启动失败: {str(e)}")
        return {"status": "error", "message": f"Engine service failed to start: {str(e)}"}
    finally:
        executor.shutdown(wait=False)


def register_with_master():
    """向主节点注册"""
    register_url = f"{config.master_url}/api/nodes/register"
    data = {
        "node_id": config.node_id,
        "ip": config.ip,
        "port": config.port
    }
    try:
        response = requests.post(register_url, json=data)
        response.raise_for_status()
        logging.info(f"Successfully registered with master node: {config.master_url}")

    except Exception as e:
        logging.error(f"Registration failed: {str(e)}")


def send_heartbeat():
    """定时发送心跳"""
    while True:
        try:
            heartbeat_url = f"{config.master_url}/api/heartbeat"
            data = {
                "node_id": config.node_id,
                "workload": 0.0
            }
            requests.post(heartbeat_url, json=data)
        except Exception as e:
            logging.error(f"Heartbeat failed: {str(e)}")
        time.sleep(config.heartbeat_interval)


def is_port_available(port):
    """检查端口是否可用"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except socket.error:
            return False


def start_worker():
    """启动工作节点服务
    """
    # 先检查端口是否可用
    if not is_port_available(config.port):
        logging.error(f"Port {config.port} is already in use, service startup failed")
        return
    
    # 端口可用后再注册到主节点
    register_with_master()
    
    # 启动心跳线程
    heartbeat_thread = threading.Thread(
        target=send_heartbeat,
        daemon=True
    )
    heartbeat_thread.start()
    
    # 启动FastAPI服务
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=config.port)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--master-ip", help="Master node IP address", default=None)
    args = parser.parse_args()
    config = WorkerConfig(args.master_ip)
    start_worker()
