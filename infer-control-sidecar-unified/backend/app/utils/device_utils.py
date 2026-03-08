# =============================================================================
# 文件: utils/device_utils.py
# 用途: 设备级辅助方法，用于硬件能力检查和资源内省
# 状态: 活跃，复用自 wings 项目的设备工具模块
#
# 功能概述:
#   本模块提供跨平台的设备探测功能，支持：
#   - NVIDIA GPU (CUDA)    —— 通过 pynvml/torch.cuda 检测
#   - 华为昇腾 NPU (Ascend) —— 通过 torch_npu 检测
#   - CPU 回退
#
# 主要功能:
#   - is_npu_available()       : 检查昇腾 NPU 是否可用
#   - get_available_device()   : 返回当前可用设备类型 (cuda/npu/cpu)
#   - gpu_count()              : 返回可见 GPU/NPU 数量
#   - get_nvidia_gpu_info()    : 获取 NVIDIA GPU 详情列表
#   - get_npu_info()           : 获取昇腾 NPU 详情列表
#   - get_hardware_env()       : 获取完整硬件环境信息
#
# Sidecar 架构契约:
#   - 硬件探测应优雅降级，不在缺少驱动时崩溃
#   - 避免对异构节点做硬编码假设
#
# =============================================================================
# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.

import os
from typing import List, Dict, Literal, Union, Any
import logging

import torch

logger = logging.getLogger(__name__)

DeviceType = Literal["cuda", "npu", "cpu"]
DEVICE_TO_ENV_NAME = {
    "cuda": "CUDA_VISIBLE_DEVICES",
    "npu": "ASCEND_RT_VISIBLE_DEVICES",
}


def is_npu_available() -> bool:
    """检查昇腾 NPU (torch_npu) 是否可用。

    Returns:
        bool: NPU 可用返回 True，否则返回 False
    """
    try:
        import torch_npu  # noqa: F401
        return torch.npu.is_available()
    except ImportError:
        return False


def get_available_device() -> DeviceType:
    """获取当前系统可用的设备类型。

    检查顺序：CUDA GPU > 昇腾 NPU > CPU

    Returns:
        DeviceType: 'cuda'、'npu' 或 'cpu'
    """
    if torch.cuda.is_available():
        return "cuda"
    elif is_npu_available():
        return "npu"
    return "cpu"


def is_device_available(device: str) -> bool:
    """检查指定设备类型是否可用。

    Args:
        device: 设备类型字符串 ('cuda', 'npu', 'cpu')

    Returns:
        bool: 设备可用返回 True
    """
    if device == "cuda":
        return torch.cuda.is_available()
    elif device == "npu":
        return is_npu_available()
    elif device == "cpu":
        return True

    return False


def move_model_to_available_device(model):
    device = get_available_device()

    if device == "cpu":
        return model

    return model.to(device)


def get_device_preferred_dtype(device: str) -> Union[torch.dtype, None]:
    if device == "cpu":
        return torch.float32
    elif device == "cuda" or device == "npu":
        return torch.float16

    return None


def is_hf_accelerate_supported(device: str) -> bool:
    return device == "cuda" or device == "npu"


def empty_cache():
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    if is_npu_available():
        torch.npu.empty_cache()


def get_available_device_env_name():
    return DEVICE_TO_ENV_NAME.get(get_available_device())


def gpu_count():
    """获取当前可见的 GPU/NPU 设备数量。

    - 对于 NVIDIA GPU：考虑 CUDA_VISIBLE_DEVICES 环境变量的限制
    - 对于昇腾 NPU：返回 torch.npu.device_count()
    - 无可用设备时返回 0

    Returns:
        int: 可用设备数量
    """
    if torch.cuda.is_available():
        cuda_visible_devices_env = os.getenv("CUDA_VISIBLE_DEVICES", None)

        if cuda_visible_devices_env is None:
            return torch.cuda.device_count()

        cuda_visible_devices = (
            cuda_visible_devices_env.split(",") if cuda_visible_devices_env else []
        )

        return min(torch.cuda.device_count(), len(cuda_visible_devices))
    elif is_npu_available():
        return torch.npu.device_count()
    else:
        return 0


def _get_nvidia_gpu_mem_info(gpu_id: int) -> Dict[str, float]:
    from pynvml import (
        nvmlDeviceGetHandleByIndex,
        nvmlDeviceGetMemoryInfo,
        nvmlDeviceGetName,
        nvmlDeviceGetUtilizationRates,
    )

    handler = nvmlDeviceGetHandleByIndex(gpu_id)
    gpu_name = nvmlDeviceGetName(handler)
    mem_info = nvmlDeviceGetMemoryInfo(handler)
    utilization = nvmlDeviceGetUtilizationRates(handler)
    return {
        "device_id": gpu_id,
        "name": gpu_name,
        "total_memory": mem_info.total,
        "used_memory": mem_info.used,
        "free_memory": mem_info.free,
        "util": utilization.gpu,
        "vendor": "Nvidia"
    }


