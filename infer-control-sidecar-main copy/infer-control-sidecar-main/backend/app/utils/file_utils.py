import asyncio
import aiofiles
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)


async def write_command_to_volume(command: str, shared_path: str, filename: str = "start_command.sh") -> bool:
    """
    将启动命令写入共享卷

    Args:
        command: 启动命令
        shared_path: 共享卷路径
        filename: 命令文件名

    Returns:
        bool: 是否写入成功
    """
    try:
        # 确保共享卷目录存在
        shared_dir = Path(shared_path)
        shared_dir.mkdir(parents=True, exist_ok=True)

        command_file = shared_dir / filename

        # 写入命令文件
        async with aiofiles.open(command_file, mode='w', encoding='utf-8') as f:
            await f.write(command)

        logger.info(f"Command written to shared volume: {command_file}")
        return True

    except Exception as e:
        logger.error(f"Failed to write command to shared volume: {e}")
        return False


async def read_status_from_volume(shared_path: str, filename: str = "engine_status.txt") -> Optional[str]:
    """
    从共享卷读取引擎状态

    Args:
        shared_path: 共享卷路径
        filename: 状态文件名

    Returns:
        Optional[str]: 引擎状态
    """
    try:
        status_file = Path(shared_path) / filename

        if not status_file.exists():
            return None

        async with aiofiles.open(status_file, mode='r', encoding='utf-8') as f:
            status = await f.read()

        return status.strip()

    except Exception as e:
        logger.error(f"Failed to read status from shared volume: {e}")
        return None


async def write_status_to_volume(status: str, shared_path: str, filename: str = "wings_status.txt") -> bool:
    """
    将wings-infer状态写入共享卷

    Args:
        status: 状态信息
        shared_path: 共享卷路径
        filename: 状态文件名

    Returns:
        bool: 是否写入成功
    """
    try:
        shared_dir = Path(shared_path)
        shared_dir.mkdir(parents=True, exist_ok=True)

        status_file = shared_dir / filename

        async with aiofiles.open(status_file, mode='w', encoding='utf-8') as f:
            await f.write(status)

        logger.info(f"Wings status written to shared volume: {status_file}")
        return True

    except Exception as e:
        logger.error(f"Failed to write wings status to shared volume: {e}")
        return False


async def write_patch_options_to_volume(patch_options: str, shared_path: str, filename: str = "engine_patch_options.env") -> bool:
    """
    将引擎补丁选项写入共享卷

    Args:
        patch_options: 补丁选项（JSON 格式）
        shared_path: 共享卷路径
        filename: 环境变量文件名

    Returns:
        bool: 是否写入成功
    """
    try:
        # 确保共享卷目录存在
        shared_dir = Path(shared_path)
        shared_dir.mkdir(parents=True, exist_ok=True)

        patch_options_file = shared_dir / filename

        # 写入环境变量文件
        env_content = f"WINGS_ENGINE_PATCH_OPTIONS={patch_options}"
        async with aiofiles.open(patch_options_file, mode='w', encoding='utf-8') as f:
            await f.write(env_content)

        logger.info(f"Patch options written to shared volume: {patch_options_file}")
        logger.info(f"Patch options: {patch_options}")
        return True

    except Exception as e:
        logger.error(f"Failed to write patch options to shared volume: {e}")
        return False