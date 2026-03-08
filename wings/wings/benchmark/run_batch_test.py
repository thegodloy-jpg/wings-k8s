# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
批量性能测试脚本

支持运行多个测试场景并生成对比报告
"""

import argparse
import asyncio
import json
import time
import logging
from typing import Dict, Any

import pandas as pd

from performance_llm import performance_test_async as llm_performance_test_async
from performance_mmum import performance_test_async as mmum_performance_test_async

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 全局变量：控制分隔符"="的个数
SEPARATOR_LENGTH = 40


class BatchPerformanceTester:
    """批量性能测试器"""
    
    def __init__(self, config_file: str):
        self.config = self.load_config(config_file)
        self.results = []
        self.summary = {}
        
    @staticmethod
    def load_config(config_file: str) -> Dict[str, Any]:
        """加载配置文件"""
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError as e:
            raise FileNotFoundError(f"Configuration file {config_file} does not exist") from e
        except json.JSONDecodeError as e:
            raise Exception(f"Configuration file format error: {e}") from e
    
    async def run_single_test(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        """运行单个测试场景"""
        logger.info(f"Starting test: {scenario['name']}")
        
        # 创建参数对象
        args = argparse.Namespace()
        
        # 基础参数
        args.model_name = self.config['model_name']
        args.model_type = self.config['model_type']
        args.model_path = self.config['model_path']
        args.ip = self.config['service']['ip']
        args.port = self.config['service']['port']
        args.protocol = self.config['service'].get('protocol', 'http')
        args.dataset_path = self.config['files']['dataset_path']
        args.csv_file = self.config['files']['csv_file']
        
        # 测试参数
        args.thread_num = scenario.get('thread_num', self.config['test_parameters']['thread_num'])
        args.input_tokens_num = scenario.get('input_tokens_num', self.config['test_parameters']['input_tokens_num'])
        args.output_tokens_num = scenario.get('output_tokens_num', self.config['test_parameters']['output_tokens_num'])
        args.uniform_interval = scenario.get('uniform_interval', self.config['test_parameters']['uniform_interval'])
        args.warmup_num = 0
        
        # 多模态参数
        if args.model_type == 'mmum':
            args.image_height = scenario.get('image_height', 512)
            args.image_width = scenario.get('image_width', 512)
            args.image_count = scenario.get('image_count', 10)
            args.image_root = self.config.get('files', {}).get('image_root', './images')
        
        # 运行测试
        start_time = time.time()
        
        try:
            # 根据模型类型选择测试函数
            if args.model_type == 'mmum':
                result = await mmum_performance_test_async(args, print_result=True, save_file=True)
            elif args.model_type == 'llm':
                result = await llm_performance_test_async(args, print_result=True, save_file=True)
            else:
                raise ValueError(f"model type must be llm or mmum, current is {args.model_type}")
            
            # 记录测试信息
            result['Scenario'] = scenario['name']
            result['Test Duration'] = round(time.time() - start_time, 2)
            
            logger.info(f"Test completed: {scenario['name']} - Duration: {result['Test Duration']}s")
            
            return result
            
        except Exception as e:
            logger.error(f"Test failed: {scenario['name']} - Error: {e}")
            return {
                'Scenario': scenario['name'],
                'Error': str(e)
            }
    
    async def run_warmup(self):
        """运行预热"""
        warmup_num = self.config['test_parameters']['warmup_num']
        if warmup_num <= 0:
            return
        
        logger.info(f"Starting warmup, total {warmup_num} requests...")
        
        args = argparse.Namespace(
            model_name=self.config['model_name'],
            model_type=self.config['model_type'],
            model_path=self.config['model_path'],
            ip=self.config['service']['ip'],
            port=self.config['service']['port'],
            protocol=self.config['service'].get('protocol', 'http'),
            dataset_path=self.config['files']['dataset_path'],
            csv_file='warmup.csv',
            thread_num=min(warmup_num, 10),
            input_tokens_num=128,
            output_tokens_num=128,
            uniform_interval=0
        )
        
        try:
            # 根据模型类型选择测试函数
            if args.model_type == 'mmum':
                args.image_height = self.config['test_parameters']['image_height']
                args.image_width = self.config['test_parameters']['image_width']
                args.image_count = self.config['test_parameters']['image_count']
                args.image_root = self.config.get('files', {}).get('image_root', './images')
                await mmum_performance_test_async(args, print_result=True, save_file=False)
            elif args.model_type == 'llm':
                await llm_performance_test_async(args, print_result=True, save_file=False)
            else:
                raise ValueError(f"model type must be llm or mmum, current is {args.model_type}")
                
            logger.info("Warmup completed\n")
        except Exception as e:
            logger.error(f"Warmup failed: {e}\n")
    
    async def run_all_tests(self):
        """运行所有测试场景"""
        logger.info("=" * SEPARATOR_LENGTH)
        logger.info("Starting batch performance test")
        logger.info("=" * SEPARATOR_LENGTH)
        
        # 预热
        await self.run_warmup()
        
        # 获取测试场景
        scenarios = self.config.get('test_scenarios', [])
        if not scenarios:
            # 如果没有配置场景，使用默认场景
            scenarios = [{
                'name': 'Default Test',
                'thread_num': self.config['test_parameters']['thread_num'],
                'input_tokens_num': self.config['test_parameters']['input_tokens_num'],
                'output_tokens_num': self.config['test_parameters']['output_tokens_num']
            }]
        
        # 按顺序运行所有测试
        results = []
        for scenario in scenarios:
            result = await self.run_single_test(scenario)
            results.append(result)
        self.results = results
        
        # 生成报告
        self.generate_report()
    
    def generate_report(self):
        """生成测试报告"""
        logger.info("=" * SEPARATOR_LENGTH)
        logger.info("Generating test report")
        logger.info("=" * SEPARATOR_LENGTH)
        
        # 创建汇总数据
        summary_data = []
        for result in self.results:
            if 'Error' not in result:
                summary_data.append({
                    'Scenario': result['Scenario'],
                    'Threads': result.get('Process Num', 'N/A'),
                    'Input Tokens': result.get('Input Length', 'N/A'),
                    'Output Tokens': result.get('Output Length', 'N/A'),
                    'TTFT (ms)': result.get('Mean TTFT(ms)', 'N/A'),
                    'Mean TPS (with first token)': result.get('Mean TPS with first token', 'N/A'),
                    'Mean TPS (without first token)': result.get('Mean TPS without first token', 'N/A'),
                    'Total Time(s)': result.get('Total Time(s)', 'N/A'),
                    'Success Rate (%)': result.get('Success Rate', 'N/A'),
                    'Test Duration (s)': result.get('Test Duration', 'N/A')
                })
        
        if summary_data:
            # 创建 DataFrame
            df = pd.DataFrame(summary_data)
            
            # 打印汇总表
            logger.info("Test Results Summary:")
            logger.info(f"\n{df.to_string(index=False)}")


async def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="Batch performance testing tool\n\n"
                   "Example configs:\n"
                   "  - llm: ./wings/benchmark/llm_batch_perf_test_config.json\n"
                   "  - mmum: ./wings/benchmark/mmum_batch_perf_test_config.json",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--config', type=str, default="./wings/benchmark/llm_batch_perf_test_config.json", 
                    help='Configuration file path')
    parser.add_argument('--scenario', type=str, help='Run only the specified test scenario')
    parser.add_argument('--dry-run', action='store_true', help='Display test plan only, do not actually run')

    args = parser.parse_args()
    
    # 创建测试器
    tester = BatchPerformanceTester(args.config)
    
    if args.dry_run:
        # 只显示测试计划
        logger.info("Test Plan:")
        for scenario in tester.config.get('test_scenarios', []):
            if args.scenario and scenario['name'] != args.scenario:
                continue
            logger.info(f"- {scenario['name']}")
            logger.info(f"  Threads: {scenario.get('thread_num')}")
            logger.info(f"  Input Tokens: {scenario.get('input_tokens_num')}")
            logger.info(f"  Output Tokens: {scenario.get('output_tokens_num')}")
        return
    
    # 如果指定了特定场景
    if args.scenario:
        scenarios = tester.config.get('test_scenarios', [])
        tester.config['test_scenarios'] = [s for s in scenarios if s['name'] == args.scenario]
        if not tester.config['test_scenarios']:
            logger.error(f"Scenario not found: {args.scenario}")
            return
    
    # 运行测试
    await tester.run_all_tests()


if __name__ == "__main__":
    asyncio.run(main())