def get_nvidia_gpu_info() -> List[Dict[str, Any]]:
    """获取所有 NVIDIA GPU 的详细信息列表。

    通过 pynvml 库查询每张卡的:
    - 设备 ID 和名称
    - 总显存/已用显存/空闲显存 (bytes)
    - GPU 利用率 (%)

    Returns:
        List[Dict]: GPU 信息字典列表，每个字典包含:
            {
                "device_id": int,
                "name": str,
                "total_memory": int,
                "used_memory": int,
                "free_memory": int,
                "util": int,
                "vendor": str
            }

    注意:
        若 pynvml 初始化失败或查询异常，返回空列表。
    """
    from pynvml import (
        nvmlDeviceGetCount,
        nvmlInit,
        nvmlShutdown,
        NVMLError,
    )

    try:
        nvmlInit()
    except Exception as e:
        logger.error(f"Unexpected error during NVML initialization: {str(e)}")
        return []

    try:
        device_count = nvmlDeviceGetCount()
        res = []
        for i in range(device_count):
            try:
                res.append(_get_nvidia_gpu_mem_info(i))
            except NVMLError as e:
                logger.warning(f"Failed to get info for GPU {i}: {str(e)}")
                continue
            except Exception as e:
                logger.error(f"Unexpected error getting info for GPU {i}: {str(e)}")
                continue

        return res
    except NVMLError as e:
        logger.error(f"Failed to get GPU count: {str(e)}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error getting GPU info: {str(e)}")
        return []
    finally:
        try:
            nvmlShutdown()
        except NVMLError as e:
            logger.warning(f"Failed to shutdown NVML: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error during NVML shutdown: {str(e)}")


def _get_npu_mem_info(npu_id: int) -> Dict[str, Any]:
    try:
        import torch_npu

        device_name = torch_npu.npu.get_device_name(npu_id)
        #
        free_memory, total_memory = torch_npu.npu.mem_get_info(npu_id)
        used_memory = total_memory - free_memory

        return {
            "device_id": npu_id,
            "name": device_name,
            "total_memory": total_memory,
            "used_memory": used_memory,
            "free_memory": free_memory,
            "vendor": "Ascend",
        }
    except Exception as e:
        logger.error(f"Failed to get NPU-{npu_id} info: {str(e)}")
        return {"error": str(e)}


def get_device_info() -> Dict[str, Any]:
    device = get_available_device()
    base_count = 0
    device_key = "device"
    count_key = "count"
    details_key = "details"
    units_key = "units"
    error_key = "error"
    total_memory_key = "total_memory"
    used_memory_key = "used_memory"
    free_memory_key = "free_memory"

    result_template = {
        device_key: device,
        count_key: base_count,
        details_key: [],
        units_key: "GB"
    }

    try:
        if device == "cuda":
            raw_gpu_info = get_nvidia_gpu_info()

            #  GB
            for device_info in raw_gpu_info:
                device_info[total_memory_key] = round(device_info[total_memory_key] / (1024 ** 3), 2)
                device_info[used_memory_key] = round(device_info[used_memory_key] / (1024 ** 3), 2)
                device_info[free_memory_key] = round(device_info[free_memory_key] / (1024 ** 3), 2)

            result_template[count_key] = len(raw_gpu_info)
            result_template[details_key] = raw_gpu_info

        elif device == "npu":
            import torch_npu

            device_count = torch.npu.device_count()
            details = []
            for device_id in range(device_count):
                info = _get_npu_mem_info(device_id)
                if not info.get("error"):
                    #  GB
                    info[total_memory_key] = round(info[total_memory_key] / (1024 ** 3), 2)
                    info[used_memory_key] = round(info[used_memory_key] / (1024 ** 3), 2)
                    info[free_memory_key] = round(info[free_memory_key] / (1024 ** 3), 2)
                    details.append(info)

            result_template[count_key] = len(details)
            result_template[details_key] = details
        else:
            logger.error(f"Unsupported device type: {device}")
            result_template[error_key] = f"Unsupported device type: {device}"

    except Exception as e:
        logger.error(f"Error getting {device} device info")
        result_template[error_key] = str(e)
    return result_template


def is_h20_gpu(total_memory: float, tolerance_gb: float = 10.0) -> str:
    """
    H20GPU

    Args:
        total_memory: GPU(GB)
        tolerance_gb: (GB)

    Returns:
        str: "H20-96G"  "H20-141G"
    """
    if abs(total_memory - 96) <= tolerance_gb:
        return "H20-96G"
    elif abs(total_memory - 141) <= tolerance_gb:
        return "H20-141G"
    return ""
