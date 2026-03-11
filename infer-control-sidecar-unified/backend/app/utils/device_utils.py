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
import subprocess
from typing import List, Dict, Literal, Union, Any
import logging

try:
    import torch
except ImportError:
    torch = None  # type: ignore[assignment]

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
        return torch is not None and torch.npu.is_available()
    except ImportError:
        return False


def get_available_device() -> DeviceType:
    """获取当前系统可用的设备类型。

    检查顺序：CUDA GPU > 昇腾 NPU > CPU

    Returns:
        DeviceType: 'cuda'、'npu' 或 'cpu'
    """
    if torch is not None and torch.cuda.is_available():
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
        return torch is not None and torch.cuda.is_available()
    elif device == "npu":
        return is_npu_available()
    elif device == "cpu":
        return True

    return False


def move_model_to_available_device(model):
    """将模型迁移到当前可用的最优计算设备上。

    按优先级自动选择设备（CUDA GPU > 昇腾 NPU > CPU）。
    若最终可用设备为 CPU，则不执行迁移，直接返回原始模型。

    Args:
        model: PyTorch 模型实例，需支持 .to(device) 方法。

    Returns:
        迁移到目标设备后的模型实例。若设备为 CPU 则返回原始模型。
    """
    device = get_available_device()

    if device == "cpu":
        return model

    return model.to(device)


def get_device_preferred_dtype(device: str) -> Union[torch.dtype, None]:
    """根据设备类型返回推荐的数据精度（dtype）。

    不同设备类型的推荐精度：
    - CPU: float32（全精度，兼容性最佳）
    - CUDA GPU / 昇腾 NPU: float16（半精度，推理性能更优）

    Args:
        device: 设备类型字符串，可选值为 'cuda'、'npu'、'cpu'。

    Returns:
        torch.dtype: 推荐的 PyTorch 数据类型。若设备类型不在已知范围内，返回 None。
    """
    if torch is None:
        return None
    if device == "cpu":
        return torch.float32
    elif device == "cuda" or device == "npu":
        return torch.float16

    return None


def is_hf_accelerate_supported(device: str) -> bool:
    """检查指定设备是否支持 HuggingFace Accelerate 库的加速特性。

    目前仅 CUDA GPU 和昇腾 NPU 支持 Accelerate 加速，CPU 不支持。

    Args:
        device: 设备类型字符串，可选值为 'cuda'、'npu'、'cpu'。

    Returns:
        bool: 支持 Accelerate 返回 True，否则返回 False。
    """
    return device == "cuda" or device == "npu"


def empty_cache():
    """清空 GPU/NPU 设备的显存缓存。

    同时检查 CUDA 和昇腾 NPU 的可用性，并分别调用对应的缓存清理方法。
    用于在模型卸载或推理完成后释放设备内存，降低 OOM 风险。

    注意:
        此操作不会释放被 PyTorch 张量占用的显存，仅释放缓存分配器中的空闲块。
    """
    if torch is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()

    if is_npu_available():
        torch.npu.empty_cache()


def get_available_device_env_name():
    """获取当前可用设备对应的可见设备环境变量名。

    映射关系：
    - CUDA GPU -> 'CUDA_VISIBLE_DEVICES'
    - 昇腾 NPU -> 'ASCEND_RT_VISIBLE_DEVICES'
    - CPU -> None（CPU 无对应环境变量）

    Returns:
        str | None: 环境变量名称字符串，若当前设备为 CPU 则返回 None。
    """
    return DEVICE_TO_ENV_NAME.get(get_available_device())


def gpu_count():
    """获取当前可见的 GPU/NPU 设备数量。

    - 对于 NVIDIA GPU：考虑 CUDA_VISIBLE_DEVICES 环境变量的限制
    - 对于昇腾 NPU：返回 torch.npu.device_count()
    - 无可用设备时返回 0

    Returns:
        int: 可用设备数量
    """
    if torch is not None and torch.cuda.is_available():
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
    """获取单张 NVIDIA GPU 的显存和利用率信息。

    通过 pynvml 库查询指定 GPU 的设备名称、显存用量及 GPU 利用率。
    此为内部方法，由 get_nvidia_gpu_info() 调用。

    Args:
        gpu_id: GPU 设备索引（从 0 开始）。

    Returns:
        Dict[str, float]: 包含以下字段的字典：
            - device_id (int): 设备索引
            - name (str): GPU 型号名称
            - total_memory (int): 总显存（字节）
            - used_memory (int): 已用显存（字节）
            - free_memory (int): 空闲显存（字节）
            - util (int): GPU 利用率百分比
            - vendor (str): 厂商标识，固定为 'Nvidia'
    """
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
        logger.error("Unexpected error during NVML initialization: %s", e)
        return []

    try:
        device_count = nvmlDeviceGetCount()
        res = []
        for i in range(device_count):
            try:
                res.append(_get_nvidia_gpu_mem_info(i))
            except NVMLError as e:
                logger.warning("Failed to get info for GPU %s: %s", i, e)
                continue
            except Exception as e:
                logger.error("Unexpected error getting info for GPU %s: %s", i, e)
                continue

        return res
    except NVMLError as e:
        logger.error("Failed to get GPU count: %s", e)
        return []
    except Exception as e:
        logger.error("Unexpected error getting GPU info: %s", e)
        return []
    finally:
        try:
            nvmlShutdown()
        except NVMLError as e:
            logger.warning("Failed to shutdown NVML: %s", e)
        except Exception as e:
            logger.error("Unexpected error during NVML shutdown: %s", e)


