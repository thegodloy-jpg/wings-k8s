import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.services.engine_manager import engine_manager
from app.config.settings import settings

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动阶段
    logger.info("=" * 50)
    logger.info("Starting Wings-Infer...")
    logger.info("=" * 50)

    # 启动引擎管理器
    try:
        logger.info("Initializing engine manager...")
        success = await engine_manager.start()

        if not success:
            logger.error("Failed to start engine manager")
            # 可以选择继续运行或退出，这里选择继续运行让用户查看状态

    except Exception as e:
        logger.error(f"Error during startup: {e}")

    logger.info("Wings-Infer started successfully")
    logger.info(f"Listening on port: {settings.WINGS_PORT}")

    # 获取并打印访问地址
    cluster_ip = settings.SERVICE_CLUSTER_IP
    node_ip = settings.NODE_IP
    node_port = settings.NODE_PORT
    port = settings.WINGS_PORT

    logger.info("=" * 50)
    logger.info("ACCESS INFORMATION:")
    logger.info("=" * 50)
    logger.info(f"Service Cluster-IP: {cluster_ip}:{port}")
    logger.info(f"NodePort: {node_ip}:{node_port}")
    logger.info(f"Health Check: http://{cluster_ip}:{port}/health")
    logger.info(f"Docs API:    http://{cluster_ip}:{port}/docs")
    logger.info("=" * 50)
    logger.info(f"OpenAI Compatible Endpoint:")
    logger.info(f"  POST {cluster_ip}:{port}/v1/chat/completions")
    logger.info(f"  POST {cluster_ip}:{port}/v1/completions")
    logger.info("=" * 50)

    yield

    # 关闭阶段
    logger.info("Shutting down Wings-Infer...")
    await engine_manager.stop()
    logger.info("Wings-Infer shutdown complete")


# 创建FastAPI应用
app = FastAPI(
    title="Wings-Infer API",
    description="Unified inference service for vLLM and SGLang engines",
    version="1.0.0",
    lifespan=lifespan
)

# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(router)


@app.get("/")
async def root():
    """根路径"""
    return {
        "message": "Wings-Infer is running",
        "docs": "/docs",
        "health": "/health"
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.WINGS_PORT,
        reload=False,
        log_level="info"
    )