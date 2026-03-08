# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: core/hardware_detect.py
# Purpose: Detects hardware runtime characteristics to inform config selection.
# Status: Active utility in launch-plan preparation.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Detection should be best-effort and safe.
# - Avoid hard failures on optional tooling absence.
# -----------------------------------------------------------------------------
# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
硬件环境上下文模块（无探测版）

为避免引入 torch/pynvml 等重依赖，本模块不再进行运行时硬件探测，
改为通过环境变量提供静态上下文：
- WINGS_DEVICE / DEVICE: nvidia|ascend（默认 nvidia）
- WINGS_DEVICE_COUNT / DEVICE_COUNT: 设备数量（默认 1）
- WINGS_DEVICE_NAME: 设备名（可选）
"""

import logging
import os
from typing import Dict, Any

logger = logging.getLogger(__name__)


def _normalize_device(raw: str) -> str:
    val = (raw or "").strip().lower()
    mapping = {
        "nvidia": "nvidia",
        "gpu": "nvidia",
        "cuda": "nvidia",
        "ascend": "ascend",
        "npu": "ascend",
    }
    return mapping.get(val, "nvidia")


def _parse_count(raw: str) -> int:
    try:
        value = int((raw or "1").strip())
        return value if value > 0 else 1
    except Exception:
        return 1


def detect_hardware() -> Dict[str, Any]:
    """
    返回静态硬件上下文（不做运行时探测）。

    Returns:
        Dict[str, Any]:
            - device: 设备类型 ('nvidia' 或 'ascend')
            - count: 设备数量
            - details: 可选设备详情（仅来自环境变量）
            - units: 内存单位（GB）
    """
    device = _normalize_device(os.getenv("WINGS_DEVICE", os.getenv("DEVICE", "nvidia")))
    count = _parse_count(os.getenv("WINGS_DEVICE_COUNT", os.getenv("DEVICE_COUNT", "1")))
    device_name = os.getenv("WINGS_DEVICE_NAME", "").strip()

    details = []
    if device_name:
        details.append({"name": device_name})

    result = {
        "device": device,
        "count": count,
        "details": details,
        "units": "GB",
    }
    logger.info("Using static hardware context (detection disabled): %s", result)
    return result

