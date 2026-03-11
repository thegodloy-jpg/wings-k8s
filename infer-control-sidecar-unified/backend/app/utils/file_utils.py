# =============================================================================
# 文件: utils/file_utils.py
# 用途: 安全文件 I/O 辅助函数，用于共享卷工件和配置文件操作
# 状态: 活跃，复用自 wings 项目的文件工具模块
#
# 功能概述:
#   本模块提供安全的文件操作函数，主要功能:
#   - get_directory_size()    : 计算目录总大小（字节）
#   - safe_write_file()       : 安全写入文件（支持 JSON/文本，指定权限）
#   - check_permission_640()  : 检查文件权限是否为 640
#   - check_torch_dtype()     : 检查模型 config.json 中 torch_dtype 是否支持
#   - load_json_config()      : 安全加载 JSON 配置文件
#
# Sidecar 架构契约:
#   - 保持命令/状态工件写入的可靠性
#   - 权限和 JSON 写入语义保持稳定
#
# =============================================================================
# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.

import os
import stat
import json
from typing import Any, Dict
import logging

logger = logging.getLogger(__name__)

INDENT = 4


def get_directory_size(path: str) -> int:
    """计算目录及其子文件的总大小（字节）。

    递归遍历目录下所有文件，累加文件大小（不包括符号链接）。

    Args:
        path: 目录路径

    Returns:
        int: 目录总大小（字节）
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
    """安全写入文件，支持 JSON 序列化和文本写入。

    使用 os.open() + os.fdopen() 确保文件权限在创建时即被设置，
    避免竞态条件下的权限空窗期。

    Args:
        file_path: 目标文件路径
        content:   要写入的内容（字符串或可 JSON 序列化对象）
        is_json:   是否以 JSON 格式写入
        flags:     文件打开标志（默认 O_WRONLY|O_CREAT|O_TRUNC）
        modes:     文件权限模式（默认 600）

    Returns:
        bool: 写入成功返回 True，失败返回 False
    """
    try:
        with os.fdopen(os.open(file_path, flags, modes), 'w') as f:
            if is_json:
                json.dump(content, f, indent=INDENT)
            else:
                f.write(content)
        return True
    except Exception as e:
        logger.error("Failed to write file %s: %s", file_path, e, exc_info=True)
        return False


def check_permission_640(file_path):
    """
    640

    :
        file_path (str):

    :
        bool: TrueFalse

    :
        FileNotFoundError:
        PermissionError:
        OSError:
    """
    try:
        #
        stat_info = os.stat(file_path)

        # 0o777
        file_permissions = stat_info.st_mode & 0o777

        # 6400o640
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
        logger.error("Error: File '%s' does not exist", file_path)
        raise
    except PermissionError:
        logger.error("Error: No permission to access file '%s'", file_path)
        raise
    except OSError as e:
        logger.error("OS error occurred while checking permissions: %s", e)
        raise OSError(f"Failed to check file permissions: {e}") from e


def check_torch_dtype(json_file_path):
    """
    JSONtorch_dtype"bfloat16"

    :
        json_file_path (str): JSON

    :
        bool: True

    :
        ValueError: torch_dtype"bfloat16"
        FileNotFoundError:
        json.JSONDecodeError: JSON
        IOError: I/O
    """
    try:
        with open(json_file_path, 'r') as f:
            data = json.load(f)

        # torch_dtype
        if data.get('torch_dtype') == 'bfloat16':
            error_msg = "Ascend310 does not support bfloat16. Please modify the config.json " \
            "under the model weight path and change torch_dtype to float16"
            raise ValueError(error_msg)

        return True

    except FileNotFoundError:
        logger.error("The file %s was not found", json_file_path)
        raise
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON format in file: %s", e)
        raise
    except IOError as e:
        logger.error("An error occurred while reading the file: %s", e)
        raise
    except Exception as e:
        logger.error("Unexpected error checking torch dtype: %s", e)
        raise RuntimeError(f"Failed to check torch dtype: {e}") from e


def load_json_config(file_path: str) -> Dict[str, Any]:
    """
     JSON

    Args:
        file_path (str): JSON

    Returns:
        Dict[str, Any]:
    """
    if not os.path.exists(file_path):
        logger.warning("Config file not found: %s", file_path)
        return {}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
            logger.info("Successfully loaded config file: %s", file_path)
            return config_data
    except json.JSONDecodeError:
        logger.error("Failed to parse JSON config file: %s", file_path, exc_info=True)
        return {}
    except Exception:
        logger.error("Unknown error loading config file: %s", file_path, exc_info=True)
        return {}
