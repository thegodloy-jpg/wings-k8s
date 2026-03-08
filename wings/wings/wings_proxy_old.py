# /opt/wings/wings_proxy.py
import os
import uvicorn
from proxy.settings import args  # 保持原有用法


def main() -> None:
    # —— 计算实际将要使用的 worker 数（与你现有逻辑保持一致）——
    workers = max(1, (os.cpu_count() or 2) - 1)

    # —— 在启动前设置本方案需要的环境变量（若外部已设则不覆盖）——
    os.environ.setdefault("KEEP_ACCESS_LOG", "0")        # 关闭 uvicorn.access 噪声（需要保留就改成 "1"）
    os.environ.setdefault("LOG_INFO_SPEAKERS", "1")      # 允许 1 个 worker 打 INFO（可改成 "2","3"...）
    os.environ.setdefault("LOG_WORKER_COUNT", str(workers))  # 与实际 workers 保持一致

    # 兼容常见变量（若你有外部工具依赖这些，可一并补上；已设则不覆盖）
    os.environ.setdefault("WEB_CONCURRENCY", str(workers))
    os.environ.setdefault("UVICORN_WORKERS", str(workers))

    uvicorn.run(
        "proxy.gateway:app",
        host=args.host,
        port=args.port,
        log_level="info",
        loop="uvloop",
        http="httptools",
        workers=workers,
    )

if __name__ == "__main__":
    main()
