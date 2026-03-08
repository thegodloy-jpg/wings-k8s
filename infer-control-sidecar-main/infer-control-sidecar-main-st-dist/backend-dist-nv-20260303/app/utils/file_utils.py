# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: utils/file_utils.py
# Purpose: Safe file IO helpers for shared-volume artifacts and general config file operations.
# Status: Active reused utility.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Preserve reliability of command/status artifact writes.
# - Keep permission and JSON write semantics stable.
# -----------------------------------------------------------------------------
# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.

import os
import stat
import json
from typing import Any, Dict
import logging

logger = logging.getLogger(__name__)

INDENT = 4


def get_directory_size(path: str) -> int:
    """()

    Args:
        path:

    Returns:
        int: ()
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
    JSON

    Args:
        file_path:
        content:
        is_json: JSON
        flags: O_WRONLY|O_CREAT
        modes: (600)

    Returns:
        bool:
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
     JSON

    Args:
        file_path (str): JSON

    Returns:
        Dict[str, Any]:
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
