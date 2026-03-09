# =============================================================================
# File: distributed/worker.py
# Purpose: 分布式工作节点（Worker）服务
# Origin:  移植自 wings/distributed/worker.py，适配 sidecar 脚本生成模式
#
# 功能概述:
#   每个 Worker 节点的职责:
#   1. 启动时向 Master 注册自身 IP 和端口
#   2. 启动后台线程定期发送心跳
#   3. 暴露 /api/start_engine 接口，收到请求后:
#      - 调用 engine adapter 的 build_start_script() 生成 bash 脚本
#      - 将脚本写入共享卷 /shared-volume/start_command.sh
#      - engine 容器读取并执行脚本
#
# 与 wings 版本的核心差异:
#   - wings: Worker 直接调用 start_engine_service() 通过 subprocess 启动引擎
#   - sidecar: Worker 只生成脚本写入共享卷，由 engine 容器执行
#   - 新增 /api/node_info 接口，供 Master 查询本节点分布式信息
#
# Sidecar 架构契约:
#   - Worker 不直接启动引擎进程
#   - 脚本写入路径由 settings.SHARED_VOLUME_PATH 控制
# =============================================================================
# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.

from __future__ import annotations

import dataclasses as _dc
import json
import logging
import os
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from pydantic import BaseModel
import requests

from app.config.settings import settings
from app.utils.env_utils import get_local_ip, get_master_ip, get_master_port, get_worker_port
from app.utils.file_utils import safe_write_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Wings Distributed Inference Worker Node")


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Worker configuration
# ---------------------------------------------------------------------------

class WorkerConfig:
    """Worker 节点配置。

    从环境变量和 distributed_config.json 加载 Master 地址和本机端口。

    Attributes:
        node_id:            唯一标识 (worker_<uuid>)
        master_ip:          Master 节点 IP
        master_url:         Master API 完整 URL
        ip:                 本机 IP
        port:               Worker API 端口
        heartbeat_interval: 心跳间隔（秒）
    """

    def __init__(self, master_ip: str | None = None):
        self.node_id = "worker_" + str(uuid.uuid4())
        self.master_ip = master_ip if master_ip else get_master_ip()
        self._load_config(self.master_ip)
        self.heartbeat_interval = 30

    def _load_config(self, master_ip: str | None = None):
        config_path = (
            Path(__file__).parent.parent / "config" / "distributed_config.json"
        )
        with open(config_path) as f:
            _config = json.load(f)

        self.ip = get_local_ip()

        master_port = get_master_port()
        if not master_port:
            master_port = _config["master"]["port"]
        self.master_url = (
            f"http://{master_ip or _config['master']['host']}:{master_port}"
        )

        worker_port = get_worker_port()
        if not worker_port:
            worker_port = _config["workers"]["port"]
        self.port = worker_port


