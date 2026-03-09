# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
LLM 性能测试 - 优化版本

主要优化：
1. 异步请求处理
2. 流式响应优化
3. 连接复用
4. 内存优化
"""

import asyncio
import json
import time
import logging
import random
import concurrent
from typing import List, Dict, Tuple, Optional

import aiohttp
import numpy as np
from transformers import AutoTokenizer

from performance_base import BasePerformanceTester, SampleRequest

# Configure logger
logger = logging.getLogger(__name__)

SEPARATOR_LENGTH = 60


class LLMPerformanceTester(BasePerformanceTester):
    """LLM性能测试实现"""
    
    def __init__(self, args):
        super().__init__(args)
        
        # LLM特定的初始化
        self.tokenizer = AutoTokenizer.from_pretrained(args.model_path)
        
        # LLM特定的统计信息
        self.llm_stats = {
            'total_tokens': 0,
            'total_input_tokens': 0,
            'total_output_tokens': 0,
            'total_first_token_time': 0,
            'total_non_first_token_time': 0
        }
        
        # 更新报告结果
        self.report_result.update({
            "Model Name": args.model_name,
            "Process Num": args.thread_num,
            "Input Length": args.input_tokens_num,
            "Output Length": args.output_tokens_num
        })
        
        # 确定协议类型
        protocol = getattr(args, 'protocol', 'http')
        if args.port:
            self.url = f"{protocol}://{args.ip}:{args.port}/v1/chat/completions"
        else:
            self.url = f"{protocol}://{args.ip}/v1/chat/completions"
    
    def prepare_requests(self) -> List[SampleRequest]:
        """准备 LLM 测试请求"""
        return sample_sonnet_input(
            dataset_path=self.args.dataset_path,
            tokenizer=self.tokenizer,
            num_requests=self.args.thread_num,
            input_len=self.args.input_tokens_num,
            output_len=self.args.output_tokens_num
        )
    
    def create_request_parameters(self, request: SampleRequest) -> Dict:
        """创建 LLM 请求参数"""
        messages = self._prompt_to_message(
            prompt=request.input_data, 
            max_length=self.args.input_tokens_num,
            if_padding=True
        )
        
        return {
            "model": self.args.model_name,
            "messages": messages,
            "stream": True,
            "max_tokens": request.output_token_length,
            "temperature": 0.6,
            "top_p": 0.95,
            "stream_options": {"include_usage": True},
            "skip_special_tokens": False,
            "ignore_eos": True
        }
    
    def calculate_metrics(self):
        """计算LLM特定的性能指标"""
        if not self.results:
            return
        
        # 计算总时间
        total_time = self._calculate_total_time()
        
        # 统计成功请求
        successful_results, success_count = self._get_successful_requests()
        
        if success_count == 0:
            self.report_result["Error"] = "All requests have failed. Please check the model service."
        
        # 提取和处理数据
        metrics_data = self._extract_and_process_metrics(successful_results, success_count, total_time)
        
        # 更新报告结果
        self._update_report_result(metrics_data, success_count, total_time)

    def print_results(self):
        """打印测试结果"""
        if self.report_result['Actual Success Request Num'] != 0:
            logger.info("Performance Test Results")
            logger.info("=" * SEPARATOR_LENGTH)
            logger.info("Thread Num: {} | Input Tokens: {} | Output Tokens: {}".format(
                self.args.thread_num, self.args.input_tokens_num, self.args.output_tokens_num))
            logger.info("=" * SEPARATOR_LENGTH)
            logger.info("Actual Success Request Num                  : {}".format(
                self.report_result['Actual Success Request Num']))
            logger.info("Actual Input Token Num                      : {}".format(
                self.report_result['Actual Input Token Num']))
            logger.info("Actual Output Token Num                     : {}".format(
                self.report_result['Actual Output Token Num']))
            logger.info("=" * SEPARATOR_LENGTH)
            logger.info("Mean output TPS with first token            : {}".format(
                self.report_result['Mean TPS with first token']))
            logger.info("Mean output TPS without first token         : {}".format(
                self.report_result['Mean TPS without first token']))
            logger.info("=" * SEPARATOR_LENGTH)
            logger.info("Mean TTFT(ms)                               : {}".format(
                self.report_result['Mean TTFT(ms)']))
            logger.info("Max  TTFT(ms)                               : {}".format(
                self.report_result['Max TTFT(ms)']))
            logger.info("Min  TTFT(ms)                               : {}".format(
                self.report_result['Min TTFT(ms)']))
            logger.info("=" * SEPARATOR_LENGTH)
            logger.info("Mean TPOT(ms)                               : {}".format(
                self.report_result['Mean TPOT(ms)']))
            logger.info("Max  TPOT(ms)                               : {}".format(
                self.report_result['Max TPOT(ms)']))
            logger.info("Min  TPOT(ms)                               : {}".format(
                self.report_result['Min TPOT(ms)']))
            logger.info("=" * SEPARATOR_LENGTH)
            logger.info("Total Time(s)                               : {}".format(
                self.report_result['Total Time(s)']))
            logger.info("Quest Per Secont                            : {}".format(
                self.report_result['qps']))
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
        """获取LLM特定信息"""
        return {
            "Model Name": getattr(self.args, 'model_name', 'Unknown'),
            "Model Path": getattr(self.args, 'model_path', 'Unknown'),
            "Tokenizer": self.tokenizer.__class__.__name__,
            "Vocabulary Size": self.tokenizer.vocab_size
        }


    async def run_test_async(self, print_result=True, save_file=True):
        """异步执行性能测试"""
        try:
            # 准备请求
            self.requests = self.prepare_requests()
            
            # 创建请求参数
            # 定义处理单个请求的函数
            def process_request(request):
                params = self.create_request_parameters(request)
                messages = params['messages']
                prompt_formatted = self.tokenizer.apply_chat_template(
                    messages, add_generation_prompt=True, tokenize=False)
                input_token_length = len(self.tokenizer(prompt_formatted).input_ids)
                
                return {
                    "parameters": params,
                    "input_token_length": input_token_length,
                    "output_token_length": request.output_token_length
                }
            
            # 使用线程池并行处理所有请求
            with concurrent.futures.ThreadPoolExecutor() as executor:
                # 提交所有任务并获取结果
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
                        "tpot": round(result['non_first_token_time'] / (output_token_length - 1) * 1000, 4)
                    })
                self.results[str(i)] = result
                if result.get('success', False):
                    self.stats['success_count'] += 1

    async def send_request_async(self, parameters: Dict, headers: Dict) -> Dict:
        """异步发送请求并处理流式响应"""
        start_time = time.time()
        para_json = parameters["parameters"]
        input_contents = para_json['messages'][0]['content']
        input_token_length = parameters.get("input_token_length", 0)
        
        try:
            output_contents, first_token_time = await self._process_request(para_json, headers, start_time)
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
        # async with self.connection_pool.get_session() as session:
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=6 * 60 * 60)
        async with aiohttp.ClientSession(trust_env=True, connector=connector, timeout=timeout) as session:
            async with session.post(self.url, json=para_json, headers=headers) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f"HTTP {response.status}: {error_text}")
                return await self._process_stream_response(response, start_time)

    async def _process_stream_response(self, response, start_time: float) -> Tuple[str, Optional[float]]:
        """处理流式响应数据"""
        contents = ""
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
                contents += content_chunk
        return contents, first_token_time

    def _process_data_chunk(self, data_str: str, first_token_time: Optional[float], 
                            start_time: float) -> Tuple[str, Optional[float]]:
        """处理单个数据块并更新首令牌时间"""
        data = json.loads(data_str)
        content_chunk = ""
        
        if data['choices']:
            delta = data['choices'][0]['delta']
            if 'content' in delta and delta['content'] is not None:
                content_chunk = delta['content']
            elif 'reasoning_content' in delta and delta['reasoning_content'] is not None:
                content_chunk = delta['reasoning_content']
            
            # 记录首 token 时间
            if first_token_time is None and content_chunk:
                first_token_time = time.time() - start_time
        
        return content_chunk, first_token_time
    
    def _prompt_to_message(self, prompt: str, max_length: int, if_padding=False) -> List[Dict]:
        """将提示转换为消息格式"""
        if not if_padding:
            messages = [{"role": "user", "content": prompt}]
        else:
            messages = [{"role": "user", "content": prompt}]   
            prompt_formatted = self.tokenizer.apply_chat_template(
                messages, add_generation_prompt=True, tokenize=False)
            prompt_len = len(self.tokenizer(prompt_formatted).input_ids)
            while prompt_len < max_length:
                needed_length = max_length - prompt_len
                random_char = random.choices(prompt, k=needed_length)
                prompt = prompt + ''.join(random_char)
                messages = [{"role": "user", "content": prompt}]
                prompt_formatted = self.tokenizer.apply_chat_template(
                    messages, add_generation_prompt=True, tokenize=False)
                prompt_len = len(self.tokenizer(prompt_formatted).input_ids)

            while prompt_len > max_length:
                prompt = prompt[:-1]
                messages = [{"role": "user", "content": prompt}]
                prompt_formatted = self.tokenizer.apply_chat_template(
                    messages, add_generation_prompt=True, tokenize=False)
                prompt_len = len(self.tokenizer(prompt_formatted).input_ids)

        return messages

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

    def _extract_and_process_metrics(self, successful_results, success_count, total_time):
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
            system_tps_with_input = (total_input_tokens + total_output_tokens) / total_time
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

    def _update_report_result(self, metrics_data, success_count, total_time):
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
            "Actual Output Token Num": round(metrics_data["avg_output_tokens"], 4),
            "Total Input Tokens": metrics_data["total_input_tokens"],
            "Total Output Tokens": metrics_data["total_output_tokens"],
            "Total Time(s)": round(total_time, 4),
            "qps": round(metrics_data["qps"], 4),
            "Actual Success Request Num": success_count,
            "Success Rate": round(success_count / results_count * 100, 2)
        })


def sample_sonnet_input(
    dataset_path: str,
    tokenizer,
    num_requests: int,
    input_len: int = 1024,
    output_len: int = 128
) -> List[SampleRequest]:
    """
    优化版本的数据集采样函数（已移除 prefix_len）

    主要优化：
    1. 缓存 tokenized 结果
    2. 预计算平均长度
    3. 批量处理
    """
    try:
        # 读取数据集
        with open(dataset_path, encoding="utf-8") as f:
            data = [line.strip() for line in f if line.strip()]
        
        if not data:
            raise ValueError(f"The dataset file {dataset_path} is empty")
        
        # 预计算所有行的 token 长度（缓存）
        tokenized_lengths = []
        for line in data:
            tokens = tokenizer(line).input_ids
            tokenized_lengths.append(len(tokens))
        
        # 计算平均长度
        avg_len = np.mean(tokenized_lengths)
        
        # 构建基础提示
        base_prompt = "Pick as many lines as you can from these poem lines:\n"
        base_msg = [{"role": "user", "content": base_prompt}]
        base_fmt = tokenizer.apply_chat_template(base_msg, add_generation_prompt=True, tokenize=False)
        base_offset = len(tokenizer(base_fmt).input_ids)
        
        if input_len <= base_offset:
            raise ValueError(
                f"'input_len' must be higher than the base prompt length ({base_offset})"
            )
        
        # 计算需要的行数
        available_len = input_len - base_offset
        num_input_lines = min(int(available_len / avg_len), len(data))

        # 批量生成样本
        samples = []
        for _ in range(num_requests):
            # 随机选择行
            selected_lines = np.random.choice(
                data,
                size=num_input_lines,
                replace=True
            ).tolist()
            
            # 构建提示
            prompt = f"{base_prompt}{''.join(selected_lines)}"
            
            # 使用缓存的模板应用
            msg = [{"role": "user", "content": prompt}]
            prompt_formatted = tokenizer.apply_chat_template(
                msg,
                add_generation_prompt=True, 
                tokenize=False
            )
            
            # 计算 token 长度
            prompt_len = len(tokenizer(prompt_formatted).input_ids)
            
            samples.append(
                SampleRequest(
                    input_data=prompt,
                    input_token_length=prompt_len,
                    output_token_length=output_len,
                )
            )
        
        return samples

        
    except FileNotFoundError as e:
        raise FileNotFoundError(f"Dataset file does not exist: {dataset_path}") from e
    except Exception as e:
        raise Exception(f"An error occurred while sampling data: {str(e)}") from e


async def performance_test_async(args, print_result=True, save_file=True):
    """异步性能测试入口函数"""
    tester = LLMPerformanceTester(args)
    return await tester.run_test_async(print_result, save_file)
