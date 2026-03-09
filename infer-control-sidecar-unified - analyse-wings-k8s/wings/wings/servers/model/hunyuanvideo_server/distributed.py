# -*- coding: utf-8 -*-
"""
分布式辅助：统一封装 barrier / broadcast / all_ready，以及非0 rank 守护线程。
关键点：
- 所有 collective 发生前，线程级绑定设备（按 LOCAL_RANK），支持 NPU/CUDA
- 守护线程的第一拍是等待 rank0 的 broadcast；拿到指令后再 barrier → infer → barrier
"""
import os
import time
import threading
from typing import Optional, Tuple

import torch
import torch.distributed as dist
from loguru import logger

# 全局状态
_DIST_DAEMON_ALIVE = False
_DIST_DAEMON_THREAD: Optional[threading.Thread] = None

# -------------------------
# 设备选择 / 线程绑定（NPU + CUDA 通用实现，保持原函数名）
# -------------------------
def _local_cuda_index() -> Optional[int]:
    """为兼容旧调用名，内部扩展为通用 LOCAL_RANK 获取；若无法获取返回 None。"""
    try:
        return int(os.getenv("LOCAL_RANK", "0"))
    except Exception:
        return None


def _is_npu_available() -> bool:
    return hasattr(torch, "npu") and torch.npu.is_available()


def _ensure_thread_cuda_device(tag: str = ""):
    """
    兼容旧名字，但内部已支持 NPU：
    - NPU 优先：torch.npu.set_device(f"npu:{LOCAL_RANK}")
    - 否则 CUDA：torch.cuda.set_device(LOCAL_RANK)
    - 若都不可用则跳过
    """
    idx = _local_cuda_index()
    if idx is None:
        return

    # NPU 优先
    try:
        if _is_npu_available():
            try:
                cur = torch.npu.current_device()
            except Exception:
                cur = None
            if cur != idx:
                torch.npu.set_device(f"npu:{idx}")
                if tag:
                    logger.debug(f"[dist] set npu:{idx} in {tag}")
                else:
                    logger.debug(f"[dist] set npu:{idx}")
            return
    except Exception as e:
        logger.warning(f"[dist] set npu:{idx} failed: {e}")

    # CUDA 次之
    if torch.cuda.is_available():
        try:
            cur = torch.cuda.current_device()
        except Exception:
            cur = None
        if cur != idx:
            try:
                torch.cuda.set_device(idx)
                if tag:
                    logger.debug(f"[dist] set cuda:{idx} in {tag}")
                else:
                    logger.debug(f"[dist] set cuda:{idx}")
            except Exception as e:
                logger.warning(f"[dist] set cuda:{idx} failed: {e}")


def _pick_dist_device() -> torch.device:
    """选择分布式张量放置设备：NPU > CUDA > CPU"""
    idx = _local_cuda_index() or 0
    if _is_npu_available():
        return torch.device(f"npu:{idx}")
    if torch.cuda.is_available():
        return torch.device(f"cuda:{idx}")
    return torch.device("cpu")


# -------------------------
# 基础 API（供 app 侧调用）
# -------------------------
def get_dist_info() -> Tuple[int, int]:
    if dist.is_initialized():
        try:
            return dist.get_rank(), dist.get_world_size()
        except Exception:
            pass
    return int(os.getenv("RANK", "0")), int(os.getenv("WORLD_SIZE", "1"))


def dist_barrier():
    """
    CUDA(NCCL): 使用 device_ids=[LOCAL_RANK]
    NPU(HCCL): 直接 dist.barrier()（HCCL 不接受 device_ids）
    """
    if not dist.is_initialized():
        return
    try:
        _ensure_thread_cuda_device("barrier")
        if _is_npu_available():
            dist.barrier()
        else:
            idx = _local_cuda_index()
            if idx is not None and torch.cuda.is_available():
                dist.barrier(device_ids=[idx])
            else:
                dist.barrier()
    except Exception as e:
        logger.warning(f"[dist] barrier failed: {e}")


def dist_all_ready(local_ok: bool, world_size: int) -> bool:
    """
    使用分布式 all_reduce 统计就绪数；NPU/CUDA 上张量放在对应设备，避免 CPU fallback。
    """
    if not dist.is_initialized():
        return bool(local_ok)

    _ensure_thread_cuda_device("all_ready")
    device = _pick_dist_device()
    t = torch.tensor([1 if local_ok else 0], device=device)
    try:
        dist.all_reduce(t, op=dist.ReduceOp.SUM)
        return int(t.item()) == world_size
    except Exception as e:
        logger.warning(f"[dist] all_reduce failed: {e}")
        return bool(local_ok)


def broadcast_object_from_rank0(obj: dict | None):
    if not dist.is_initialized():
        logger.debug("[dist] broadcast skipped (process_group not initialized)")
        return
    _ensure_thread_cuda_device("broadcast")
    packet = [obj]
    dist.broadcast_object_list(packet, src=0)


# -------------------------
# 非 0 rank 守护线程
# -------------------------
def _loop(model, save_dir, parsed_args):
    rank, world = get_dist_info()
    logger.info(f"[dist-daemon] rank={rank} started")

    while _DIST_DAEMON_ALIVE:
        try:
            if not dist.is_initialized():
                time.sleep(0.1)
                continue

            # ★ 首次等待广播前，必须把本线程绑定到 LOCAL_RANK 对应设备
            _ensure_thread_cuda_device("daemon/wait-bcast")

            recv = [None]
            # 阻塞领取 rank0 的广播命令（第一拍和 rank0 对齐：都是 broadcast）
            dist.broadcast_object_list(recv, src=0)
            cmd = recv[0] or {}
            if not isinstance(cmd, dict):
                continue

            op = cmd.get("cmd", "")
            if op == "shutdown":
                logger.info(f"[dist-daemon] rank={rank} shutdown")
                break

            if op == "generate":
                payload = cmd.get("payload", {}) or {}

                # 第二拍：全员 barrier（与 rank0 对齐）
                dist_barrier()
                try:
                    _ensure_thread_cuda_device("daemon/generate")

                    # 避免循环导入：在用到时再引入
                    from .app import generate_video_files  # type: ignore

                    # 非 0 rank 内部会触发推理，但 generate_video_files 会在分布式下避免写盘
                    generate_video_files(model, save_dir, **payload)

                except Exception as e:
                    logger.exception(f"[dist-daemon] rank={rank} generate failed: {e}")
                finally:
                    # 第四拍：统一收尾 barrier
                    dist_barrier()

        except Exception as e:
            logger.warning(f"[dist-daemon] rank={rank} loop err: {e}")
            time.sleep(0.2)

    logger.info(f"[dist-daemon] rank={rank} exit")


def start_daemon_thread(model, save_dir, parsed_args):
    global _DIST_DAEMON_ALIVE, _DIST_DAEMON_THREAD
    rank, world = get_dist_info()
    if world <= 1 or rank == 0:
        return
    if not dist.is_initialized():
        logger.warning("[dist] start_daemon_thread called before process_group init; skip starting daemon.")
        return

    _DIST_DAEMON_ALIVE = True
    _DIST_DAEMON_THREAD = threading.Thread(
        target=_loop, args=(model, save_dir, parsed_args),
        name="dist-daemon", daemon=True
    )
    _DIST_DAEMON_THREAD.start()


def stop_daemon_thread():
    global _DIST_DAEMON_ALIVE
    _DIST_DAEMON_ALIVE = False