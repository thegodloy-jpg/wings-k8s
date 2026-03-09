#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core.py — Schema 与工具：
- Pydantic 请求/响应模型（GenerateRequestPublic/SubmitResponse/TaskInfo/StatusResponse）
- 工具函数：时间步校验、文件名清理、状态串、状态序列化
- 路径/软链工具：ckpts 根、text_encoder/vae 别名、Wings 根、代码目录

说明：
- 函数体与原版保持一致（名称/参数/日志文本）
- 仅做模块化聚合；避免循环依赖的地方使用延迟导入
"""

import os
import re
import json
from pathlib import Path
from typing import Optional, Any, Dict
from loguru import logger
from pydantic import BaseModel, Field, field_validator, ConfigDict


# ---------------------------------------------------------------------
# 对外 API 模型（video_size 取代 resolution）
# ---------------------------------------------------------------------
class GenerateRequestPublic(BaseModel):
    prompt: str
    video_size: str = Field("1280x720", description='形如 "WxH"')
    video_length: int = Field(129, alias="frames")
    seed: int = -1
    num_inference_steps: int = Field(50, ge=1, le=100, alias="num_infer_steps")
    num_videos: int = Field(1, ge=1, le=int(os.getenv("MAX_VIDEOS_PER_REQ", "9")))
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    @field_validator("video_size")
    @classmethod
    def _validate_video_size(cls, v: str) -> str:
        if not re.match(r"^\d+x\d+$", v):
            raise ValueError('video_size 必须为 "WxH" 形式，例如 "1280x720"')
        return v


class SubmitResponse(BaseModel):
    task_id: str
    task_status: str
    message: str


class TaskInfo(BaseModel):
    video_url: Optional[str] = None
    error: Optional[str] = None


class StatusResponse(BaseModel):
    task_id: str
    task_status: str
    task_info: TaskInfo
    message: str


# ===== hyvideo VAE 路径补丁：既兼容标识符，也兼容路径 =====
def _normalize_hyvideo_vae_map(models_root: str):
    """
    把 hyvideo.vae.VAE_PATH 里的相对路径（如 ./ckpts/.../vae）
    统一改成绝对路径（基于 models_root，例如 /weight）。
    """
    try:
        import hyvideo.vae as _vae
        base = Path(models_root).expanduser().resolve()
        changed = {}
        for k, v in list(getattr(_vae, "VAE_PATH", {}).items()):
            vv = str(v).strip()
            if "ckpts/" in vv:  # 典型：./ckpts/hunyuan-video-t2v-720p/vae
                sub = vv.split("ckpts/", 1)[1]
                absdir = (base / sub).resolve()
            else:
                absdir = Path(vv).expanduser().resolve()
            changed[k] = str(absdir)
        if changed:
            _vae.VAE_PATH.update(changed)
            logger.info("[patch] VAE_PATH absolutized:", _vae.VAE_PATH)
    except Exception as patch_error:
        logger.warning(f"[patch] normalize VAE_PATH skipped: {patch_error}")


def _patch_hyvideo_load_vae():
    """
    只在传入是“路径”时把它转绝对；如果传入是“标识符”（如 884-16c-hy），则不改，
    让内部去用 VAE_PATH[key]（我们已把它绝对化）。
    """
    try:
        import hyvideo.vae as _vae
        import hyvideo.inference as _inf
        _orig = _vae.load_vae

        def _looks_like_path(s: str) -> bool:
            s = s.strip()
            return s.startswith(("/", "./", "../", "~")) or ("/" in s) or ("\\" in s)

        def _load_vae_guard(vae_arg, *a, **kw):
            s = str(vae_arg)
            if hasattr(_vae, "VAE_PATH") and s in _vae.VAE_PATH:
                # 是标识符：直接走原逻辑（VAE_PATH 已被我们改成绝对路径）
                logger.info(f"[patch] load_vae key: {s} -> { _vae.VAE_PATH.get(s) }")
                return _orig(s, *a, **kw)
            # 走路径分支：才转绝对路径
            if _looks_like_path(s):
                pp = str(Path(s).expanduser().resolve())
                logger.info(f"[patch] load_vae path -> {pp}")
                return _orig(pp, *a, **kw)
            # 既不是 key、也不像路径，就原样给原函数（以防未来新增 key）
            logger.info(f"[patch] load_vae passthrough: {s}")
            return _orig(s, *a, **kw)

        _vae.load_vae = _load_vae_guard
        if hasattr(_inf, "load_vae"):
            _inf.load_vae = _load_vae_guard

        logger.info("[patch] hyvideo.load_vae installed")
    except Exception as patch_error:
        logger.warning(f"[patch] hyvideo patch failed: {patch_error}")


def _ensure_valid_t(t: int) -> int:
    if (t - 1) % 4 == 0:
        return t
    raise ValueError("`video_length-1` must be a multiple of 4 (e.g. 25, 69, 129, ...)")


def _safe_prompt(s: str, maxlen: int = 100) -> str:
    s = s.replace("/", "").replace("\\", "").replace(" ", "_")
    s = re.sub(r'[:\\*?\"<>|]+', "_", s)
    return s[:maxlen]


def _status_message(status: str) -> str:
    return {
        "in_queue": "Task has been submitted and is queued for processing.",
        "running": "Task is currently being processed.",
        "done": "Task has been completed.",
        "failed": "Task execution failed. Please check the error details for more information.",
        "notfound": "Invalid task ID.",
    }.get(status, "")


def _serialize_status_response(task_id: str, rec: Optional[Dict[str, Any]]):
    if rec is None:
        return StatusResponse(
            task_id=task_id,
            task_status="notfound",
            task_info=TaskInfo(video_url=None, error=None),
            message=_status_message("notfound"),
        )
    status = rec.get("status", "in_queue")
    first_url = None
    if status == "done" and rec.get("results"):
        first_url = rec["results"][0].get("url")
    return StatusResponse(
        task_id=task_id,
        task_status=status,
        task_info=TaskInfo(video_url=first_url, error=rec.get("error")),
        message=_status_message(status),
    )


def _resolve_existing(path_str: Optional[str]) -> Optional[Path]:
    if not path_str:
        return None
    p = Path(path_str).expanduser()
    try:
        return p.resolve(strict=True)
    except FileNotFoundError:
        return None


def _is_samefile(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except FileNotFoundError:
        return False


def _safe_symlink(src: Path, dst: Path, label: str) -> None:
    try:
        if dst.is_symlink():
            if _is_samefile(dst, src):
                logger.debug(f"[ckpts] {label} already linked: {dst} -> {src}")
                return
            dst.unlink()
        elif dst.exists():
            logger.warning(f"[ckpts] skip linking {label}: '{dst}' exists and is not a symlink")
            return
        dst.symlink_to(src, target_is_directory=src.is_dir())
        logger.info(f"[ckpts] link created: {dst} -> {src}")
    except Exception as link_error:
        logger.warning(
            f"[ckpts] create link failed ({label}): {dst} -> {src} :"
            f"{type(link_error).__name__}: {link_error}"
        )


# ------------------ 语义步骤 ------------------ #
def _get_wings_root_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _get_code_dir() -> Path:
    return Path(__file__).resolve().parent


def _get_model_base(args) -> Path:
    model_base_str = getattr(args, "model_base", None)
    if not model_base_str:
        raise RuntimeError("[ckpts] model_base not set")
    model_base = Path(model_base_str).expanduser().resolve()
    if not model_base.exists():
        raise RuntimeError(f"[ckpts] model_base not found: {model_base}")
    return model_base


def _ensure_ckpts_root(code_dir: Path, model_base: Path) -> None:
    _safe_symlink(src=model_base, dst=code_dir / "ckpts", label="ckpts root")


def _ensure_alias(model_base: Path, attr_name: str, alias_name: str, args, warn_if_missing: bool = False) -> None:
    src = _resolve_existing(getattr(args, attr_name, None))
    if src is None:
        if warn_if_missing:
            logger.warning(
                "[ckpts] text_encoder_2_path not set or not exists; "
                "if your dir name differs from 'text_encoder_2', "
                "HyVideo may fallback to ./ckpts/text_encoder_2 and fail."
            )
        return
    _safe_symlink(src=src, dst=model_base / alias_name, label=alias_name)


def _ensure_ckpts_layout(args) -> None:
    code_dir = _get_code_dir()
    model_base = _get_model_base(args)
    _ensure_ckpts_root(code_dir, model_base)
    _ensure_alias(model_base, "text_encoder_2_path", "text_encoder_2", args, warn_if_missing=True)
    _ensure_alias(model_base, "text_encoder_path", "text_encoder", args)
    _ensure_alias(model_base, "vae_path", "vae", args)