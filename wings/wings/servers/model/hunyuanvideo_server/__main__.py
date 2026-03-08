# -*- coding: utf-8 -*-
"""
__main__.py / main.py
- rank0: 启动 Uvicorn (HTTP)
- 非 rank0: 不启 HTTP，进入 app.lifespan() 完整初始化并启动分布式守护线程，然后常驻等待
- 两条分支都先绑定设备到 LOCAL_RANK，避免 NCCL/HCCL 初始化卡住
"""
import os
import asyncio
import signal
import traceback

import torch
import uvicorn

# 可选：如需在 NPU 上禁用 JIT 编译/内部格式，可在外部环境统一配置
try:
    import torch_npu  # type: ignore
    torch_npu.npu.set_compile_mode(jit_compile=False)
    torch.npu.config.allow_internal_format = False
except Exception:
    pass


def _bind_cuda_to_local_rank():
    """为当前进程绑定 NPU/CUDA 设备（兼容旧名）。"""
    try:
        lr = int(os.getenv("LOCAL_RANK", "0"))
    except Exception:
        lr = 0

    # NPU 优先
    try:
        if hasattr(torch, "npu") and torch.npu.is_available():
            try:
                cur = torch.npu.current_device()
            except Exception:
                cur = None
            if cur != lr:
                torch.npu.set_device(f"npu:{lr}")
                print(f"[main] torch.npu.set_device({lr})")
            return
    except Exception as e:
        print(f"[main] set npu:{lr} failed: {e!r}")

    # CUDA 次之
    if torch.cuda.is_available():
        try:
            cur = torch.cuda.current_device()
        except Exception:
            cur = None
        if cur != lr:
            try:
                torch.cuda.set_device(lr)
                print(f"[main] torch.cuda.set_device({lr})")
            except Exception as e:
                print(f"[main] set cuda:{lr} failed: {e!r}")


async def _worker_only_main():
    """
    非 rank0 进程：不启 HTTP；直接跑 app 的 lifespan()，完成初始化与守护线程启动后常驻。
    """
    from wings.servers.model.hunyuanvideo_server.app import wings_engine, lifespan

    _bind_cuda_to_local_rank()

    stop = asyncio.Event()

    def _set_stop():
        try:
            stop.set()
        except Exception:
            pass

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _set_stop)
        except NotImplementedError:
            pass

    print("[worker] standby; waiting for shutdown broadcast")
    try:
        async with lifespan(wings_engine):
            await stop.wait()
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    host = os.getenv("SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("SERVER_PORT", "8081"))

    WORLD_SIZE = int(os.getenv("WORLD_SIZE", "1"))
    RANK = int(os.getenv("RANK", "0"))

    try:
        if WORLD_SIZE > 1 and RANK != 0:
            asyncio.run(_worker_only_main())
        else:
            _bind_cuda_to_local_rank()
            from wings.servers.model.hunyuanvideo_server.app import wings_engine
            uvicorn.run(
                wings_engine,
                host=host,
                port=port,
                reload=False,
                workers=1,          # 必须为 1，避免多进程重复初始化分布式
                log_level="info",
            )
    except SystemExit as e:
        print("[fatal] SystemExit:", e)
        traceback.print_exc()
        raise
    except Exception as e:
        print("[fatal] unhandled exception:", repr(e))
        traceback.print_exc()
        raise