# Global config — initialised by start_worker() / __main__
config: WorkerConfig | None = None


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.post("/api/start_engine")
async def start_engine_api(request: EngineStartRequest):
    """收到 Master 的引擎启动指令后，走完整 launcher 管线并写入共享卷。

    流程（与 standalone 模式的 build_launcher_plan 完全一致）:
      1. 从 request.params 重建 LaunchArgs（包含 Master 注入的分布式参数）
      2. 推导 PortPlan
      3. 调用 build_launcher_plan() 走硬件探测 → 配置合并 → adapter 脚本生成
      4. 将完整 bash 脚本写入 /shared-volume/start_command.sh
      5. engine 容器轮询该文件并执行

    与直接调用 start_engine_service() 的区别:
      - build_launcher_plan() 会执行 detect_hardware()、load_and_merge_configs()
      - 对不同硬件（GPU/NPU）的 Worker 节点能正确适配
      - host/port 注入逻辑按 node_rank 自动处理（rank0 绑定端口，其余不绑定）
    """
    try:
        from app.core.start_args_compat import LaunchArgs
        from app.core.port_plan import derive_port_plan
        from app.core.wings_entry import build_launcher_plan

        # ---- 1. 从 params 重建 LaunchArgs ----
        la_fields = {f.name for f in _dc.fields(LaunchArgs)}
        la_kwargs = {k: v for k, v in request.params.items() if k in la_fields}
        la_kwargs["engine"] = request.engine  # 确保 engine 来自顶层字段
        launch_args = LaunchArgs(**la_kwargs)

        # ---- 2. 推导 PortPlan ----
        port_plan = derive_port_plan(
            port=launch_args.port,
            enable_reason_proxy=settings.ENABLE_REASON_PROXY,
            health_port=settings.HEALTH_PORT,
        )

        # ---- 3. build_launcher_plan（硬件探测 + 配置合并 + 脚本生成） ----
        launcher_plan = build_launcher_plan(launch_args, port_plan)

        # ---- 4. 写入共享卷 ----
        shared_dir = settings.SHARED_VOLUME_PATH
        os.makedirs(shared_dir, exist_ok=True)
        script_path = os.path.join(
            shared_dir, settings.START_COMMAND_FILENAME
        )
        ok = safe_write_file(script_path, launcher_plan.command, is_json=False)
        if not ok:
            return {
                "status": "error",
                "message": f"Failed to write script to {script_path}",
            }

        logger.info("Engine start script written to %s", script_path)
        return {
            "status": "started",
            "message": "Engine start script written to shared volume",
        }

    except Exception as e:
        logger.error("Engine script generation failed: %s", e, exc_info=True)
        return {
            "status": "error",
            "message": f"Engine script generation failed: {e}",
        }


@app.get("/api/node_info")
async def node_info():
    """返回本节点的基本信息，供 Master 查询。"""
    return {
        "node_id": config.node_id if config else "unknown",
        "ip": config.ip if config else "unknown",
        "port": config.port if config else 0,
    }


# ---------------------------------------------------------------------------
# Master registration & heartbeat
# ---------------------------------------------------------------------------

def register_with_master():
    """向 Master 注册本节点。"""
    register_url = f"{config.master_url}/api/nodes/register"
    data = {
        "node_id": config.node_id,
        "ip": config.ip,
        "port": config.port,
    }
    try:
        response = requests.post(register_url, json=data)
        response.raise_for_status()
        logger.info(
            "Successfully registered with master node: %s", config.master_url
        )
    except Exception as e:
        logger.error("Registration failed: %s", e)


def send_heartbeat():
    """后台线程：定期向 Master 发送心跳。"""
    while True:
        try:
            heartbeat_url = f"{config.master_url}/api/heartbeat"
            data = {"node_id": config.node_id, "workload": 0.0}
            requests.post(heartbeat_url, json=data)
        except Exception as e:
            logger.error("Heartbeat failed: %s", e)
        time.sleep(config.heartbeat_interval)


# ---------------------------------------------------------------------------
# Worker lifecycle
# ---------------------------------------------------------------------------

def is_port_available(port: int) -> bool:
    """检查端口是否可用。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except socket.error:
            return False


def start_worker(worker_config: WorkerConfig | None = None):
    """启动 Worker 节点服务。

    1. 检查端口可用
    2. 向 Master 注册
    3. 启动心跳守护线程
    4. 启动 FastAPI 服务
    """
    global config
    if worker_config:
        config = worker_config
    elif config is None:
        config = WorkerConfig()

    if not is_port_available(config.port):
        logger.error(
            "Port %d is already in use, service startup failed", config.port
        )
        return

    register_with_master()

    heartbeat_thread = threading.Thread(target=send_heartbeat, daemon=True)
    heartbeat_thread.start()

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=config.port)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--master-ip", help="Master node IP address", default=None
    )
    args = parser.parse_args()
    config = WorkerConfig(args.master_ip)
    start_worker()
