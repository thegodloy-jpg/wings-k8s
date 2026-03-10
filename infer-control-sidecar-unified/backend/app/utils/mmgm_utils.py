# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""HunyuanVideo 路径自动探测工具。

在给定的模型根目录下自动探测混元（HunyuanVideo）关键路径：
- dit_weight:  优先 <var>/transformers/mp_rank_00_model_states.pt；
               否则全局搜 mp_rank_00_model_states.pt 或 pytorch_model_*.pt
- vae_path:    优先 <var>/vae；否则全局搜名为 'vae' 的目录
- text_encoder_path:      默认 <root>/text_encoder
- text_encoder_2_path:    默认 <root>/clip-vit-large-patch14
"""

import os
from pathlib import Path as _P
from typing import Dict


def autodiscover_hunyuan_paths(model_path_root: str) -> Dict[str, str]:
    """自动探测 HunyuanVideo 模型关键路径。

    Args:
        model_path_root: 模型根目录

    Returns:
        Dict[str, str]: 包含 dit_weight, vae_path, text_encoder_path,
                        text_encoder_2_path 的字典

    Raises:
        ValueError: 模型根目录不存在
    """
    base = _P(model_path_root).expanduser().resolve()
    if not base.exists():
        raise ValueError(f"[MMGM] model_path not exists: {base}")

    vardir = _find_variant_directory(base)

    return {
        "dit_weight": _find_dit_weight(base, vardir),
        "vae_path": _find_vae_path(base, vardir),
        "text_encoder_path": _find_text_encoder_path(base),
        "text_encoder_2_path": _find_text_encoder_2_path(base),
    }


def _find_variant_directory(base_path: _P):
    """查找变体目录（720p / 540p）。"""
    if (base_path / "hunyuan-video-t2v-720p").is_dir():
        return base_path / "hunyuan-video-t2v-720p"
    elif (base_path / "hunyuan-video-t2v-540p").is_dir():
        return base_path / "hunyuan-video-t2v-540p"
    return None


def _find_dit_weight(base_path: _P, variant_dir) -> str:
    """查找 DIT 权重文件。"""
    if variant_dir and (variant_dir / "transformers" / "mp_rank_00_model_states.pt").is_file():
        return str((variant_dir / "transformers" / "mp_rank_00_model_states.pt").resolve())

    for root, _, files in os.walk(str(base_path)):
        for fn in files:
            if fn == "mp_rank_00_model_states.pt" or fn.startswith("pytorch_model_"):
                return str((_P(root) / fn).resolve())
    return ""


def _find_vae_path(base_path: _P, variant_dir) -> str:
    """查找 VAE 目录。"""
    if variant_dir and (variant_dir / "vae").is_dir():
        return str((variant_dir / "vae").resolve())

    for root, dirs, _ in os.walk(str(base_path)):
        if "vae" in dirs:
            return str((_P(root) / "vae").resolve())
    return ""


def _find_text_encoder_path(base_path: _P) -> str:
    """查找文本编码器 1 目录。"""
    if (base_path / "text_encoder").is_dir():
        return str((base_path / "text_encoder").resolve())

    for root, dirs, _ in os.walk(str(base_path)):
        for d in dirs:
            if d.startswith("text_encoder"):
                return str((_P(root) / d).resolve())
    return ""


def _find_text_encoder_2_path(base_path: _P) -> str:
    """查找文本编码器 2 / CLIP-L 目录。"""
    if (base_path / "clip-vit-large-patch14").is_dir():
        return str((base_path / "clip-vit-large-patch14").resolve())

    if (base_path / "text_encoder_2").is_dir():
        return str((base_path / "text_encoder_2").resolve())

    for root, dirs, _ in os.walk(str(base_path)):
        for d in dirs:
            if d == "text_encoder_2" or "clip-vit-large" in d:
                return str((_P(root) / d).resolve())
    return ""
