#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
state.py — 启动前置 / 状态管理模块
- 设定通用环境变量默认值（不影响设备选择）
- 解析服务端 CLI 参数（--server-host/--server-port/--device/*模型路径* 等），并从 sys.argv 剥离
- 根据 --device=cpu|cuda|npu 初始化 USE_NPU/USE_CUDA/DEVICE_STR（与原版完全一致的逻辑）

注意：
- 本模块 import 即执行解析/设置（与单文件版保持相同行为时机）
- 仅新增必要的 import/导出，不改动原有函数体/日志文案/缩进
"""

# ===== Imports (G.FMT.05: 在模块注释之后，globals/常量之前) =====
import os
import sys
from typing import Any, Dict, List

from loguru import logger
import torch


# ---------------------------------------------------------------------
# 基础环境变量（按需；不影响设备选择）
# ---------------------------------------------------------------------
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("DISABLE_TQDM", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:False")

# ---------------------------------------------------------------------
# 设备由外部参数 --device 控制（cpu|cuda|npu）
# ---------------------------------------------------------------------
USE_NPU: bool = False
USE_CUDA: bool = False
DEVICE_STR: str = "cpu"
_TORCH_NPU = None  # 仅 npu 时用于保存导入句柄


def _parse_server_cli_and_strip(argv: List[str]) -> Dict[str, Any]:
    import argparse
    p = argparse.ArgumentParser(add_help=False)
    # Server-only listening parameters
    p.add_argument("--server-host", dest="server_host", type=str,
                   help="Hostname or IP address for the server to bind to.")
    p.add_argument("--server-port", dest="server_port", type=int,
                   help="Port number for the server to listen on.")
    p.add_argument("--device", dest="device", type=str, required=True,  # Marked as required
                   help="Device to run the model on. Options: 'cpu', 'cuda', 'npu'.")
    p.add_argument("--model-base", dest="model_base", type=str,
                   help="Base directory path for model files.")
    p.add_argument("--model-path", dest="model_path", type=str,  # Compatible with historical args
                   help="Path to the main model file. (For historical compatibility)")
    p.add_argument("--dit-weight", dest="dit_weight", type=str,
                   help="Path to the DIT (Diffusion Transformer) model weights.")
    p.add_argument("--vae-path", dest="vae_path", type=str,
                   help="Path to the VAE (Variational Autoencoder) model.")
    p.add_argument("--text-encoder-path", dest="text_encoder_path", type=str,
                   help="Path to the primary text encoder model.")
    p.add_argument("--text-encoder-2-path", dest="text_encoder_2_path", type=str,
                   help="Path to the secondary text encoder model (e.g., for SDXL architecture).")
    p.add_argument("--flow-reverse", dest="flow_reverse",
                   type=lambda s: s.lower() in ("1", "true", "yes"), default=True,
                   help="Enable or reverse the flow process. Accepts '1', 'true', 'yes' (default: True).")
    p.add_argument("--save-path", dest="save_path", type=str,
                   help="Directory path for saving generated outputs. Optional.")

    known, remaining = p.parse_known_args(argv)
    sys.argv = [sys.argv[0]] + remaining  # 从 sys.argv 剔除这些"服务端参数"

    data = {k: v for k, v in vars(known).items() if v is not None}
    data = {k: v for k, v in vars(known).items() if v is not None}
    if "server_host" in data:
        os.environ["SERVER_HOST"] = data["server_host"]
    if "server_port" in data:
        os.environ["SERVER_PORT"] = str(data["server_port"])
    return data


def _apply_device_from_cli_or_raise():
    global USE_NPU, USE_CUDA, DEVICE_STR, _TORCH_NPU

    dev = SERVER_CLI.get("device") if 'SERVER_CLI' in globals() else None
    if not dev:
        raise RuntimeError(
            "[startup] missing required argument: --device (cpu|cuda|npu)"
        )
    dev = str(dev).strip().lower()
    if dev not in ("cpu", "cuda", "npu"):
        raise RuntimeError(
            f"[startup] invalid --device '{dev}', must be one of: cpu, cuda, npu"
        )

    # 初始化三元状态
    USE_NPU = (dev == "npu")
    USE_CUDA = (dev == "cuda")
    DEVICE_STR = "npu" if USE_NPU else ("cuda" if USE_CUDA else "cpu")

    if USE_NPU:
        try:
            import torch_npu as _torch_npu
            _TORCH_NPU = _torch_npu
            # 与原先 B 版保持一致的安全设置
            try:
                _torch_npu.npu.set_compile_mode(jit_compile=False)
            except Exception as _e:
                logger.warning(f"[npu] set_compile_mode ignored: {_e}")
            if hasattr(torch.npu, "config") and hasattr(torch.npu.config, "allow_internal_format"):
                torch.npu.config.allow_internal_format = False
            logger.info("[device] using NPU (from --device)")
        except Exception as ne:
            # 明确失败：外部指定了 npu，但运行环境不支持
            raise RuntimeError(f"[device] NPU selected but not available: {ne}") from ne
    elif USE_CUDA:
        # 不强制要求 cuda 可用；若不可用仅告警（方便容器里先跑到报错点）
        if not torch.cuda.is_available():
            logger.warning("[device] --device=cuda but torch.cuda.is_available()=False")
        logger.info("[device] using CUDA (from --device)")
    else:
        logger.info("[device] using CPU (from --device)")


# 在模块 import 时立刻解析服务端参数并剥离
SERVER_CLI: Dict[str, Any] = _parse_server_cli_and_strip(sys.argv[1:])
# 使用外部参数初始化设备（缺失或非法会立即抛错）
_apply_device_from_cli_or_raise()