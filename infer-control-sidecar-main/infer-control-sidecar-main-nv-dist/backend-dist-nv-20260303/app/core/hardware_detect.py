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


 torch/pynvml

- WINGS_DEVICE / DEVICE: nvidia|ascend nvidia
- WINGS_DEVICE_COUNT / DEVICE_COUNT:  1
- WINGS_DEVICE_NAME:
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


    Returns:
        Dict[str, Any]:
            - device:  ('nvidia'  'ascend')
            - count:
            - details:
            - units: GB
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

