# =============================================================================
# 文件: utils/wings_file_utils.py
# 用途: wings 项目文件工具兼容副本，迁移期间保留
# 状态: 临时兼容模块，閿止扩展
#
# 功能概述:
#   提供与 file_utils.py 一致的工具函数，迁移时复制自 wings 代码库。
#   未来应将调用收敛到单一 file_utils 实现。
#
# Sidecar 架构契约:
#   - 优先使用主 file_utils 实现
#   - 避免重复模块中产生行为差异
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
    """计算目录所有文件的总大小（字节）。

    递归遍历目录，跳过符号链接。

    Args:
        path: 目录路径

    Returns:
        int: 文件总大小（字节）
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
    """安全且原子地将内容写入文件。

    使用低级 I/O 和 os.fdatasync 确保内容落盘;
    支持以 JSON 格式序列化。

    Args:
        file_path: 目标文件路径
        content:   待写内容（字符串或可序列化对象）
        is_json:   若 True 则将 content 以 JSON 写入
        flags:     open 标志（默认 O_WRONLY|O_CREAT|O_TRUNC）
        modes:     文件权限（默认 600）

    Returns:
        bool: 成功返回 True，失败返回 False
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
    """检查文件权限是否为 640。

    Args:
        file_path (str): 文件路径

    Returns:
        bool: 权限正确返回 True，否则 False

    Raises:
        FileNotFoundError: 文件不存在
        PermissionError:   无访问权限
        OSError:           系统调用失败
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
        logger.error(f"Error: File '{file_path}' does not exist")
        raise
    except PermissionError:
        logger.error(f"Error: No permission to access file '{file_path}'")
        raise
    except OSError as e:
        logger.error(f"OS error occurred while checking permissions: {e}")
        raise OSError(f"Failed to check file permissions: {e}") from e


def check_torch_dtype(json_file_path):
    """检查 config.json 中 torch_dtype 是否为 bfloat16（Ascend310 不支持）。

    Args:
        json_file_path (str): 模型配置 JSON 文件路径

    Returns:
        bool: 检查通过返回 True

    Raises:
        ValueError:        torch_dtype 为 bfloat16 时抛出
        FileNotFoundError: 文件不存在
        json.JSONDecodeError: JSON 解析失败
        IOError:           读取失败
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
    """加载 JSON 配置文件。

    Args:
        file_path (str): JSON 文件路径

    Returns:
        Dict[str, Any]: 解析后的配置字典（文件不存在时返回空字典）
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
