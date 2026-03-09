# -----------------------------------------------------------------------------
# 文件: core/hardware_detect.py
# 用途: 硬件环境探测模块，为 config_loader 提供设备类型和数量信息。
#
# 工作原理:
#   在 sidecar 架构中，推理引擎和 sidecar 分属不同容器，因此 sidecar
#   无法直接访问 GPU/NPU 硬件。本模块改为从环境变量读取硬件信息，
#   而不是直接调用 torch/pynvml 进行探测。
#
# 支持的环境变量:
#   - WINGS_DEVICE / DEVICE       : 设备类型，可选 nvidia|ascend，默认 nvidia
#   - WINGS_DEVICE_COUNT / DEVICE_COUNT : 设备数量，默认 1
#   - WINGS_DEVICE_NAME           : 设备型号名称（可选，如 "Ascend910B"）
#
# 输出格式:
#   {
#     "device": "nvidia" | "ascend",
#     "count": int,
#     "details": [{"name": "..."}],  # 如有设备名称则填充
#     "units": "GB"
#   }
#
# Sidecar 契约:
#   - 探测应使用最佳努力策略，不应因任何探测失败而崩溃
#   - 避免破坏异构节点（混合 GPU/NPU 环境）的兼容性
# -----------------------------------------------------------------------------
# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""硬件环境静态探测模块。

在 sidecar 架构中，推理引擎和控制容器分属不同容器，无法直接用
 torch/pynvml 探测硬件。改用环境变量驱动：

- WINGS_DEVICE / DEVICE: 设备类型，支持 nvidia|ascend，默认 nvidia
- WINGS_DEVICE_COUNT / DEVICE_COUNT: 设备数量，默认 1
- WINGS_DEVICE_NAME: 设备型号名称（可选）
"""

import logging
import os
from typing import Dict, Any

logger = logging.getLogger(__name__)


def _normalize_device(raw: str) -> str:
    """将用户输入的设备类型字符串标准化为内部统一格式。

    支持多种别名映射：
      - 'nvidia'/'gpu'/'cuda' -> 'nvidia'
      - 'ascend'/'npu'        -> 'ascend'
      - 其他未识别的值回退到 'nvidia'

    Args:
        raw: 原始设备类型字符串（大小写不敏感）

    Returns:
        str: 标准化后的设备类型 ('nvidia' 或 'ascend')
    """
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
    """解析设备数量字符串，确保返回至少为 1 的正整数。

    异常输入（非数字字符串、负数、零）均回退到默认值 1，
    避免配置错误导致 launcher 崩溃。

    Args:
        raw: 原始设备数量字符串

    Returns:
        int: 解析后的设备数量（>= 1）
    """
    try:
        value = int((raw or "1").strip())
        return value if value > 0 else 1
    except Exception:
        return 1


def detect_hardware() -> Dict[str, Any]:
    """从环境变量探测硬件环境信息。

    该函数不依赖任何 GPU/NPU SDK，仅通过环境变量获取硬件描述。
    这是因为在 sidecar 架构中，控制容器内可能没有安装 torch/pynvml。

    读取的环境变量（按优先级）：
      - WINGS_DEVICE > DEVICE : 设备类型
      - WINGS_DEVICE_COUNT > DEVICE_COUNT : 设备数量
      - WINGS_DEVICE_NAME : 设备型号名称（可选）

    Returns:
        Dict[str, Any]: 硬件环境描述字典，包含以下字段：
            - device:  设备类型 ('nvidia' 或 'ascend')
            - count:   设备数量
            - details: 设备详情列表（含型号名称）
            - units:   显存单位 (GB)
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

