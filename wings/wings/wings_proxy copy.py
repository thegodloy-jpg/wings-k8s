# -*- coding: utf-8 -*-
"""
轻量代理服务启动脚本
"""

import uvicorn
from wings.proxy.simple_proxy import app
import os
import multiprocessing
import uvicorn

from wings.proxy.settings import args  # 保持原有用法



def run_health_service():
    """运行健康检查服务"""
    from wings.proxy.health_service import app as health_app
    from wings.proxy.health_service import HEALTH_SERVICE_PORT
    uvicorn.run(health_app, host="0.0.0.0", port=HEALTH_SERVICE_PORT, log_level="info")


if __name__ == "__main__":
    health_process = multiprocessing.Process(target=run_health_service)
    health_process.start()
    workers = max(1, (os.cpu_count() or 2) - 1)
    try:
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=18000,
            log_level="info",
            loop="uvloop",
            http="httptools",
            workers=workers,
        )
    finally:
        # 确保健康检查服务进程也被终止
        if health_process.is_alive():
            health_process.terminate()
            health_process.join()