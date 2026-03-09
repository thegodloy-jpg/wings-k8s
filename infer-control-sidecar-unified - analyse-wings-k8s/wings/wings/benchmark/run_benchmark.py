# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
性能测试运行脚本 - 支持多种测试模式和配置

主要功能：
1. 支持原始版本和优化版本切换
2. 支持同步和异步模式
3. 提供丰富的配置选项
4. 自动生成测试报告
"""

import argparse
import asyncio
import time
import os
import sys
import logging
from typing import Dict, Any

from performance_llm import performance_test_async as llm_performance_test_async
from performance_mmum import performance_test_async as mmum_performance_test_async

# Configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 全局变量：控制分隔符"="的个数
SEPARATOR_LENGTH = 40


def create_test_config(args) -> Dict[str, Any]:
    """创建测试配置"""
    config = {
        "model_type": args.model_type,
        "model_name": args.model_name,
        "model_path": args.model_path,
        "service": {
            "ip": args.ip,
            "port": args.port,
            "protocol": args.protocol
        },
        "test_parameters": {
            "thread_num": args.thread_num,
            "input_tokens_num": args.input_tokens_num,
            "output_tokens_num": args.output_tokens_num,
            "uniform_interval": args.uniform_interval,
            "warmup_num": args.warmup_num
        },
        "files": {
            "dataset_path": args.dataset_path,
            "csv_file": args.csv_file
        }
    }
    
    return config


def print_test_config(config: Dict[str, Any]):
    """Print test configuration"""
    logger.info("Performance Test Configuration")
    logger.info("=" * SEPARATOR_LENGTH)
    logger.info(f"Model Type: {config['model_type']}")
    logger.info(f"Model Name: {config['model_name']}")
    logger.info(f"Service Address:\
                 {config['service']['protocol']}://{config['service']['ip']}:{config['service']['port']}")
    logger.info(f"Concurrent Threads: {config['test_parameters']['thread_num']}")
    logger.info(f"Input Tokens: {config['test_parameters']['input_tokens_num']}")
    logger.info(f"Output Tokens: {config['test_parameters']['output_tokens_num']}")
    logger.info("=" * SEPARATOR_LENGTH)


async def run_warmup(args):
    """Execute warmup requests"""
    if args.warmup_num <= 0:
        return
    
    logger.info(f"Starting warmup, total {args.warmup_num} requests...")
    
    warmup_args = argparse.Namespace(**{
        **vars(args),
        'thread_num': args.warmup_num,
        'csv_file': 'warmup_results.csv'
    })
    
    try:
        # 根据模型类型选择测试函数
        if args.model_type == 'mmum':
            await mmum_performance_test_async(warmup_args, print_result=True, save_file=False)
        elif args.model_type == 'llm':
            await llm_performance_test_async(warmup_args, print_result=True, save_file=False)
        else:
            raise ValueError(f"model type must be llm or mmum, current is {args.model_type}")
    except Exception as e:
        logger.error(f"Warmup failed: {e}")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="Wings Performance Test Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法:
# 基本测试
python run_benchmark.py \
    --model-path /path/to/model \
    --model-name model_name \
    --ip {ip} \
    --port {port}

# 测试maas平台的https
python run_benchmark.py \
    --model-path /path/to/model \
    --model-name model_name \
    --protocol https \
    --ip {ip}/serving-gateway/7102d45851a64c5a897c4b869280e025

# 多模态测试
python run_benchmark.py \
    --model-type mmum \
    --image-height 512 \
    --image-width 512 \
    --image-count 10

# wings容器中的用法
cd /opt
python wings/benchmark/run_benchmark.py  \
    --model-name {model_name} \
    --model-path /weights \
    --ip {ip} \
    --port {port} \
    --thread-num 1 \
    --input-tokens-num 128 \
    --output-tokens-num 128
    """
    )
    
    # 模型参数
    parser.add_argument('-M', '--model-name', type=str, default="qwen", help='Model name')
    parser.add_argument('-T', '--model-type', type=str, default="llm", 
                    choices=['llm', 'embedding', 'rerank', 'mmum'], help='Model type')
    parser.add_argument('--model-path', type=str, default="/weights", help='Model path')
    
    # 多模态参数
    parser.add_argument('--image-height', type=int, default=512, help='Image height for mmum models')
    parser.add_argument('--image-width', type=int, default=512, help='Image width for mmum models')
    parser.add_argument('--image-count', type=int, default=10, help='Image count for mmum models')
    parser.add_argument('--image-root', type=str, default="./wings/benchmark/images", 
                        help='Root directory for test images')

    # 服务参数
    parser.add_argument('--ip', type=str, default="127.0.0.1", help='Service IP')
    parser.add_argument('--port', type=str, default="", help='Service port')
    parser.add_argument('--protocol', type=str, default="http", choices=['http', 'https'], help='Protocol type')

    # 测试参数
    parser.add_argument('-P', '--thread-num', type=int, default=1, help='Concurrency number')
    parser.add_argument('-I', '--input-tokens-num', type=int, default=2048, help='Input token count')
    parser.add_argument('-O', '--output-tokens-num', type=int, default=2048, help='Output token count')
    parser.add_argument('--uniform-interval', type=float, default=0, help='Uniform request interval (seconds)')
    parser.add_argument('--warmup-num', type=int, default=0, help='Warm-up request count')

    # 优化选项
    parser.add_argument('--timeout', type=int, default=30, help='Request timeout (seconds)')

    # 文件参数
    parser.add_argument('--dataset-path', type=str, default="./wings/benchmark/sonnet_20x.txt", help='Dataset path')
    parser.add_argument('-C', '--csv-file', type=str, default="benchmark_results.csv", help='Result file')

    args = parser.parse_args()
    
    # 检查数据集是否存在
    if not os.path.exists(args.dataset_path):
        logger.error(f"Dataset file not found: {args.dataset_path}")
        logger.info("Please run the following command to generate a sample dataset:")
        logger.info(f"python -m wings.benchmark.data_generator --type text --output {args.dataset_path}")
        sys.exit(1)
    
    # 创建当前配置
    config = create_test_config(args)
    
    # 打印配置
    print_test_config(config)
    
    # 执行预热
    if args.warmup_num > 0:
        asyncio.run(run_warmup(args))
    
    # 执行性能测试
    start_time = time.time()
    
    try:
        # 根据模型类型选择测试函数
        if args.model_type == 'mmum':
            result = asyncio.run(mmum_performance_test_async(args))
        elif args.model_type == 'llm':
            result = asyncio.run(llm_performance_test_async(args))
        else:
            raise ValueError(f"model type must be llm or mmum, current is {args.model_type}")
        
        # 计算总耗时
        total_time = time.time() - start_time
        
        # 添加测试概要信息
        result['Test Summary'] = {
            'Total Test Time (s)': round(total_time, 2),
        }
        
        logger.info(f"Test completed! Total time: {total_time:.2f} seconds")
    except Exception as e:
        logger.error(f"Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
