# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.

import os
import stat
import json
from typing import Any, Dict
import logging

logger = logging.getLogger(__name__)

INDENT = 4


def get_directory_size(path: str) -> int:
    """计算目录总大小(字节)
    
    Args:
        path: 目录路径
        
    Returns:
        int: 目录总大小(字节)
    """
    total_size = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if not os.path.islink(fp):
                total_size += os.path.getsize(fp)
    return total_size


def safe_write_file(file_path: str,
                   content: Any,
                   is_json: bool = False,
                   flags: int = os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                   modes: int = stat.S_IRUSR | stat.S_IWUSR) -> bool:
    """
    安全写入文件，支持文本和JSON格式
    
    Args:
        file_path: 文件路径
        content: 要写入的内容
        is_json: 是否JSON格式数据
        flags: 文件打开标志，默认O_WRONLY|O_CREAT
        modes: 文件权限，默认用户读写(600)
        
    Returns:
        bool: 是否写入成功
    """
    try:
        with os.fdopen(os.open(file_path, flags, modes), 'w') as f:
            if is_json:
                json.dump(content, f, indent=INDENT)
            else:
                f.write(content)
        return True
    except Exception as e:
        logger.error(f"Failed to write file {file_path}: {e}", exc_info=True)
        return False


def check_permission_640(file_path):
    """
    检查指定文件是否具有640权限
    
    参数:
        file_path (str): 要检查的文件路径
        
    返回:
        bool: 权限正确返回True，否则返回False
        
    异常:
        FileNotFoundError: 文件不存在时抛出
        PermissionError: 没有权限访问文件时抛出
        OSError: 其他文件系统相关错误
    """
    try:
        # 获取文件状态信息
        stat_info = os.stat(file_path)
        
        # 提取权限数值（与0o777进行位与，只保留权限部分）
        file_permissions = stat_info.st_mode & 0o777
        
        # 验证是否为640权限（八进制0o640）
        if file_permissions == 0o640:
            message = f"File '{file_path}' has correct permissions: 640"
            logger.info(message)
            return True
        else:
            message = f"File '{file_path}' has incorrect permissions. " \
                      f"Current permissions: octal {oct(file_permissions)}, " \
                      f"please change to permission 640!"
            logger.info(message)
            return False
            
    except FileNotFoundError:
        logger.error(f"Error: File '{file_path}' does not exist")
        raise
    except PermissionError:
        logger.error(f"Error: No permission to access file '{file_path}'")
        raise
    except OSError as e:
        logger.error(f"OS error occurred while checking permissions: {e}")
        raise OSError(f"Failed to check file permissions: {e}") from e


def check_torch_dtype(json_file_path):
    """
    检查JSON文件中的torch_dtype字段是否为"bfloat16"
    
    参数:
        json_file_path (str): JSON文件的路径
        
    返回:
        bool: 如果检查通过返回True
        
    异常:
        ValueError: 如果torch_dtype的值为"bfloat16"时抛出
        FileNotFoundError: 如果指定路径的文件不存在
        json.JSONDecodeError: 如果文件不是有效的JSON格式
        IOError: 处理文件时发生的其他I/O错误
    """
    try:
        with open(json_file_path, 'r') as f:
            data = json.load(f)
            
        # 检查torch_dtype字段
        if data.get('torch_dtype') == 'bfloat16':
            error_msg = "Ascend310 does not support bfloat16. Please modify the config.json " \
            "under the model weight path and change torch_dtype to float16"
            raise ValueError(error_msg)
            
        return True
        
    except FileNotFoundError:
        logger.error(f"The file {json_file_path} was not found")
        raise
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON format in file: {e}")
        raise
    except IOError as e:
        logger.error(f"An error occurred while reading the file: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error checking torch dtype: {e}")
        raise RuntimeError(f"Failed to check torch dtype: {e}") from e


def load_json_config(file_path: str) -> Dict[str, Any]:
    """
    加载单个 JSON 配置文件。

    Args:
        file_path (str): JSON 配置文件的路径。

    Returns:
        Dict[str, Any]: 加载得到的配置字典。如果文件不存在或解析失败，返回空字典。
    """
    if not os.path.exists(file_path):
        logger.warning(f"Config file not found: {file_path}")
        return {}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
            logger.info(f"Successfully loaded config file: {file_path}")
            return config_data
    except json.JSONDecodeError:
        logger.error(f"Failed to parse JSON config file: {file_path}", exc_info=True)
        return {}
    except Exception:
        logger.error(f"Unknown error loading config file: {file_path}", exc_info=True)
        return {}
