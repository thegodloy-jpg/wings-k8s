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
    try:
        import torch_npu  # noqa: F401
        return torch.npu.is_available()
    except ImportError:
        return False


def get_available_device() -> DeviceType:
    if torch.cuda.is_available():
        return "cuda"
    elif is_npu_available():
        return "npu"
    return "cpu"


def is_device_available(device: str) -> bool:
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
    """获取NVIDIA GPU信息
    
    Returns:
        List[Dict]: 包含每个GPU信息的字典列表，格式为:
            {
                "device_id": int,
                "name": str,
                "total_memory": int,
                "used_memory": int,
                "free_memory": int,
                "util": int,
                "vendor": str
            }
            如果出错则返回空列表
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
        # 获取总显存和剩余显存
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

            # 将字节转换为 GB 并保留两位小数
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
                    # 将字节转换为 GB 并保留两位小数
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
    根据显存大小判断是否为H20系列GPU
    
    Args:
        total_memory: GPU显存大小(GB)
        tolerance_gb: 允许的误差范围(GB)
        
    Returns:
        str: "H20-96G" 或 "H20-141G"，如果不匹配则返回空字符串
    """
    if abs(total_memory - 96) <= tolerance_gb:
        return "H20-96G"
    elif abs(total_memory - 141) <= tolerance_gb:
        return "H20-141G"
    return ""
