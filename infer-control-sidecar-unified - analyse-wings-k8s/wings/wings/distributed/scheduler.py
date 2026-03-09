# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.

from typing import Dict, Optional
import logging
import random
import time
import requests

from wings.distributed.monitor import MonitorService


class SchedulerPolicy:
    ROUND_ROBIN = "round_robin"
    LEAST_LOAD = "least_load"
    RANDOM = "random"


class TaskScheduler:
    def __init__(self, monitor: MonitorService):
        self.monitor = monitor
        self.policy = SchedulerPolicy.LEAST_LOAD
        self.max_retries = 3
        self.retry_delay = 1  # 重试延迟(秒)
    
    @staticmethod
    def start():
        """启动调度器"""
        logging.info("Task scheduler started successfully")

    @staticmethod
    def stop():
        """停止调度器"""
        logging.info("Task scheduler stopped successfully")
    
    @staticmethod
    def _least_load(nodes: Dict) -> str:
        """最少负载策略"""
        return min(nodes.items(), key=lambda x: x[1]['workload'] if isinstance(x[1], dict) else x[1].workload)[0]
    
    @staticmethod
    def _round_robin(nodes: Dict) -> str:
        """轮询策略"""
        return list(nodes.keys())[0]
    
    def set_policy(self, policy: str):
        """设置调度策略"""
        self.policy = policy
        logging.info(f"Scheduling policy set to: {policy}")

    def schedule(self, url: str, data: Dict, retries: int = 0):
        """调度任务"""
        if retries >= self.max_retries:
            raise Exception("Maximum retry attempts reached")

        node_id = self._select_node()
        if not node_id:
            raise Exception("No available worker nodes")

        try:
            return self._forward_request(node_id, url, data)
        except Exception as e:
            logging.warning(f"Task scheduling failed (attempt {retries + 1}/{self.max_retries}): {str(e)}")
            time.sleep(self.retry_delay)
            return self.schedule(url, data, retries + 1)

    def select_worker(self) -> Optional[Dict]:
        """选择工作节点并返回完整节点信息"""
        node_id = self._select_node()
        if not node_id:
            return None
        return self.monitor.get_active_nodes().get(node_id)

    def _select_node(self) -> Optional[str]:
        """根据策略选择节点"""
        active_nodes = self.monitor.get_active_nodes()
        if not active_nodes:
            return None

        if self.policy == SchedulerPolicy.ROUND_ROBIN:
            return self._round_robin(active_nodes)
        elif self.policy == SchedulerPolicy.LEAST_LOAD:
            return self._least_load(active_nodes)
        else:  # random
            return random.choice(list(active_nodes.keys()))

    def _forward_request(self, node_id: str, url: str, data: Dict):
        """转发请求到工作节点"""
        node_info = self.monitor.get_active_nodes().get(node_id)
        if not node_info:
            raise Exception(f"Node {node_id} does not exist")
            
        node_url = f"http://{node_info.ip}:{node_info.port}{url}"
        try:
            response = requests.post(node_url, json=data)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to forward request to node {node_id}: {str(e)}")
            raise
