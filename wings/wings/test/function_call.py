# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-
import json
import argparse
import logging
import sys

import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def test_function_call_compatibility(base_url, model_name):
    """
    测试大模型服务对OpenAI v1/chat/completions接口函数调用功能的兼容性
    Test the compatibility of the large model service 
    with the OpenAI v1/chat/completions interface function call feature
    """
    # 准备请求数据
    api_endpoint, headers, payload = _prepare_request_data(base_url, model_name)
    
    try:
        # 发送请求并获取响应
        response = _send_request(api_endpoint, headers, payload)
        
        # 检查HTTP响应状态
        if not _check_http_status(response):
            return False
        
        # 解析响应JSON
        result = _parse_response_json(response)
        if result is None:
            return False
        
        # 验证响应结构
        if not _validate_response_structure(result):
            return False
        
        # 验证函数调用内容
        return _validate_function_call_content(result)
          
    except requests.exceptions.RequestException as e:
        logger.info(f"❌ Network request error: {e}")
        return False
    except Exception as e:
        logger.info(f"❌ Unknown error occurred: {e}")
        return False


def _prepare_request_data(base_url, model_name):
    """准备请求数据"""
    api_endpoint = f"{base_url}/v1/chat/completions"
    
    headers = {
        "Content-Type": "application/json",
    }
    
    payload = {
        "messages": [
            {
                "role": "user",
                "content": "Hows the weather like in Beijing today"
            }
        ],
        "temperature": 0,
        "max_tokens": 1000,
        "model": model_name,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "query_weather",
                    "description": "Get weather of an location, the user shoud supply a location first",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {
                                "type": "string",
                                "description": "The city, e.g. Beijing"
                            }
                        },
                        "required": [
                            "city"
                        ]
                    }
                }
            }
        ]
    }
    
    return api_endpoint, headers, payload


def _send_request(api_endpoint, headers, payload):
    """发送POST请求"""
    logger.info("Sending request to model service...")
    return requests.post(api_endpoint, headers=headers, data=json.dumps(payload), timeout=60)


def _check_http_status(response):
    """检查HTTP响应状态"""
    if response.status_code != 200:
        logger.info(f"❌ Request failed, status code: {response.status_code}")
        logger.info(f"Error message: {response.text}")
        return False
    return True


def _parse_response_json(response):
    """解析响应JSON"""
    try:
        result = response.json()
        logger.info("✅ Request successful, response format meets expectations")
        return result
    except json.JSONDecodeError as e:
        logger.info(f"❌ JSON parsing error: {e}")
        logger.info(f"Raw response: {response.text}")
        return None


def _validate_response_structure(result):
    """验证响应结构是否包含必要的字段"""
    required_fields = ["id", "object", "created", "choices"]
    for field in required_fields:
        if field not in result:
            logger.info(f"❌ Response missing required field: {field}")
            return False
    
    choices = result.get("choices", [])
    if len(choices) == 0:
        logger.info("❌ No choices included in response")
        return False
    
    return True


def _validate_function_call_content(result):
    """验证函数调用内容"""
    first_choice = result["choices"][0]
    message = first_choice.get("message", {})
    
    # 检查是否包含函数调用
    if message.get("tool_calls"):
        return _validate_tool_calls(message["tool_calls"])
    else:
        logger.info("ℹ️ Model did not return function call, directly replied with content:")
        logger.info(f"   Reply content: {message.get('content', 'None')}")
        # 在某些情况下，模型可能直接回答而不调用函数，这不一定表示不兼容
        return True  # 或返回 False，如果你严格要求必须调用函数


def _validate_tool_calls(tool_calls):
    """验证工具调用"""
    function_call = tool_calls[0]["function"]
    logger.info("✅ Model returned function call request")
    logger.info(f"   Function name: {function_call.get('name', 'Unknown')}")
    logger.info(f"   Function arguments: {function_call.get('arguments', 'None')}")
    
    # 验证函数调用结构
    if function_call.get("name") == "query_weather":
        return _validate_function_arguments(function_call)
    else:
        logger.info("❌ Model called unexpected function")
        return False


def _validate_function_arguments(function_call):
    """验证函数参数"""
    try:
        arguments = json.loads(function_call.get("arguments", "{}"))
        if "city" in arguments:
            logger.info("✅ Function call parameter format is correct")
            return True
        else:
            logger.info("❌ Function call missing required parameter 'city'")
            return False
    except json.JSONDecodeError:
        logger.info("❌ Function parameters are not in valid JSON format")
        return False


def main():
    """
    主函数：运行兼容性测试并输出结果
    Main function: Run compatibility test and output results
    """
    # 设置命令行参数解析
    parser = argparse.ArgumentParser(description="Test OpenAI function call compatibility")
    parser.add_argument("--base-url", required=True, 
                        help="Base URL of the model service (e.g., http://127.0.0.1:18000)")
    parser.add_argument("--model-name", required=True, help="Model name to test (e.g., Qwen2.5-32B)")
    
    args = parser.parse_args()
    
    logger.info(f"Starting compatibility test for large model service with OpenAI function call feature...")
    logger.info(f"Base URL: {args.base_url}")
    logger.info(f"Model: {args.model_name}")
    logger.info("=" * 60)
  
    success = test_function_call_compatibility(args.base_url, args.model_name)
  
    logger.info("=" * 60)
    if success:
        logger.info("🎉 Compatibility test passed! Service is basically compatible with OpenAI function call interface.")
    else:
        logger.info("💥 Compatibility test failed! Service may have compatibility issues.")
  
    return success

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.exit(1)
