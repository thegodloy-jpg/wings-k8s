# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
通用性能测试基类

提供通用的性能测试框架，支持不同类型的模型测试
"""

import time
import asyncio
import logging
import threading
import json
from pathlib import Path
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Union, List, Dict
from contextlib import asynccontextmanager

import pandas as pd
import aiohttp

# Configure logger
logger = logging.getLogger(__name__)


@dataclass
class SampleRequest:
    """表示单个性能测试请求"""
    input_data: Union[str, Any]  # 通用输入数据，可以是文本、向量等
    input_token_length: int  # 输入数据大小（token数、向量维度等）
    output_token_length: int  # 期望输出大小


class BasePerformanceTester(ABC):
    """通用性能测试基类"""
    
    def __init__(self, args):
        self.args = args
        self.requests = []
        self.results = {}
        self.report_result = {
            "Test Timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        
        # 基础性能统计
        self.stats = {
            'total_request_size': 0,
            'total_time': 0,
            'success_count': 0,
            'error_count': 0
        }
    
    @abstractmethod
    def prepare_requests(self) -> List[SampleRequest]:
        """准备测试请求"""
        pass
    
    @abstractmethod
    def create_request_parameters(self, request: SampleRequest) -> Dict:
        """创建特定模型的请求参数"""
        pass
    
    @abstractmethod
    async def send_request_async(self, parameters: Dict, headers: Dict) -> Dict:
        """异步发送请求并返回结果"""
        pass
    
    @abstractmethod
    def calculate_metrics(self):
        """计算性能指标 - 由子类实现具体的指标计算逻辑"""
        pass
    
    @abstractmethod
    def get_model_specific_info(self) -> Dict:
        """获取模型特定信息，如模型大小、精度等"""
        pass

    @abstractmethod
    def print_results(self):
        """打印测试结果"""
        pass
    
    async def run_test_async(self, print_result=True, save_file=True):
        """异步执行性能测试"""
        pass
    
    def get_basic_stats(self) -> Dict:
        """获取基础统计信息"""
        return {
            "Total Requests": len(self.results),
            "Successful Requests": self.stats['success_count'],
            "Failed Requests": self.stats['error_count'],
            "Success Rate": round(self.stats['success_count'] / len(self.results) * 100, 2) if self.results else 0
        }

    async def _execute_requests_async(self, parameters_list: List[Dict]):
        """异步执行请求并收集结果"""
        pass
    
    async def _delayed_request(self, params_data: Dict, headers: Dict, delay: float):
        """延迟发送请求"""
        await asyncio.sleep(delay)
        return await self.send_request_async(
            params_data,
            headers
        )
    
    def _save_results(self):
        """保存测试结果"""
        # 保存汇总结果到 CSV
        df = pd.DataFrame([self.report_result])
        if Path(self.args.csv_file).exists():
            existing_df = pd.read_csv(self.args.csv_file)
            df = pd.concat([existing_df, df], ignore_index=True)
        
        df.to_csv(self.args.csv_file, index=False)        
        # 保存详细结果到 JSON
        json_file = f"./{self.args.model_name}_detailed_results.json"
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(self.results, f, indent=4, ensure_ascii=False)
    