# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
多模态理解模型性能测试 - 优化版本

主要功能：
1. 支持图像+文本的多模态输入
2. 异步请求处理
3. 流式响应优化
4. 连接复用
5. 自动下载和管理测试图像
"""

import asyncio
import json
import time
import logging
import os
import base64
import concurrent
import random
from typing import List, Dict, Tuple, Optional

import aiohttp
import numpy as np
from PIL import Image
from transformers import AutoTokenizer, Qwen2VLProcessor

from performance_base import BasePerformanceTester, SampleRequest

# Configure logger
logger = logging.getLogger(__name__)

SEPARATOR_LENGTH = 60


class ImageManager:
    """图像管理器 - 负责加载和管理测试图像"""
    
    def __init__(self, base_dir: str = "images", image_size: Tuple[int, int] = (1920, 1080)):
        self.base_dir = base_dir
        self.image_size = image_size
        self._ensure_directory()

    @staticmethod
    def encode_image_to_base64(image_path: str) -> str:
        """将图像编码为 base64 字符串"""
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')   

    def get_image_data(self, image_paths: List[str]) -> List[Dict]:
        """获取图像数据列表，包含 base64 编码和基本信息"""
        image_data_list = []
        
        for path in image_paths:
            with Image.open(path) as img:
                # 获取图像基本信息
                width, height = img.size
                
                # 编码为 base64
                base64_data = self.encode_image_to_base64(path)
                
                image_data_list.append({
                    "path": path,
                    "base64": base64_data,
                    "width": width,
                    "height": height,
                    "format": img.format or "PNG"
                })
        
        return image_data_list
    
    def get_available_images(self, num_images: int) -> List[str]:
        """获取指定数量的可用图像路径"""
        existing_images = [f for f in os.listdir(self.image_dir) if f.endswith('.png')]
        
        if len(existing_images) < num_images:
            raise ValueError(f"Not enough images in {self.image_dir}.\
                               Found {len(existing_images)}, need {num_images}. \
                               Please run data_generator.py first.")
        
        # 返回指定数量的图像路径
        image_paths = [os.path.join(self.image_dir, f) for f in existing_images[:num_images]]
        return image_paths

    def _ensure_directory(self):
        """确保图像目录存在"""
        subdir = f"images_{self.image_size[0]}x{self.image_size[1]}"
        self.image_dir = os.path.join(self.base_dir, subdir)
        if not os.path.exists(self.image_dir):
            raise FileNotFoundError(f"Image directory not found: {self.image_dir}. Please run data_generator.py first.")
        

class MMUMPerformanceTester(BasePerformanceTester):
    """多模态理解模型性能测试实现"""
    
    def __init__(self, args):
        super().__init__(args)
        
        # MMUM特定的初始化
        self.tokenizer = AutoTokenizer.from_pretrained(args.model_path)
        self.processor = Qwen2VLProcessor.from_pretrained(args.model_path, trust_remote_code=True, use_fast=True)
        
        # 图像管理器
        self.image_manager = ImageManager(
            base_dir=getattr(args, 'image_root', 'images'),
            image_size=(
                getattr(args, 'image_width', 1920),
                getattr(args, 'image_height', 1080)
            )
        )
        
        # MMUM特定的统计信息
        self.mmum_stats = {
            'total_images': 0,
            'total_image_pixels': 0,
            'total_text_tokens': 0,
            'total_output_tokens': 0,
            'total_first_token_time': 0,
            'total_non_first_token_time': 0
        }
        
        # 更新报告结果
        self.report_result.update({
            "Model Name": args.model_name,
            "Process Num": args.thread_num,
            "Input Length": args.input_tokens_num,
            "Output Length": args.output_tokens_num,
            "Image Width": getattr(args, 'image_width', 512),
            "Image Height": getattr(args, 'image_height', 512),
            "Image Count": getattr(args, 'image_count', 1)
        })
        
        # 确定协议类型
        protocol = getattr(args, 'protocol', 'http')
        if args.port:
            self.url = f"{protocol}://{args.ip}:{args.port}/v1/chat/completions"
        else:
            self.url = f"{protocol}://{args.ip}/v1/chat/completions"
    
    def prepare_requests(self) -> List[SampleRequest]:
        """准备多模态测试请求"""
        # 获取测试图像
        image_count = getattr(self.args, 'image_count', 1)
        image_paths = self.image_manager.get_available_images(image_count)
        image_data_list = self.image_manager.get_image_data(image_paths)
        
        # 准备文本输入
        text_requests = generate_multimodal_prompts(
            tokenizer=self.tokenizer,
            num_requests=self.args.thread_num,
            input_len=self.args.input_tokens_num,
            output_len=self.args.output_tokens_num,
            return_prompt_formatted=False
        )
        
        # 组合图像和文本
        requests = []
        for i, text_request in enumerate(text_requests):
            # 为每个请求选择图像（可以重复使用）
            image_data = image_data_list[i % len(image_data_list)]
            
            # 创建多模态输入数据
            multimodal_input = {
                "text": text_request.input_data,
                "image": image_data
            }
            
            requests.append(
                SampleRequest(
                    input_data=multimodal_input,
                    input_token_length=text_request.input_token_length,
                    output_token_length=text_request.output_token_length
                )
            )
        
        return requests
    
    def create_request_parameters(self, request: SampleRequest) -> Dict:
        """创建多模态请求参数"""
        multimodal_input = request.input_data
        text = multimodal_input["text"]
        image_data = multimodal_input["image"]
        
        # 构建多模态消息
        content = [
            {
                "type": "text",
                "text": text
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/{image_data['format'].lower()};base64,{image_data['base64']}"
                }
            }
        ]
        
        return {
            "model": self.args.model_name,
            "messages": [{"role": "user", "content": content}],
            "stream": True,
            "max_tokens": request.output_token_length,
            "temperature": 0.6,
            "top_p": 0.95,
            "stream_options": {"include_usage": True},
            "skip_special_tokens": False,
            "ignore_eos": True
        }
    
    def calculate_metrics(self):
        """计算MMUM特定的性能指标"""
        if not self.results:
            return
        
        # 计算总时间
        total_time = self._calculate_total_time()
        
        # 统计成功请求
        successful_results, success_count = self._get_successful_requests()
        
        if success_count == 0:
            self.report_result["Error"] = "All requests have failed. Please check the model service."
        
        # 计算图像token数
        image_tokens = self._calculate_image_tokens()

        # 提取和处理数据
        metrics_data = self._extract_and_process_metrics(successful_results, success_count, total_time, image_tokens)
    
        # 更新报告结果
        self._update_report_result(metrics_data, image_tokens, success_count, total_time)

    def print_results(self):
        """打印测试结果"""
        if self.report_result['Actual Success Request Num'] != 0:
            logger.info("Multimodal Understanding Model Performance Test Results")
            logger.info("=" * SEPARATOR_LENGTH)
            logger.info("Thread Num: {} | Input Tokens: {} | Output Tokens: {} | Image: {}x{}".format(
                self.args.thread_num, 
                self.args.input_tokens_num, 
                self.args.output_tokens_num,
                self.report_result.get("Image Width", 1920),
                self.report_result.get("Image Height", 1080)
            ))
            logger.info("=" * SEPARATOR_LENGTH)
            logger.info("Actual Success Request Num                  : {}".format(
                self.report_result['Actual Success Request Num']))
            logger.info("Actual Input Token Num                      : {}".format(
                self.report_result['Actual Input Token Num']))
            logger.info("Actual Image Token Num                      : {}".format(
                self.report_result['Actual Image Token Num']))
            logger.info("Actual Output Token Num                     : {}".format(
                self.report_result['Actual Output Token Num']))
            logger.info("=" * SEPARATOR_LENGTH)
            logger.info("Mean output TPS with first token            : {}".format(
                self.report_result['Mean TPS with first token']))
            logger.info("Mean output TPS without first token         : {}".format(
                self.report_result['Mean TPS without first token']))
            logger.info("=" * SEPARATOR_LENGTH)
            logger.info("Mean TTFT(ms)                               : {}".format(self.report_result['Mean TTFT(ms)']))
            logger.info("Max  TTFT(ms)                               : {}".format(self.report_result['Max TTFT(ms)']))
            logger.info("Min  TTFT(ms)                               : {}".format(self.report_result['Min TTFT(ms)']))
            logger.info("=" * SEPARATOR_LENGTH)
            logger.info("Mean TPOT(ms)                               : {}".format(self.report_result['Mean TPOT(ms)']))
            logger.info("Max  TPOT(ms)                               : {}".format(self.report_result['Max TPOT(ms)']))
            logger.info("Min  TPOT(ms)                               : {}".format(self.report_result['Min TPOT(ms)']))
            logger.info("=" * SEPARATOR_LENGTH)
            logger.info("Total Time(s)                               : {}".format(
                self.report_result['Total Time(s)']))
            logger.info("Quest Per Secont                            : {}".format(self.report_result['qps']))
            logger.info("System TPS with input                       : {}".format(
                self.report_result['System TPS with input']))
            logger.info("=" * SEPARATOR_LENGTH)

            error_count = self.stats['error_count']
            if error_count > 0:
                logger.warning(f"There are ***{error_count}*** requests failed!")
        else:
            logger.error("All requests failed! Please check if the model service is " \
            "running normally or if the test parameters are correct")
    
    def get_model_specific_info(self) -> Dict:
        """获取MMUM特定信息"""
        return {
            "Model Name": getattr(self.args, 'model_name', 'Unknown'),
            "Model Path": getattr(self.args, 'model_path', 'Unknown'),
            "Tokenizer": self.tokenizer.__class__.__name__,
            "Vocabulary Size": self.tokenizer.vocab_size,
            "Model Type": "Multimodal Understanding",
            "Image Size": f"{getattr(self.args, 'image_width', 1920)}x{getattr(self.args, 'image_height', 1080)}",
            "Images Per Request": getattr(self.args, 'image_count', 1)
        }
    
    async def run_test_async(self, print_result=True, save_file=True):
        """异步执行性能测试"""
        try:
            # 准备请求
            self.requests = self.prepare_requests()
            
            # 创建请求参数
            def process_request(request):
                params = self.create_request_parameters(request)
                # 对于多模态，输入token长度已经在prepare_requests中设置
                return {
                    "parameters": params,
                    "input_token_length": request.input_token_length,
                    "output_token_length": request.output_token_length
                }
            
            # 使用线程池并行处理所有请求
            with concurrent.futures.ThreadPoolExecutor() as executor:
                parameters_list = list(executor.map(process_request, self.requests))
            
            # 执行异步测试
            await self.execute_requests_async(parameters_list)
            
            # 计算指标
            self.calculate_metrics()
            
            # 添加模型特定信息
            model_info = self.get_model_specific_info()
            self.report_result.update(model_info)
            
            # 输出结果
            if print_result:
                self.print_results()
                
            # 保存结果
            if save_file:
                self._save_results()
                
            return self.report_result
        except Exception as e:
            raise Exception(f"Performance test failed: {str(e)}") from e
        
    async def execute_requests_async(self, parameters_list: List[Dict]):
        """异步执行请求并收集结果"""
        tasks = []
        headers = {"Content-type": "application/json"}
        
        for i, params_data in enumerate(parameters_list):
            if hasattr(self.args, 'uniform_interval') and self.args.uniform_interval > 0:
                # 均匀分布模式
                delay = i * (self.args.uniform_interval / self.args.thread_num)
                task = asyncio.create_task(
                    self._delayed_request(params_data, headers, delay)
                )
            else:
                task = asyncio.create_task(
                    self.send_request_async(
                        params_data,
                        headers
                    )
                )
            tasks.append(task)
        
        # 等待所有任务完成
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 处理结果
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                self.results[str(i)] = {
                    "input_token_length": parameters_list[i]["input_token_length"],
                    "infer_time": 0,
                    "error": str(result),
                    "success": False
                }
                self.stats['error_count'] += 1
            else:
                output_token_length = len(self.tokenizer.tokenize(result["output_contents"]))
                if output_token_length <= 1:
                    result.update({
                        "output_token_length": output_token_length,
                        "non_first_token_throughput": 0,
                        "with_first_token_throughput": 0,
                        "tpot": 0
                    })
                else:
                    result.update({
                        "output_token_length": output_token_length,
                        "non_first_token_throughput": round(
                            (output_token_length - 1) / result['non_first_token_time'], 4),
                        "with_first_token_throughput": round(
                            output_token_length / result['infer_time'], 4),
                        "tpot": round(
                            result['non_first_token_time'] / (output_token_length - 1) * 1000, 4)
                    })
                self.results[str(i)] = result
                if result.get('success', False):
                    self.stats['success_count'] += 1
    
    async def send_request_async(self, parameters: Dict, headers: Dict) -> Dict:
        """异步发送请求并处理流式响应"""
        para_json = parameters["parameters"]
        input_token_length = parameters.get("input_token_length", 0)
        input_contents = para_json['messages'][0]['content'][0]['text']
        start_time = time.time()
        
        try:
            output_contents, first_token_time = await self._process_request(
                para_json, headers, start_time
            )
            end_time = time.time()
            
            infer_time = end_time - start_time
            non_first_token_time = infer_time - first_token_time if first_token_time else 0
        
            return {
                "input_contents": input_contents,
                "output_contents": output_contents,
                "input_token_length": input_token_length,
                "infer_time": infer_time,
                "first_token_time": first_token_time or 0,
                "non_first_token_time": non_first_token_time,
                "start_time": start_time,
                "end_time": end_time,
                "success": True
            }

        except asyncio.TimeoutError as e:
            raise Exception("Request timed out") from e
        except aiohttp.ClientError as e:
            raise Exception(f"Network error: {str(e)}") from e
        except Exception as e:
            raise Exception(f"Request failed: {str(e)}") from e

    async def _process_request(self, para_json: Dict, headers: Dict, start_time: float) -> Tuple[str, Optional[float]]:
        """处理HTTP请求和流式响应"""
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=6 * 60 * 60)
        async with aiohttp.ClientSession(trust_env=True, connector=connector, timeout=timeout) as session:
            async with session.post(self.url, json=para_json, headers=headers) as response:
                await self._check_response_status(response)
                return await self._process_stream_response(response, start_time)

    async def _check_response_status(self, response) -> None:
        """检查HTTP响应状态"""
        if response.status != 200:
            error_text = await response.text()
            raise Exception(f"HTTP {response.status}: {error_text}")

    async def _process_stream_response(self, response, start_time: float) -> Tuple[str, Optional[float]]:
        """处理流式响应数据"""
        output_contents = ""
        first_token_time = None
        
        async for line in response.content:
            line = line.decode('utf-8')
            
            if line.startswith("data:"):
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                
                content_chunk, first_token_time = self._process_data_chunk(
                    data_str, first_token_time, start_time
                )
                output_contents += content_chunk
        
        return output_contents, first_token_time

    def _process_data_chunk(self, data_str: str, first_token_time: Optional[float], 
                            start_time: float) -> Tuple[str, Optional[float]]:
        """处理单个数据块并更新首令牌时间"""
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            return "", first_token_time
        
        content_chunk = ""
        
        if data.get('choices'):
            delta = data['choices'][0].get('delta', {})
            
            if delta.get('content') is not None:
                content_chunk = delta['content']
            elif delta.get('reasoning_content') is not None:
                content_chunk = delta['reasoning_content']
            
            # 记录首 token 时间
            if first_token_time is None and content_chunk:
                first_token_time = time.time() - start_time
        
        return content_chunk, first_token_time

    def _calculate_total_time(self):
        """计算总时间"""
        start_times = [v.get("start_time", 0) for v in self.results.values() if v['success']]
        end_times = [v.get("end_time", 0) for v in self.results.values() if v['success']]
        return max(end_times) - min(start_times) if start_times and end_times else 0

    def _get_successful_requests(self):
        """获取成功的请求"""
        successful_results = [v for v in self.results.values() if v.get("success", False)]
        success_count = len(successful_results)
        return successful_results, success_count

    def _extract_and_process_metrics(self, successful_results, success_count, total_time, image_tokens):
        """提取和处理指标数据"""
        output_tokens, input_tokens, ttft, tpot = [], [], [], []
        tps_with_first_token_sum, tps_without_first_token_sum, qps = 0, 0, 0
        mean_ttft, mean_tpot, max_ttft, min_ttft = 0, 0, 0, 0
        max_tpot, min_tpot, mean_tps_with_first, mean_tps_without_first = 0, 0, 0, 0
        avg_input_tokens, avg_output_tokens, system_tps_with_input = 0, 0, 0
                
        for v in successful_results:
            output_tokens.append(v.get("output_token_length", 0))
            input_tokens.append(v.get("input_token_length", 0))
            ttft.append(v.get("first_token_time", 0))
            tpot.append(v.get("tpot", 0))
            tps_with_first_token_sum += v.get("with_first_token_throughput", 0)
            tps_without_first_token_sum += v.get("non_first_token_throughput", 0)
        
        total_output_tokens = sum(output_tokens)
        total_input_tokens = sum(input_tokens)
        total_image_tokens = image_tokens * success_count
        
        # 计算统计量
        if ttft:
            mean_ttft = np.mean(ttft) * 1000
            mean_tpot = np.mean(tpot)
            max_ttft = np.max(ttft) * 1000 
            min_ttft = np.min(ttft) * 1000
        if tpot:
            max_tpot = np.max(tpot)
            min_tpot = np.min(tpot)
        if success_count:
            system_tps_with_input = (total_input_tokens + total_output_tokens + 
                                     total_image_tokens) / total_time
            qps = success_count / total_time
            mean_tps_with_first = tps_with_first_token_sum / success_count
            mean_tps_without_first = tps_without_first_token_sum / success_count
            avg_input_tokens = total_input_tokens / success_count
            avg_output_tokens = total_output_tokens / success_count
        
        return {
            "mean_ttft": mean_ttft,
            "mean_tpot": mean_tpot,
            "max_ttft": max_ttft,
            "min_ttft": min_ttft,
            "max_tpot": max_tpot,
            "min_tpot": min_tpot,
            "system_tps_with_input": system_tps_with_input,
            "mean_tps_with_first": mean_tps_with_first,
            "mean_tps_without_first": mean_tps_without_first,
            "tps_with_first_token": tps_with_first_token_sum,
            "tps_without_first_token_sum": tps_without_first_token_sum,
            "avg_input_tokens": avg_input_tokens,
            "avg_output_tokens": avg_output_tokens,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "qps": qps
        }

    def _calculate_image_tokens(self):
        """计算图像token数"""
        # 获取第一张图片用于计算token数（假设所有图片尺寸相同）
        image_paths = self.image_manager.get_available_images(1)
        image = Image.open(image_paths[0]).convert("RGB")
        
        # 使用Qwen2VLProcessor计算图像token数
        test_prompt = "What is this image about?"
        fmt = self.processor.apply_chat_template(
            [{"role": "user", "content": [
                {"type": "image"}, {"type": "text", "text": test_prompt}
            ]}], add_generation_prompt=True, tokenize=False
        )
        batch = self.processor(text=[fmt], images=[image], return_tensors="pt")
        return batch["input_ids"].shape[1] - len(self.tokenizer(fmt).input_ids)

    def _update_report_result(self, metrics_data, image_tokens, success_count, total_time):
        """更新报告结果"""
        results_count = len(self.results)
        
        self.report_result.update({
            "Mean TTFT(ms)": round(metrics_data["mean_ttft"], 4),
            "Mean TPOT(ms)": round(metrics_data["mean_tpot"], 4),
            "Mean TPS with first token": round(metrics_data["mean_tps_with_first"], 4),
            "Mean TPS without first token": round(metrics_data["mean_tps_without_first"], 4),
            "Max TTFT(ms)": round(metrics_data["max_ttft"], 4),
            "Min TTFT(ms)": round(metrics_data["min_ttft"], 4),
            "Max TPOT(ms)": round(metrics_data["max_tpot"], 4),
            "Min TPOT(ms)": round(metrics_data["min_tpot"], 4),
            "System TPS with input": round(metrics_data["system_tps_with_input"], 4),
            "Output TPS with first token": round(metrics_data["tps_with_first_token"], 4),
            "Output TPS without first token": round(metrics_data["tps_without_first_token_sum"], 4),
            "Actual Input Token Num": round(metrics_data["avg_input_tokens"], 4),
            "Actual Image Token Num": image_tokens,
            "Actual Output Token Num": round(metrics_data["avg_output_tokens"], 4),
            "Total Input Tokens": metrics_data["total_input_tokens"],
            "Total Text Tokens": metrics_data["total_input_tokens"] - (image_tokens * success_count),
            "Total Image Tokens": image_tokens * success_count,
            "Total Output Tokens": metrics_data["total_output_tokens"],
            "Total Time(s)": round(total_time, 4),
            "qps": round(metrics_data["qps"], 4),
            "Actual Success Request Num": success_count,
            "Success Rate": round(success_count / results_count * 100, 2)
        })


def generate_multimodal_prompts(
    tokenizer,
    num_requests: int,
    input_len: int = 1024,
    output_len: int = 128,
    return_prompt_formatted: bool = False
) -> List[SampleRequest]:
    """
    Generate multimodal test text input without external dataset
    Optimized version: use long sentences for padding, 
    only use short characters when less than 5 tokens remaining
    """
    # Long sentences for padding (meaningful content)
    long_padding_sentences = [
        "This image shows extremely rich visual elements and artistic expression.",
        "Through careful composition and color usage, it creates a unique visual experience.",
        "Every detail in the picture has been carefully arranged, reflecting the creator's "
        "professional standards.",
        "The overall visual effect is impressive, demonstrating superb photographic skills.",
        "The treatment of light and shadow is just right, enhancing the three-dimensional and "
        "layered feel of the picture.",
        "The color matching is harmonious and unified, creating a specific emotional atmosphere "
        "and artistic effect.",
        "The composition layout is reasonable, the subject is prominent, and the background is "
        "concise but not simple.",
        "This picture captures a decisive moment with strong visual impact.",
        "Through unique perspective and expression techniques, it shows the extraordinary in "
        "the ordinary.",
        "The various elements in the picture echo each other, jointly forming a complete "
        "visual narrative."
    ]
    
    # Common padding characters (only for final fine-tuning)
    padding_chars = "hello"
    
    # Build base prompt
    base_prompt = "Please describe the content of this image in detail, \
        including the objects, scenes, colors, and any noteworthy details:\n"
    base_msg = [{"role": "user", "content": base_prompt}]
    base_fmt = tokenizer.apply_chat_template(base_msg, add_generation_prompt=True, tokenize=False)
    base_offset = len(tokenizer(base_fmt).input_ids)
    
    if input_len <= base_offset:
        raise ValueError(
            f"'input_len' must be higher than the base prompt length ({base_offset})"
        )
    
    # Generate samples
    samples = []
    for _ in range(num_requests):
        # Build initial prompt        
        # 应用聊天模板并调整长度
        def adjust_prompt_length(prompt: str, target_len: int) -> str:
            """将提示长度调整为目标值，优先使用长句子进行填充"""
            msg = [{"role": "user", "content": prompt}]
            prompt_formatted = tokenizer.apply_chat_template(
                msg, add_generation_prompt=True, tokenize=False
            )
            current_len = len(tokenizer(prompt_formatted).input_ids)
            
            # 如果太短，计算所需的填充内容
            if current_len < target_len:
                remaining = target_len - current_len
                
                # 预先计算长句的平均token长度
                long_sentences_tokens = []
                for sentence in long_padding_sentences[:10]:  # 取前10个样本计算平均（原注释为3，此处根据实际代码修正）
                    test_msg = [{"role": "user", "content": ", " + sentence}]
                    test_fmt = tokenizer.apply_chat_template(
                        test_msg, add_generation_prompt=True, tokenize=False
                    )
                    long_sentences_tokens.append(len(tokenizer(test_fmt).input_ids))
                avg_long_sentence_len = np.mean(long_sentences_tokens) if long_sentences_tokens else 20
                
                # 计算需要多少个长句子
                while remaining >= 5:
                    # 估计需要多少个长句子
                    num_long_sentences = int(remaining / avg_long_sentence_len) + 1
                    
                    if num_long_sentences > 0:
                        # 随机选择长句子（允许重复）
                        selected_long = [random.choice(long_padding_sentences) for _ in range(num_long_sentences)]
                        prompt += ", " + ", ".join(selected_long)
                        # 重新计算当前长度
                        msg = [{"role": "user", "content": prompt}]
                        prompt_formatted = tokenizer.apply_chat_template(
                            msg, add_generation_prompt=True, tokenize=False
                        )
                        current_len = len(tokenizer(prompt_formatted).input_ids)
                        remaining = target_len - current_len
                        
            # 最后使用短字符进行精确调整
            while current_len < target_len:
                # 添加一个随机字符
                prompt += random.choice(padding_chars)
                msg = [{"role": "user", "content": prompt}]
                prompt_formatted = tokenizer.apply_chat_template(
                    msg, add_generation_prompt=True, tokenize=False
                )
                current_len = len(tokenizer(prompt_formatted).input_ids)
            
            # 如果太长，则截断
            while current_len > target_len:
                # 尝试删除最后一个字符
                prompt = prompt[:-1]
                msg = [{"role": "user", "content": prompt}]
                prompt_formatted = tokenizer.apply_chat_template(
                    msg, add_generation_prompt=True, tokenize=False
                )
                current_len = len(tokenizer(prompt_formatted).input_ids)
            return prompt

        # 调整提示长度
        prompt = adjust_prompt_length(base_prompt, input_len)

        # 最终验证
        msg = [{"role": "user", "content": prompt}]
        prompt_formatted = tokenizer.apply_chat_template(
            msg, add_generation_prompt=True, tokenize=False
        )
        prompt_len = len(tokenizer(prompt_formatted).input_ids)

        samples.append(
            SampleRequest(
                input_data=prompt_formatted if return_prompt_formatted else prompt,
                input_token_length=prompt_len,
                output_token_length=output_len,
            )
        )
    
    return samples


async def performance_test_async(args, print_result=True, save_file=True):
    """异步性能测试入口函数"""
    tester = MMUMPerformanceTester(args)
    return await tester.run_test_async(print_result, save_file)
