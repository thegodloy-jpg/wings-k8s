#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一版（GPU + Ascend NPU） - 使用 hyvideo 内部的分布式逻辑
要点：
- 任务执行时序在所有 rank 上对齐：Broadcast → Barrier → Infer → Barrier
- 删掉 rank0 上“广播前的 barrier”，避免和非0 rank 的“等待广播”打架
"""
import uuid
import os
import json
import asyncio
import traceback
from pathlib import Path
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Any, Dict, Callable, Tuple, List
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
import subprocess

from loguru import logger
from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import JSONResponse

# 统一从 env 读 rank/world_size
from . import distributed as D

RANK = int(os.getenv("RANK", "0"))
WORLD_SIZE = int(os.getenv("WORLD_SIZE", "1"))
IS_DIST = WORLD_SIZE > 1
ALL_READY = False

import torch
import torch.distributed as dist
from hyvideo.config import parse_args

if TYPE_CHECKING:
    from hyvideo.inference import HunyuanVideoSampler as SamplerT
else:
    SamplerT = Any

from hyvideo.utils.file_utils import save_videos_grid

from .state import SERVER_CLI, USE_NPU, USE_CUDA, DEVICE_STR
from .core import (
    GenerateRequestPublic, SubmitResponse, StatusResponse,
    _ensure_valid_t, _safe_prompt, _serialize_status_response,
    _ensure_ckpts_layout, _get_wings_root_dir,
    _normalize_hyvideo_vae_map, _patch_hyvideo_load_vae
)

# 全局
#MODEL: Optional[HunyuanVideoSampler] = None
MODEL: Optional[SamplerT] = None
SAVE_DIR: Optional[Path] = None
PARSED_ARGS: Optional[Any] = None
RESOLVED_MODEL_PATHS: Dict[str, str] = {}
TASKS: Dict[str, Dict[str, Any]] = {}
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "2"))
SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENCY)
QUEUE: "asyncio.Queue[Tuple[str, Dict[str, Any]]]" = asyncio.Queue()
EXECUTOR: Optional[ThreadPoolExecutor] = None


# ---- Warmup config (NPU only by default) ----
WARMUP_ENABLED = os.getenv("WARMUP_ENABLED", "auto")  # 'auto' | '1' | '0'
WARMUP_STEPS   = int(os.getenv("WARMUP_STEPS", "2"))
WARMUP_VSIZE   = os.getenv("WARMUP_VIDEO_SIZE", "1280x720")
WARMUP_T       = int(os.getenv("WARMUP_VIDEO_LENGTH", os.getenv("WARMUP_FRAMES", "25")))
WARMUP_PROMPT  = os.getenv("WARMUP_PROMPT", "warmup")
WARMUP_STRICT  = os.getenv("WARMUP_STRICT", "0").lower() in ("1", "true", "yes")
WARMUP_DONE    = False


# ---- Export / NPU stability (minimal knobs) ----
EXPORT_FPS   = int(os.getenv("EXPORT_FPS", "24"))  # 统一导出帧率，默认 24
FORCE_CFR    = os.getenv("FORCE_CFR", "1").lower() in ("1", "true", "yes")  # 导出后强制恒定帧率
ALIGN_T_MULT4= os.getenv("ALIGN_T_MULT4", "0").lower() in ("1", "true", "yes")  # T 轴对齐 4 的倍数
NPU_SAFEMODE = os.getenv("NPU_SAFEMODE", "0").lower() in ("1", "true", "yes")  # NPU 安全模式(收敛并行/缓存/量化)



def _is_warmup_enabled() -> bool:
    if not USE_NPU:
        return False
    v = str(WARMUP_ENABLED).lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no"):
        return False
    # auto: 启用（仅 NPU）
    return True


# ---------------- NPU 可选依赖懒加载 ----------------
def _mindiesd_load():
    try:
        from mindiesd import CacheConfig, CacheAgent, quantize  # type: ignore
        return CacheConfig, CacheAgent, quantize
    except Exception as load_error:
        logger.warning(f"[mindiesd] not available or partial: {load_error}")
        return None, None, None


# ---------------- 模型初始化与推理 ----------------

def _pick_npu_index(args) -> int:
    """
    选择当前进程要绑定的 NPU id。

    优先级：
    1. torchrun 下的 LOCAL_RANK（0,1,2,...，每个进程唯一）
    2. Ascend 常见变量 ASCEND_DEVICE_ID / DEVICE_ID / RANK_ID
    3. args.device_id
    4. 默认 0
    5. 如果设置了 ASCEND_RT_VISIBLE_DEVICES=0,1,... ，则把 LOCAL_RANK
       映射到这个列表里的实际物理卡号。
    """
    # 先拿 local_rank（torchrun 会设）
    local_rank_env = os.getenv("LOCAL_RANK")
    if local_rank_env is not None:
        local_rank = int(local_rank_env)
    else:
        local_rank = None

    # 如果有 ASCEND_RT_VISIBLE_DEVICES，就做一次映射
    visible = os.getenv("ASCEND_RT_VISIBLE_DEVICES")
    if visible is not None:
        dev_list = [int(x.strip()) for x in visible.split(",") if x.strip() != ""]
        if local_rank is not None and 0 <= local_rank < len(dev_list):
            return dev_list[local_rank]
        # fallback: 就用第一个
        if dev_list:
            return dev_list[0]

    # 没有 ASCEND_RT_VISIBLE_DEVICES，直接走优先级链
    for env_name in ["LOCAL_RANK", "ASCEND_DEVICE_ID", "DEVICE_ID", "RANK_ID"]:
        if os.getenv(env_name) is not None:
            return int(os.getenv(env_name))

    # 再不行就看 args 里有没有 device_id
    if hasattr(args, "device_id"):
        try:
            return int(getattr(args, "device_id"))
        except Exception:
            pass

    # 最后兜底
    return 0


def initialize_model(model_base_fallback: str) -> SamplerT:
    """
    ✅ NPU 多进程安全版初始化（仅改服务端，不改 hyvideo）
    - 每个 torchrun 子进程都会各跑一遍这个函数
    - 流程：
      1) 绑定本进程 NPU（按 LOCAL_RANK / ASCEND_RT_VISIBLE_DEVICES）
      2) 注入 transfer_to_npu shim
      3) 懒加载 hyvideo（此时已绑定到正确 NPU）
      4) 设置 args.device = f"npu:{idx}"，再 from_pretrained
      5) NPU 加速（mindiesd）可选
    """
    global PARSED_ARGS
    args = PARSED_ARGS or parse_args()
    PARSED_ARGS = args

    # 解析模型根目录
    models_root_path = Path(model_base_fallback or getattr(args, "model_base", ""))
    logger.info(f"[init] models_root_path = {models_root_path}")
    logger.info(f"[init] vae_path = {args}")

    if not models_root_path.exists():
        raise RuntimeError(f"[init] model root not found: {models_root_path}")

    # -------- 绑定本进程到 NPU（关键：发生在任何张量/模型创建之前）--------
    if USE_NPU:
        npu_idx = _pick_npu_index(args)  # 根据 LOCAL_RANK / ASCEND_RT_VISIBLE_DEVICES 等
        logger.info(f"[init] binding current process to NPU:{npu_idx}")

        try:
            # 绑定当前进程的默认设备
            torch.npu.set_device(f"npu:{npu_idx}")
            try:
                # 若可用，设置默认张量设备，确保后续未显式指定 device 的路径也落到本卡
                torch.set_default_device(f"npu:{npu_idx}")
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"[npu] set_device({npu_idx}) failed: {e}")

        # 确保 args 视图与实际一致（重要：必须是带 index 的设备字符串）
        try:
            setattr(args, "device_id", npu_idx)
        except Exception as e:
            logger.warning(f"[npu] failed to set args.device_id={npu_idx}: {e}")
        try:
            setattr(args, "device", f"npu:{npu_idx}")  # ★ 不要用 "npu"（无 index）
        except Exception as e:
            logger.warning(f"[npu] failed to set args.device='npu:{npu_idx}': {e}")

        # ★ 在绑定后立刻注入 transfer_to_npu 的 shim
        try:
            from torch_npu.contrib import transfer_to_npu  # noqa: F401
            logger.debug("[npu] transfer_to_npu shim enabled")
        except Exception as e:
            logger.warning(f"[npu] transfer_to_npu shim not enabled: {e}")

    if USE_CUDA:
        # （CUDA 分支保持你原有逻辑）
        try:
            os.chdir(Path(__file__).resolve().parent)
        except Exception as _e:
            logger.warning("[patch] chdir skipped: %s", _e)
        _normalize_hyvideo_vae_map(getattr(args, "model_base", models_root_path))
        _patch_hyvideo_load_vae()

    logger.info(f"[init] model base dir = {models_root_path}")
    logger.info(
        f"[init] args.device={getattr(args, 'device', None)} "
        f"args.device_id={getattr(args, 'device_id', None)}"
    )

        # ---- NPU 安全模式：收敛并行/缓存/量化到稳定基线（可用 env 关闭）----
    if USE_NPU and NPU_SAFEMODE:
        for k in ("ulysses_degree", "ring_degree"):
            if hasattr(args, k):
                try:
                    setattr(args, k, 1)
                except Exception:
                    pass
        # 关缓存/注意力缓存
        for k in ("use_cache", "use_cache_double", "use_attentioncache"):
            if hasattr(args, k):
                try:
                    setattr(args, k, False)
                except Exception:
                    pass
        # 关量化
        if hasattr(args, "quant_desc_path"):
            try:
                setattr(args, "quant_desc_path", None)
            except Exception:
                pass
        logger.info("[npu-safemode] ulysses=1, ring=1, caches=off, quant=off")



    # ★★★ 懒加载 hyvideo（确保发生在设备绑定与 shim 注入之后）
    try:
        from hyvideo.inference import HunyuanVideoSampler
    except Exception as e:
        logger.error(f"[init] import HunyuanVideoSampler failed: {e}")
        raise

    # -------- 真正构建 sampler（此时 hyvideo 会按当前默认 NPU + args.device 正确上卡）--------
    sampler = HunyuanVideoSampler.from_pretrained(models_root_path, args=args)

    # eval / no grad
    if hasattr(sampler, "eval"):
        sampler.eval()
    torch.set_grad_enabled(False)

    # -------- NPU专属加速（量化 / cache）（可选）--------
    if USE_NPU:
        _mindiesd_quant_and_cache(args, sampler)

    logger.info("[init] sampler ready on %s", getattr(args, "device", "unknown"))
    return sampler


def _mindiesd_quant_and_cache(args, sampler: SamplerT):
    if not USE_NPU:
        return
    cache_config, cache_agent, quantize = _mindiesd_load()
    tr = sampler.pipeline.transformer
    _apply_quantization(args, tr, quantize)
    _apply_dit_block_cache(args, sampler, cache_config, cache_agent)
    _apply_attention_cache(args, tr, cache_config, cache_agent)


def _apply_quantization(args, transformer, quantize_func):
    if not quantize_func or not getattr(args, "quant_desc_path", None):
        return
    try:
        logger.info("[quant] apply: %s", args.quant_desc_path)
        quantize_func(model=transformer, quant_des_path=args.quant_desc_path, use_nz=False)
        transformer.to(transformer.device)
        logger.info("[quant] done.")
    except Exception as e:
        logger.warning("[quant] failed: %s: %s", type(e).__name__, e)


def _apply_dit_block_cache(args, sampler, cache_config, cache_agent):
    if not cache_config or not cache_agent:
        return
    try:
        tr = sampler.pipeline.transformer
        if getattr(args, "use_cache", False):
            _setup_single_block_cache(args, tr, cache_config, cache_agent)
        if getattr(args, "use_cache_double", False):
            _setup_double_block_cache(args, tr, cache_config, cache_agent)
    except Exception as e:
        logger.warning("[dit-block-cache] skipped: %s", e)


def _setup_single_block_cache(args, transformer, cache_config, cache_agent):
    cfg_single = cache_config(
        method="dit_block_cache",
        blocks_count=len(transformer.single_blocks),
        steps_count=args.infer_steps,
        step_start=args.cache_start_steps,
        step_interval=args.cache_interval,
        step_end=args.infer_steps - 1,
        block_start=args.single_block_start,
        block_end=args.single_block_end,
    )
    transformer.cache_single = cache_agent(cfg_single)


def _setup_double_block_cache(args, transformer, cache_config, cache_agent):
    cfg_double = cache_config(
        method="dit_block_cache",
        blocks_count=len(transformer.double_blocks),
        steps_count=args.infer_steps,
        step_start=args.cache_start_steps,
        step_interval=args.cache_interval,
        step_end=args.infer_steps - 1,
        block_start=args.double_block_start,
        block_end=args.double_block_end,
    )
    transformer.cache_dual = cache_agent(cfg_double)


def _apply_attention_cache(args, transformer, cache_config, cache_agent):
    if not cache_config or not cache_agent:
        return
    try:
        cfg_d = cache_config(method="attention_cache",
                             blocks_count=len(transformer.double_blocks),
                             steps_count=args.infer_steps)
        cfg_s = cache_config(method="attention_cache",
                             blocks_count=len(transformer.single_blocks),
                             steps_count=args.infer_steps)
        cache_d = cache_agent(cfg_d)
        cache_s = cache_agent(cfg_s)
        for blk in transformer.double_blocks:
            blk.cache = cache_d
        for blk in transformer.single_blocks:
            blk.cache = cache_s
    except Exception as e:
        logger.warning("[attention-cache] skipped: %s", e)


def _npu_sync(phase: str) -> None:
    if not USE_NPU:
        return
    try:
        torch.npu.synchronize()
        logger.debug(f"[npu] {phase}-synchronize completed")
    except Exception as sync_error:
        logger.warning(f"[npu] {phase}-synchronize failed: {sync_error}")


def _do_warmup(sampler: SamplerT, args: Any) -> bool:
    """NPU 预热：一次极短推理，不保存结果。失败是否阻断由 WARMUP_STRICT 控制。"""
    if not _is_warmup_enabled():
        logger.info("[warmup] skipped (disabled)")
        return True

    try:
        # 形状与超参：尽量贴近线上默认，步数短
        vs = WARMUP_VSIZE
        t  = WARMUP_T or int(getattr(args, "video_length", 25))
        w, h = _parse_video_size(vs)
        guidance = float(getattr(args, "cfg_scale", 1.0))
        flow     = float(getattr(args, "flow_shift", 7.0))
        egs      = getattr(args, "embedded_cfg_scale", None)
        neg      = getattr(args, "neg_prompt", "")
        seed     = 42

        logger.info(f"[warmup] begin: size={vs}, T={t}, steps={WARMUP_STEPS}")
        _npu_sync("pre-warmup")
        _ = _run_inference(
            sampler,
            prompt=WARMUP_PROMPT,
            w=w, h=h,
            video_length=t,
            seed=seed,
            negative_prompt=neg,
            num_inference_steps=max(1, WARMUP_STEPS),
            guidance_scale=guidance,
            num_videos=1,
            flow_shift=flow,
            batch_size=1,
            embedded_guidance_scale=egs,
        )
        _npu_sync("post-warmup")
        logger.info("[warmup] ok")
        return True
    except Exception as e:
        logger.error(f"[warmup] failed: {type(e).__name__}: {e}")
        if WARMUP_STRICT:
            raise
        return False


def _cfr_rewrite_inplace(path: str, fps: int) -> None:
    """用 ffmpeg 原地重写时间戳，强制 CFR；失败则跳过（不影响主流程）。"""
    if not FORCE_CFR:
        return
    try:
        tmp = str(Path(path).with_suffix(".cfr.tmp.mp4"))
        cmd = [
            "ffmpeg", "-y", "-i", path,
            "-r", str(fps), "-vsync", "1",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            tmp
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.replace(tmp, path)
        logger.debug(f"[cfr] rewrite OK: {path} @ {fps}fps")
    except Exception as e:
        logger.warning(f"[cfr] rewrite skipped for {path}: {e}")


def _resolve_seed(seed: int) -> int:
    return int.from_bytes(os.urandom(4), "little") if seed == -1 else int(seed)


def _parse_video_size(video_size: str) -> "tuple[int, int]":
    w, h = map(int, video_size.split("x"))
    return w, h


def _run_inference(
    sampler,
    *,
    prompt: str,
    w: int,
    h: int,
    video_length: int,
    seed: int,
    negative_prompt: "str | None",
    num_inference_steps: int,
    guidance_scale: float,
    num_videos: int,
    flow_shift: float,
    batch_size: int,
    embedded_guidance_scale: "float | None",
):
    neg = negative_prompt or ""
    with torch.inference_mode():
        return sampler.predict(
            prompt=prompt,
            height=h,
            width=w,
            video_length=_ensure_valid_t(video_length),
            seed=seed,
            negative_prompt=neg,
            infer_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            num_videos_per_prompt=num_videos,
            flow_shift=flow_shift,
            batch_size=batch_size,
            embedded_guidance_scale=embedded_guidance_scale,
        )


def _save_outputs(
    outputs,
    *,
    prompt: str,
    save_dir: "Path",
    base_seed: int,
    progress_cb: "Callable[[int,int,str,int,int], None] | None",
) -> "tuple[list[str], list[int]]":
    samples = outputs["samples"]
    out_prompts = outputs.get("prompts") or [prompt]
    out_seeds = outputs.get("seeds") or []
    base_prompt = out_prompts[0]
    safe_prompt = _safe_prompt(base_prompt)

    ts = datetime.utcnow().strftime("%Y-%m-%d-%H-%M-%S")
    save_dir.mkdir(parents=True, exist_ok=True)

    video_paths: "list[str]" = []
    seeds: "list[int]" = []

    total = len(samples)
    saved = 0
    for i, tensor in enumerate(samples):
        s = out_seeds[i] if i < len(out_seeds) else (base_seed + i)
        fname = f"{ts}_seed{s}_{safe_prompt}.mp4"
        path = str((save_dir / fname).resolve())

        # 单样本写盘
        #save_videos_grid(tensor.unsqueeze(0), path, fps=24)
        save_videos_grid(tensor.unsqueeze(0), path, fps=EXPORT_FPS)
        if USE_NPU:
            _cfr_rewrite_inplace(path, EXPORT_FPS)


        saved += 1
        video_paths.append(path)
        seeds.append(s)

        if progress_cb:
            try:
                progress_cb(saved, total, path, i, s)
            except Exception as progress_cb_error:
                logger.warning(f"[progress_cb] ignored error: {progress_cb_error}")

    return video_paths, seeds


def _select_batch_size(num_videos: int) -> int:
    if num_videos < 1:
        return 1
    if USE_NPU:
        return int(getattr(PARSED_ARGS, "batch_size", 1))
    return 1


def _align_video_length_4k_plus_1(t: int) -> int:
    if t <= 0:
        return 1
    r = (t - 1) % 4
    if r == 0:
        return t
    lower = ((t - 1) // 4) * 4 + 1      # 向下最近的 4k+1
    upper = lower + 4                   # 向上最近的 4k+1
    # 选最近；等距时优先向上，避免缩短视频
    return upper if (t - lower) > (upper - t) else lower


def generate_video_files(
    sampler: SamplerT,
    save_dir: "Path",
    *,
    prompt: str,
    video_size: str,
    video_length: int,
    seed: int,
    num_inference_steps: int,
    guidance_scale: float,
    flow_shift: float,
    embedded_guidance_scale: "float | None",
    num_videos: int,
    negative_prompt: "str | None",
    progress_cb: "Callable[[int, int, str, int, int], None] | None" = None,
) -> "tuple[list[str], list[int]]":
    _seed = _resolve_seed(seed)
    w, h = _parse_video_size(video_size)
    batch_size = _select_batch_size(num_videos)

    logger.info(
        f"[infer] dev={DEVICE_STR} {w}x{h}, T={video_length}, steps={num_inference_steps}, "
        f"seed={_seed}, gs={guidance_scale}, flow={flow_shift}, egs={embedded_guidance_scale}, "
        f"num_videos={num_videos}, batch_size={batch_size}"
    )


    # 仅做 4k+1 对齐（契合 hyvideo 约束：video_length-1 ≡ 0 (mod 4)）
    orig_T = video_length
    video_length = _align_video_length_4k_plus_1(video_length)
    if video_length != orig_T:
        logger.info(f"[infer] align T: {orig_T} -> {video_length} (4k+1)")

    # 保险：在真正调用前做显式校验（所有 rank 都会打印相同的最终 T）
    if (video_length - 1) % 4 != 0:
        raise ValueError(f"[infer] illegal T after align: {video_length}; expect 4k+1 (e.g. 25, 69, 129)")



    _npu_sync("pre")
    outputs = _run_inference(
        sampler,
        prompt=prompt,
        w=w,
        h=h,
        video_length=video_length,
        seed=_seed,
        negative_prompt=negative_prompt,
        num_inference_steps=num_inference_steps,
        guidance_scale=guidance_scale,
        num_videos=num_videos,
        flow_shift=flow_shift,
        batch_size=batch_size,
        embedded_guidance_scale=embedded_guidance_scale,
    )

    # 分布式下非 0 rank 不写盘
    if IS_DIST and RANK != 0:
        _npu_sync("post")
        return [], []

    video_paths, seeds = _save_outputs(
        outputs,
        prompt=prompt,
        save_dir=save_dir,
        base_seed=_seed,
        progress_cb=progress_cb,
    )
    _npu_sync("post")
    return video_paths, seeds


# ---------------- Worker 与任务管理 ----------------
async def process_task(task_id: str, payload: Dict[str, Any]):
    """
    分布式任务四拍：
    1) rank0：broadcast(generate, payload)
    2) 全体：barrier
    3) 全体：infer
    4) 全体：barrier
    """
    def _ensure_thread_cuda_device():
        """兼容旧名：内部支持 NPU + CUDA 的线程级设备绑定"""
        try:
            lr = int(os.getenv("LOCAL_RANK", "0"))
        except Exception:
            lr = 0

        # NPU 优先
        try:
            if hasattr(torch, "npu") and torch.npu.is_available():
                cur = torch.npu.current_device()
                if cur != lr:
                    torch.npu.set_device(f"npu:{lr}")
                    logger.debug(f"[dist] worker-thread set_device(npu:{lr})")
                return
        except Exception as e:
            logger.warning(f"[dist] worker-thread set npu:{lr} failed: {e}")

        # CUDA 次之
        if torch.cuda.is_available():
            try:
                cur = torch.cuda.current_device()
            except Exception:
                cur = None
            if cur != lr:
                try:
                    torch.cuda.set_device(lr)
                    logger.debug(f"[dist] worker-thread set_device(cuda:{lr})")
                except Exception as e:
                    logger.warning(f"[dist] worker-thread set_device(cuda:{lr}) failed: {e}")

    rec = TASKS.get(task_id)
    if not rec:
        return
    try:
        async with SEMAPHORE:
            rec["status"] = "running"
            rec["updated_at"] = datetime.utcnow().isoformat()
            logger.info(f"[process_task] {task_id} -> running")

            loop = asyncio.get_running_loop()

            def _progress_cb(saved: int, total: int, path: str, idx: int, s: int):
                if IS_DIST and RANK != 0:
                    return
                url = path
                def _apply():
                    r = TASKS.get(task_id)
                    if not r:
                        return
                    r["results"].append({"idx": idx, "seed": s, "path": path, "url": url})
                    r["progress"]["saved"] = saved
                    try:
                        r["progress"]["percent"] = int(saved * 100 / max(total, 1))
                    except Exception:
                        r["progress"]["percent"] = 0
                    r["updated_at"] = datetime.utcnow().isoformat()
                loop.call_soon_threadsafe(_apply)

            def _run_blocking():
                _ensure_thread_cuda_device()

                if IS_DIST and dist.is_initialized():
                    # ★ 第一拍：rank0 先广播，非0 rank 在守护线程里接收
                    if RANK == 0:
                        try:
                            logger.debug("[process_task] rank0 BCAST(generate) begin")
                            D.broadcast_object_from_rank0({"cmd": "generate", "payload": payload})
                            logger.debug("[process_task] rank0 BCAST(generate) ok")
                        except Exception as e:
                            logger.error(f"[process_task] broadcast generate failed: {e}")
                            raise

                    # ★ 第二拍：全体 barrier（与守护线程一致）
                    try:
                        logger.debug("[process_task] BARRIER(pre-infer) begin")
                        D.dist_barrier()
                        logger.debug("[process_task] BARRIER(pre-infer) ok")
                    except Exception as e:
                        logger.warning(f"[process_task] pre-infer barrier failed: {e}")

                # 第三拍：推理
                result = generate_video_files(
                    MODEL,
                    SAVE_DIR,
                    prompt=payload["prompt"],
                    video_size=payload["video_size"],
                    video_length=payload["video_length"],
                    seed=payload["seed"],
                    num_inference_steps=payload["num_inference_steps"],
                    guidance_scale=payload.get("guidance_scale", getattr(PARSED_ARGS, "cfg_scale", 1.0)),
                    flow_shift=payload.get("flow_shift", getattr(PARSED_ARGS, "flow_shift", 7.0)),
                    embedded_guidance_scale=payload.get("embedded_guidance_scale", getattr(PARSED_ARGS, "embedded_cfg_scale", None)),
                    num_videos=payload["num_videos"],
                    negative_prompt=payload.get("negative_prompt", getattr(PARSED_ARGS, "neg_prompt", "")),
                    progress_cb=_progress_cb if (not IS_DIST or RANK == 0) else None,
                )

                # 第四拍：收尾 barrier
                if IS_DIST and dist.is_initialized():
                    try:
                        logger.debug("[process_task] BARRIER(post-infer) begin")
                        D.dist_barrier()
                        logger.debug("[process_task] BARRIER(post-infer) ok")
                    except Exception as e:
                        logger.warning(f"[process_task] post-infer barrier failed: {e}")

                return result

            video_paths, _ = await loop.run_in_executor(EXECUTOR, _run_blocking)

            if (not IS_DIST) or (RANK == 0):
                rec["status"] = "done"
                rec["progress"]["saved"] = rec["progress"]["total"]
                rec["progress"]["percent"] = 100
                rec["updated_at"] = datetime.utcnow().isoformat()
                logger.info(f"[process_task] {task_id} -> done: {len(video_paths)} files")

    except Exception as task_error:
        logger.exception(f"[process_task] {task_id} failed")
        r = TASKS.get(task_id)
        if r:
            r["status"] = "failed"
            r["error"] = f"{type(task_error).__name__}: {task_error}\n{traceback.format_exc()}"
            r["updated_at"] = datetime.utcnow().isoformat()
    finally:
        try:
            QUEUE.task_done()
        except Exception:
            logger.exception("QUEUE.task_done() failed")


async def worker_loop():
    logger.info(f"[worker] start with MAX_CONCURRENCY={MAX_CONCURRENCY}, device={DEVICE_STR}")
    while True:
        task_id, payload = await QUEUE.get()
        asyncio.create_task(process_task(task_id, payload))


# ---------------- 启动/收尾 ----------------
def _parse_official_args():
    return parse_args()


def _merge_required_server_cli(args):
    if (not SERVER_CLI.get("model_base")) and SERVER_CLI.get("model_path"):
        SERVER_CLI["model_base"] = SERVER_CLI["model_path"]

    required = ["model_base", "dit_weight", "vae_path", "text_encoder_path", "text_encoder_2_path"]
    missing = [k for k in required if k not in SERVER_CLI or not SERVER_CLI[k]]
    if missing:
        raise RuntimeError(
            "[startup] missing required arguments: " +
            ", ".join(f"--{k.replace('_','-')}" for k in missing)
        )
    for k in required:
        setattr(args, k, SERVER_CLI[k])
    if "flow_reverse" in SERVER_CLI:
        setattr(args, "flow_reverse", True)
    return args


def _validate_paths_or_raise(args):
    checks = [
        ("model_base", Path(args.model_base), "dir"),
        ("dit_weight", Path(args.dit_weight), "file"),
        ("vae_path", Path(args.vae_path), "dir"),
        ("text_encoder_path", Path(args.text_encoder_path), "dir"),
        ("text_encoder_2_path", Path(args.text_encoder_2_path), "dir"),
    ]
    for name, p, kind in checks:
        if kind == "dir" and not p.is_dir():
            raise RuntimeError(f"[startup] path does not exist (dir required): --{name.replace('_','-')}='{p}'")
        if kind == "file" and not p.is_file():
            raise RuntimeError(f"[startup] path does not exist (file required): --{name.replace('_','-')}='{p}'")


def _resolve_and_log_paths(args):
    global RESOLVED_MODEL_PATHS
    try:
        RESOLVED_MODEL_PATHS = {
            "model_base": str(Path(args.model_base).resolve()),
            "dit_weight": str(Path(args.dit_weight).resolve()),
            "vae_path": str(Path(args.vae_path).resolve()),
            "text_encoder_path": str(Path(args.text_encoder_path).resolve()),
            "text_encoder_2_path": str(Path(args.text_encoder_2_path).resolve()),
        }
        logger.info("[startup] Resolved model paths:\n" + json.dumps(RESOLVED_MODEL_PATHS, indent=2))
    except Exception as resolve_error:
        logger.warning(
            f"[startup] resolve model paths failed:{type(resolve_error).__name__}: {resolve_error}"
        )


def _init_model_and_ckpts(args):
    global MODEL
    if USE_CUDA:
        try:
            _ensure_ckpts_layout(args)
        except Exception as e:
            logger.warning(f"[ckpts] layout setup skipped (CUDA): {e}")
    else:
        logger.info("[ckpts] skip layout symlinks (device != CUDA)")
    MODEL = initialize_model(getattr(args, "model_base", "ckpts"))


def _resolve_save_dir():
    global SAVE_DIR
    cli_save = SERVER_CLI.get("save_path")
    if cli_save:
        SAVE_DIR = Path(cli_save).expanduser().resolve()
    else:
        wings_root = _get_wings_root_dir()
        SAVE_DIR = (wings_root / "outputs").resolve()
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"[save] output directory = {SAVE_DIR}")


def _start_runtime_workers():
    global EXECUTOR
    EXECUTOR = ThreadPoolExecutor(max_workers=max(1, MAX_CONCURRENCY), thread_name_prefix="infer")
    asyncio.create_task(worker_loop())
    logger.info("[startup] ready.")


def _shutdown_runtime():
    try:
        if EXECUTOR:
            EXECUTOR.shutdown(wait=False, cancel_futures=True)
    except Exception as shutdown_error:
        logger.warning(f"[shutdown] executor shutdown error: {shutdown_error}")


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    """
    启动流程 - 使用 hyvideo 的分布式环境；在任何分布式同步前先绑定设备到 LOCAL_RANK。
    """
    def _ensure_thread_cuda_device():
        """兼容旧名：内部支持 NPU + CUDA 的主线程设备绑定"""
        try:
            lr = int(os.getenv("LOCAL_RANK", "0"))
        except Exception:
            lr = 0

        # NPU 优先
        try:
            if hasattr(torch, "npu") and torch.npu.is_available():
                cur = torch.npu.current_device()
                if cur != lr:
                    torch.npu.set_device(f"npu:{lr}")
                    logger.debug(f"[dist] main-thread set_device(npu:{lr})")
                return
        except Exception as e:
            logger.warning(f"[dist] main-thread set npu:{lr} failed: {e}")

        # CUDA 次之
        if torch.cuda.is_available():
            try:
                cur = torch.cuda.current_device()
            except Exception:
                cur = None
            if cur != lr:
                try:
                    torch.cuda.set_device(lr)
                    logger.debug(f"[dist] main-thread set_device(cuda:{lr})")
                except Exception as e:
                    logger.warning(f"[dist] main-thread set_device(cuda:{lr}) failed: {e}")

    global PARSED_ARGS, ALL_READY, RANK, WORLD_SIZE, IS_DIST

    # 先用 env 兜底
    RANK = int(os.getenv("RANK", "0"))
    WORLD_SIZE = int(os.getenv("WORLD_SIZE", "1"))
    IS_DIST = WORLD_SIZE > 1

    _ensure_thread_cuda_device()

    # 1) 解析/合并/校验
    args = _parse_official_args()
    PARSED_ARGS = args
    args = _merge_required_server_cli(args)
    _validate_paths_or_raise(args)
    _resolve_and_log_paths(args)

    # 2) 初始化模型（hyvideo 内部可能会此时 init_process_group）
    _init_model_and_ckpts(args)

    # 3) 再次从 torch.distributed 读取真实 rank/world_size（若已 init）
    if dist.is_initialized():
        try:
            RANK, WORLD_SIZE = dist.get_rank(), dist.get_world_size()
            IS_DIST = WORLD_SIZE > 1
        except Exception as e:
            logger.warning(f"[startup] read dist rank/world failed: {e}")

    # 3.5) ★★★ 预热（仅 NPU；分布式前后各 barrier 一次）★★★
    global WARMUP_DONE
    warmup_ok = True
    try:
        if USE_NPU:
            if dist.is_initialized():
                try: D.dist_barrier()
                except Exception as e: logger.warning(f"[startup] warmup pre-barrier skip: {e}")
            warmup_ok = _do_warmup(MODEL, PARSED_ARGS)
            if dist.is_initialized():
                try: D.dist_barrier()
                except Exception as e: logger.warning(f"[startup] warmup post-barrier skip: {e}")
    except Exception as e:
        warmup_ok = False
        logger.exception("[startup] warmup fatal")
    WARMUP_DONE = bool(warmup_ok)

    # 4) 输出目录
    _resolve_save_dir()

    # 5) 聚合就绪 + 初始 barrier
    local_ok = (MODEL is not None) and (SAVE_DIR is not None) and ( (not USE_NPU) or WARMUP_DONE )
    try:
        ALL_READY = D.dist_all_ready(local_ok, WORLD_SIZE)
    except Exception as e:
        logger.warning(f"[startup] dist_all_ready failed (fallback local_ok): {e}")
        ALL_READY = bool(local_ok)

    if IS_DIST and dist.is_initialized():
        try:
            _ensure_thread_cuda_device()
            D.dist_barrier()
        except Exception as e:
            logger.warning(f"[startup] initial barrier failed or skipped: {e}")

    # 6) 各 rank 的运行角色
    if (not IS_DIST) or (RANK == 0):
        _start_runtime_workers()
    if IS_DIST and RANK != 0:
        D.start_daemon_thread(MODEL, SAVE_DIR, PARSED_ARGS)

    logger.info(f"[startup] rank={RANK}/{WORLD_SIZE}, IS_DIST={IS_DIST}, device={DEVICE_STR}")
    logger.info("Application startup complete")
    try:
        yield
    finally:
        logger.info("[shutdown] cleaning up...")
        if IS_DIST and RANK == 0 and dist.is_initialized():
            try:
                _ensure_thread_cuda_device()
                D.broadcast_object_from_rank0({"cmd": "shutdown"})
            except Exception as e:
                logger.warning(f"[shutdown] broadcast shutdown failed: {e}")

        try:
            D.stop_daemon_thread()
        except Exception as e:
            logger.warning(f"[shutdown] stop_daemon_thread failed: {e}")

        _shutdown_runtime()


# 使用 lifespan 版本的 app
wings_engine = FastAPI(title="HunyuanVideo Unified (GPU + NPU)", lifespan=lifespan)

# ---------------- 路由 ----------------
@wings_engine.get("/health")
def health():
    ready = ALL_READY
    payload = {"status": bool(ready)}
    return JSONResponse(status_code=200 if ready else 503, content=payload)


@wings_engine.post("/v1/videos/text2video", response_model=SubmitResponse)
async def submit_job(req_pub: GenerateRequestPublic = Body(...)):
    if MODEL is None or SAVE_DIR is None:
        raise HTTPException(status_code=503, detail="Model not ready")
    w, h = map(int, req_pub.video_size.split("x"))
    if req_pub.video_length <= 0 or w <= 0 or h <= 0:
        raise HTTPException(status_code=400, detail="`video_size`/`frames` must be positive")
    internal = {
        "prompt": req_pub.prompt.strip(),
        "video_size": req_pub.video_size,
        "video_length": req_pub.video_length,
        "seed": req_pub.seed,
        "num_inference_steps": req_pub.num_inference_steps,
        "num_videos": req_pub.num_videos,
        "guidance_scale": getattr(PARSED_ARGS, "cfg_scale", 1.0),
        "flow_shift": getattr(PARSED_ARGS, "flow_shift", 7.0),
        "embedded_guidance_scale": getattr(PARSED_ARGS, "embedded_cfg_scale", None),
        "negative_prompt": getattr(PARSED_ARGS, "neg_prompt", ""),
    }
    task_id = uuid.uuid4().hex
    now = datetime.utcnow().isoformat()
    TASKS[task_id] = {
        "status": "in_queue",
        "progress": {"total": internal["num_videos"], "saved": 0, "percent": 0},
        "params": internal.copy(),
        "results": [],
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    await QUEUE.put((task_id, internal))
    return SubmitResponse(
        task_id=task_id,
        task_status="in_queue",
        message="The video generation task has been submitted. Please use the task_id to check the task status.",
    )


@wings_engine.get("/v1/videos/text2video/{task_id}", response_model=StatusResponse)
async def get_task(task_id: str):
    rec = TASKS.get(task_id)
    return _serialize_status_response(task_id, rec)