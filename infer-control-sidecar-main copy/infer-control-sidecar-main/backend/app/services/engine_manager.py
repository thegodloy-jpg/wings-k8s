import asyncio
import logging
import httpx
from app.services.command_builder import CommandBuilder
from app.utils.file_utils import write_command_to_volume
from app.config.settings import settings

logger = logging.getLogger(__name__)


class EngineManager:
    """引擎管理器，负责启动和监控引擎容器"""

    def __init__(self):
        self.engine_started = False
        self.startup_task = None

    async def start_engine(self) -> bool:
        """
        启动引擎服务

        通过将启动命令写入共享卷，让引擎容器读取并执行

        Returns:
            bool: 是否成功启动
        """
        try:
            logger.info("Starting engine service...")

            # 1. 构建启动命令（包含环境变量）
            command = CommandBuilder.build_command()
            logger.info(f"Built engine command: {command}")

            # 2. 将命令写入共享卷
            success = await write_command_to_volume(
                command=command,
                shared_path=settings.SHARED_VOLUME_PATH,
                filename="start_command.sh"
            )

            if not success:
                logger.error("Failed to write command to shared volume")
                return False

            logger.info("Engine command written to shared volume successfully")
            return True

        except Exception as e:
            logger.error(f"Error starting engine: {e}")
            return False

    async def wait_for_engine_ready(self) -> bool:
        """
        等待引擎服务就绪

        通过HTTP健康检查来判断引擎是否启动

        Returns:
            bool: 引擎是否就绪
        """
        logger.info("Waiting for engine to be ready...")

        timeout = settings.HEALTH_CHECK_TIMEOUT
        interval = settings.HEALTH_CHECK_INTERVAL
        elapsed = 0

        engine_url = f"http://127.0.0.1:{settings.ENGINE_PORT}/health"

        while elapsed < timeout:
            try:
                # 使用HTTP健康检查
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(engine_url)

                    if response.status_code == 200:
                        logger.info("Engine is ready!")
                        self.engine_started = True
                        return True
                    else:
                        logger.info(f"Waiting for engine... HTTP status: {response.status_code}")

            except httpx.ConnectError as e:
                logger.info(f"Waiting for engine... Connection error: {type(e).__name__}")
            except httpx.TimeoutException:
                logger.info(f"Waiting for engine... Timeout")
            except Exception as e:
                logger.warning(f"Error checking engine health: {e}")

            await asyncio.sleep(interval)
            elapsed += interval

        logger.error(f"Engine startup timeout after {timeout} seconds")
        return False

    async def start(self) -> bool:
        """
        启动引擎管理器

        Returns:
            bool: 是否成功启动
        """
        try:
            # 1. 启动引擎
            if not await self.start_engine():
                return False

            # 2. 等待引擎就绪
            if not await self.wait_for_engine_ready():
                return False

            logger.info("Engine manager started successfully")
            return True

        except Exception as e:
            logger.error(f"Error in engine manager startup: {e}")
            return False

    def is_engine_ready(self) -> bool:
        """
        检查引擎是否就绪

        Returns:
            bool: 引擎是否就绪
        """
        return self.engine_started

    async def stop(self):
        """
        停止引擎管理器
        """
        logger.info("Stopping engine manager...")
        if self.startup_task:
            self.startup_task.cancel()
        self.engine_started = False


# 创建全局engine_manager实例
engine_manager = EngineManager()