def _get_npu_mem_info(npu_id: int) -> Dict[str, Any]:
    """获取单张华为昇腾 NPU 的内存信息。

    通过 torch_npu 库查询指定 NPU 的设备名称和内存用量。
    此为内部方法，由 get_device_info() 调用。

    Args:
        npu_id: NPU 设备索引（从 0 开始）。

    Returns:
        Dict[str, Any]: 成功时包含以下字段的字典：
            - device_id (int): 设备索引
            - name (str): NPU 型号名称
            - total_memory (int): 总内存（字节）
            - used_memory (int): 已用内存（字节）
            - free_memory (int): 空闲内存（字节）
            - vendor (str): 厂商标识，固定为 'Ascend'
        查询失败时返回 {"error": str}。
    """
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
        logger.error("Failed to get NPU-%s info: %s", npu_id, e)
        return {"error": str(e)}


def get_device_info() -> Dict[str, Any]:
    """检测并汇总当前系统的计算设备信息。

    自动识别设备类型（CUDA GPU / 昇腾 NPU / CPU），查询所有可用设备的
    详细信息（型号、显存/内存用量等），并将内存单位从字节转换为 GB。

    Returns:
        Dict[str, Any]: 设备信息字典，结构如下：
            {
                "device": str,       # 设备类型 ('cuda' / 'npu' / 'cpu')
                "count": int,        # 可用设备数量
                "details": List[Dict],  # 每张设备的详细信息列表
                "units": str,        # 内存单位，固定为 'GB'
                "error": str         # （可选）出错时的错误信息
            }

    注意:
        - CUDA 设备信息通过 pynvml 获取，包含利用率；
        - NPU 设备信息通过 torch_npu 获取，不含利用率；
        - 若设备类型不受支持或查询异常，result 中会包含 'error' 字段。
    """
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
            logger.error("Unsupported device type: %s", device)
            result_template[error_key] = f"Unsupported device type: {device}"

    except Exception as e:
        logger.error("Error getting %s device info", device)
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


def check_pcie_cards(device_id="d802", subsystem_id="4000"):
    """检查指定的device_id,subsystem_id的pcie设备是否存在

    Args:
        device_id (str): 要查找的目标设备ID，默认值为"d802"
        subsystem_id (str): 要匹配的目标子系统ID，默认值为"4000"

    Returns:
        tuple: (is_exist, count, bdf_list) - 设备是否存在、总数和BDF列表
        - is_exist (bool): 如果找到至少一个匹配设备则返回True
        - count (int): 匹配设备的总数量
        - bdf_list (list): 匹配设备的BDF地址列表

    其他:
        常用的device id/subsystem id 与设备对应关系
        d500/0110  300I Pro标卡
        d500/0100  300I Duo标卡
        d802/3000  910B4 模组
        d802/3005  910B4-1 模组
        d802/4000  300I A2标卡
        d803/3003  Ascend910 (910C模组)
    """
    try:
        result = subprocess.run(
            ['/usr/bin/lspci', '-d', f':{device_id}'],
            capture_output=True, text=True, check=True
        )

        if not result.stdout.strip():
            return False, 0, []

        device_bdfs = []
        for line in result.stdout.strip().split('\n'):
            if line and ':' in line:
                bdf = line.split()[0]
                device_bdfs.append(bdf)

        count = 0
        matched_bdfs = []
        for bdf in device_bdfs:
            detail_result = subprocess.run(
                ['/usr/bin/lspci', '-vvv', '-s', bdf],
                capture_output=True, text=True, check=True
            )
            if f'Device {subsystem_id}' in detail_result.stdout:
                count += 1
                matched_bdfs.append(bdf)

        return count > 0, count, matched_bdfs

    except subprocess.CalledProcessError as e:
        error_msg = str(e)
        if "command not found" in error_msg or "No such file or directory" in error_msg:
            logger.error("lspci command is not available")
            return False, 0, []
        else:
            logger.error(f"Command execution failed: {error_msg}")
            return False, 0, []

    except FileNotFoundError:
        logger.error("lspci command not found")
        return False, 0, []

    except ValueError as e:
        logger.error(f"Result parsing failed: {str(e)}")
        return False, 0, []

    except Exception as e:
        logger.error(f"Unexpected error occurred: {str(e)}")
        return False, 0, []
