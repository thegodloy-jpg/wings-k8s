# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.

import time
import threading
from typing import Dict
import logging


class NodeStatus:
    def __init__(self, node_id: str, ip: str, port: int):
        self.node_id = node_id
        self.ip = ip
        self.port = port
        self.last_heartbeat = time.time()
        self.missed_heartbeats = 0
        self.status = "active"  # active, inactive, failed
        self.workload = 0.0


class MonitorService:
    def __init__(self):
        self.nodes: Dict[str, NodeStatus] = {}
        self.lock = threading.Lock()
        self.check_interval = 30  # 检查间隔(秒)
        self.max_missed_heartbeats = 60  # 最大允许丢失心跳次数改为60
        self._stop_event = threading.Event()
        self.thread = None

    def register_node(self, node_id: str, ip: str, port: int):
        """注册新节点"""
        with self.lock:
            if node_id not in self.nodes:
                self.nodes[node_id] = NodeStatus(node_id, ip, port)
                logging.info(f"New node registered: {node_id} ({ip}:{port})")

    def update_heartbeat(self, node_id: str, workload: float = 0.0):
        """更新节点心跳"""
        with self.lock:
            if node_id in self.nodes:
                self.nodes[node_id].last_heartbeat = time.time()
                self.nodes[node_id].workload = workload
                self.nodes[node_id].missed_heartbeats = 0
                self.nodes[node_id].status = "active"
            else:
                logging.warning(f"Unknown node heartbeat: {node_id}")

    def start(self):
        """启动监控服务"""
        self._stop_event.clear()
        self.thread = threading.Thread(
            target=self._check_nodes,
            daemon=True
        )
        self.thread.start()
        logging.info("Monitoring service started")

    def stop(self):
        """停止监控服务"""
        self._stop_event.set()
        self.thread.join()
        logging.info("Monitoring service stopped")

    def get_active_nodes(self) -> Dict[str, dict]:
        """获取所有活跃节点"""
        with self.lock:
            return {
                node_id: {
                    "node_id": status.node_id,
                    "ip": status.ip,
                    "port": status.port,
                    "status": status.status,
                    "workload": status.workload,
                    "last_heartbeat": status.last_heartbeat
                }
                for node_id, status in self.nodes.items()
                if status.status == "active"
            }
    
    def _check_nodes(self):
        """定期检查节点状态"""
        while not self._stop_event.is_set():
            current_time = time.time()
            with self.lock:
                for node_id, status in list(self.nodes.items()):
                    time_since_last_heartbeat = current_time - status.last_heartbeat
                    
                    if time_since_last_heartbeat > self.check_interval * 1.5:
                        status.missed_heartbeats += 1
                        logging.warning(
                        f"Node {node_id} missed heartbeat #{status.missed_heartbeats}"
                        )
                        
                        if status.missed_heartbeats >= self.max_missed_heartbeats:
                            del self.nodes[node_id]
                            logging.error(f"Node {node_id} removed due to \
                                          {self.max_missed_heartbeats} consecutive missed heartbeats")
            
            time.sleep(self.check_interval)