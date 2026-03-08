# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
硬件环境检测模块

负责检测当前的计算硬件环境 (NVIDIA GPU, Ascend NPU, CPU)。
"""

import logging
from typing import Dict, Any

from wings.utils.device_utils import get_device_info

logger = logging.getLogger(__name__)


def detect_hardware() -> Dict[str, Any]:
    """
    检测当前的硬件环境。

    优先级: NVIDIA GPU > Ascend NPU > CPU

    Returns:
        Dict[str, Any]: 包含详细硬件信息的字典，包括:
            - device: 设备类型 ('nvidia', 'ascend', 'cpu')
            - count: 设备数量
            - details: 每个设备的详细信息列表
            - units: 内存单位
    """
    logger.info("Starting hardware environment detection...")

    res = get_device_info()
    device = "device"
    if res.get(device) == 'cuda':
        res[device] = 'nvidia'
    elif res.get(device) == 'npu':
        res[device] = 'ascend'

    logger.info(f"Detected hardware environment: {res.get(device)}")
    
    return res
