#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FastAPI + Transformers LLM Inference (CUDA/NPU) with lifespan startup

Required server arguments (CLI or ENV):
  --model-name / MODEL_NAME   : logical model name to expose in responses
  --model-path / MODEL_PATH   : path to HF model (local dir or repo id)
  --host       / HOST         : bind host (e.g., 0.0.0.0)
  --port       / PORT         : bind port (e.g., 5000)
  --device     / DEVICE       : 'cuda' or 'npu' (no auto-detect; CPU disabled)

Endpoints:
  - POST /v1/chat/completions
  - POST /v1/completions

Features:
  * SSE streaming with anti-buffer headers
  * Default stop strategy (Chat auto-infer; Completion optional)
  * Single-machine multi-GPU (CUDA) via device_map="auto" (unchanged)
  * Minimal concurrency gate (asyncio.Semaphore)
  * Realtime server-side streaming logs (env STREAM_LOG=1)
  * Skip prompt echo + postprocess to strip pseudo special tokens
"""

# --- imports: 每行一个模块，避免一行多导入（便于审阅/差分） ---
import os
import json
import time
import uuid
import threading
import asyncio
import re
import subprocess
import sys
import argparse
from typing import Any, Dict, Iterator, List, Optional, Union, Pattern, Tuple
from contextlib import asynccontextmanager
from dataclasses import dataclass

import torch
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, conint, confloat

from transformers import AutoTokenizer, AutoModelForCausalLM, AutoConfig
from transformers.generation import GenerationConfig
from transformers.generation.streamers import TextIteratorStreamer

from loguru import logger


# =========================
# 必填配置解析（CLI 优先，其次 ENV；缺失则直接报错）
# =========================
@dataclass(frozen=True)
class ServerConfig:
    model_name: str
    model_path: str
    host: str
    port: int
    device: str  # 'cuda' | 'npu'


def _parse_required_config() -> ServerConfig:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--model-name")
    parser.add_argument("--model-path")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--device")  # 'cuda' or 'npu'
    # 忽略未知参数（例如被其他框架注入的）
    args, _ = parser.parse_known_args()

    def need(k_cli: Optional[str], env_key: str, pretty: str) -> str:
        v = k_cli if (k_cli is not None and str(k_cli) != "") else os.getenv(env_key)
        if v is None or str(v) == "":
            raise ValueError(
                f"[fatal] Missing required parameter: {pretty}. "
                f"Pass via CLI --{pretty.replace('_','-')} or ENV {env_key}."
            )
        return str(v)

    model_name = need(args.model_name, "MODEL_NAME", "model_name")
    model_path = need(args.model_path, "MODEL_PATH", "model_path")
    host = need(args.host, "HOST", "host")
    port_str = need(str(args.port) if args.port is not None else None, "PORT", "port")
    device = need(args.device, "DEVICE", "device").lower().strip()

    if device not in ("cuda", "npu"):
        raise ValueError("[fatal] device must be 'cuda' or 'npu' (CPU disabled).")

    try:
        port = int(port_str)
        if port <= 0 or port > 65535:
            raise ValueError
    except Exception as e:
        raise ValueError("[fatal] port must be a valid integer in (1..65535).") from e

    return ServerConfig(
        model_name=model_name,
        model_path=model_path,
        host=host,
        port=port,
        device=device,
    )


CONFIG = _parse_required_config()

# =========================
# 其他可选配置（环境变量可覆盖）
# =========================
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "2"))

# 采样与边界
MAX_NEW_TOKENS_CAP = int(os.getenv("MAX_NEW_TOKENS_CAP", "2048"))
TEMPERATURE_CAP = float(os.getenv("TEMPERATURE_CAP", "2.0"))
TOP_P_CAP = float(os.getenv("TOP_P_CAP", "1.0"))
DEFAULT_MAX_NEW_TOKENS = int(os.getenv("DEFAULT_MAX_NEW_TOKENS", "256"))
DEFAULT_REPETITION_PENALTY = float(os.getenv("DEFAULT_REPETITION_PENALTY", "1.1"))

# 输出展示用逻辑名（对齐为 server 级别必填的 model_name）
DEFAULT_MODEL_NAME_CHAT = CONFIG.model_name
DEFAULT_MODEL_NAME_INSTRUCT = CONFIG.model_name

# 默认停词（JSON 数组或 '||' 分隔）
DEFAULT_STOP_WORDS_ENV = os.getenv("DEFAULT_STOP_WORDS", "")

# CUDA 多卡（仅 CUDA 生效；需要 accelerate）
# 例：MAX_MEMORY='{"cuda:0":"78GiB"}' 或 'cuda:0=78GiB,cuda:1=78GiB'
MAX_MEMORY_ENV = os.getenv("MAX_MEMORY", "")
# 例：NO_SPLIT_MODULE_CLASSES='LlamaDecoderLayer,Block'
NO_SPLIT_MODULE_CLASSES = [s.strip() for s in os.getenv("NO_SPLIT_MODULE_CLASSES", "").split(",") if s.strip()]



# 指定具体设备 id（可选）。例：TORCH_DEVICE='npu:0' 或 'cuda:0'
TORCH_DEVICE = os.getenv("TORCH_DEVICE", "")

# 流式服务端打印开关
STREAM_LOG = int(os.getenv("STREAM_LOG", "0"))

# =========================
# 小工具
# =========================


def _now() -> int:
    """G.FMT.08：函数体换行，避免一行多语句。"""
    return int(time.time())


def _gen_id() -> str:
    """G.FMT.08：函数体换行，避免一行多语句。"""
    return uuid.uuid4().hex


def _sse(line: Union[str, Dict[str, Any]]) -> str:
    """构建 SSE 数据行。G.FMT.08：拆分为多行。"""
    payload = line if isinstance(line, str) else json.dumps(line, ensure_ascii=False)
    return "data: " + payload + "\n\n"


def _parse_max_memory(val: str) -> Optional[Dict[str, str]]:
    """解析 max_memory 配置；支持 JSON 或逗号键值对。"""
    if not val:
        return None
    try:
        if val.strip().startswith("{"):
            obj = json.loads(val)
            if isinstance(obj, dict):
                return obj
    except json.JSONDecodeError as e:
        # JSON 解析失败，继续尝试逗号分隔格式
        logger.debug("Failed to parse max_memory as JSON, trying comma-separated format: %s", e)
    mm: Dict[str, str] = {}
    for part in val.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            mm[k.strip()] = v.strip()
    return mm or None


def _default_stop_words() -> List[str]:
    """从环境变量解析默认停词。"""
    raw = DEFAULT_STOP_WORDS_ENV.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            arr = json.loads(raw)
            if isinstance(arr, list):
                return [str(x) for x in arr]
        except Exception:
            return []
    return [s for s in raw.split("||") if s]


def _run_nvidia_smi() -> Optional[str]:
    """运行 nvidia-smi 命令并返回输出"""
    nvidia_smi_path = "/usr/bin/nvidia-smi"
    try:
        out = subprocess.run(
            [nvidia_smi_path, "--query-gpu=index,name,memory.total,memory.used", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception as e:
        logger.warning("Failed to run nvidia-smi: %s", e)
    return None


def _run_npu_commands() -> Optional[str]:
    """运行 NPU 相关命令并返回输出"""
    npu_commands = [
        ["/usr/local/Ascend/driver/tools/npu-smi", "info"],
        ["/usr/local/Ascend/driver/tools/ascend-dmi", "-l"]
    ]
    
    for cmd in npu_commands:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            if out.returncode == 0:
                return f"{' '.join(cmd)}:\n{out.stdout.strip()}"
        except Exception as e:
            logger.warning("Failed to run %s: %s", cmd, e)
    return None


def _device_summary(backend: str):
    """打印设备简要信息（CUDA/NPU）。"""
    try:
        if backend == "cuda":
            output = _run_nvidia_smi()
            if output:
                logger.info("[startup] nvidia-smi:\n%s", output)
        elif backend == "npu":
            output = _run_npu_commands()
            if output:
                logger.info("[startup] %s", output)
    except Exception as e:
        logger.warning("Failed to get device summary: %s", e)


def _log_stream(trace_id: str, tag: str, chunk: str):
    """按需输出流式预览日志；避免一行多语句。"""
    if not STREAM_LOG:
        return
    preview = chunk.replace("\n", "\\n")
    if len(preview) > 200:
        preview = preview[:200] + "…"
    logger.info(f"[stream][{trace_id}][{tag}] {preview}", flush=True)


# 伪 special token / 噪声前缀清洗
_PREFIX_GARBAGE_PAT = re.compile(
    r"^(?:<\uFF5C?begin[_▁ ]of[_▁ ]sentence\uFF5C?>|<\|begin_of_text\|>|<\|beginoftext\|>|<\|BOS\|>)",
    re.IGNORECASE,
)


def _postprocess_text(s: str) -> str:
    s = _PREFIX_GARBAGE_PAT.sub("", s)
    return s


# =========================
# Pydantic Schemas
# =========================
class ChatMessage(BaseModel):
    role: str
    content: str


class CreateChatCompletion(BaseModel):
    model: Optional[str] = Field(default=DEFAULT_MODEL_NAME_CHAT)
    messages: List[ChatMessage]
    temperature: confloat(ge=0, le=TEMPERATURE_CAP) = 1.0
    top_p: confloat(gt=0, le=TOP_P_CAP) = 1.0
    n: conint(ge=1, le=1) = 1
    max_tokens: Optional[conint(ge=1, le=MAX_NEW_TOKENS_CAP)] = None
    stream: bool = False
    stop: Optional[List[str]] = None  # 可缺省（默认策略）


class CreateCompletion(BaseModel):
    model: Optional[str] = Field(default=DEFAULT_MODEL_NAME_INSTRUCT)
    prompt: Union[str, List[str]]
    max_tokens: conint(ge=1, le=MAX_NEW_TOKENS_CAP) = DEFAULT_MAX_NEW_TOKENS
    temperature: confloat(ge=0, le=TEMPERATURE_CAP) = 1.0
    top_p: confloat(gt=0, le=TOP_P_CAP) = 1.0
    n: conint(ge=1, le=1) = 1
    stream: bool = False
    stop: Optional[List[str]] = None  # 可缺省（默认不开启，除非配置/请求提供）


# =========================
# 并发闸门 + 取消
# =========================
_sema = asyncio.Semaphore(MAX_CONCURRENCY)


class CancelFlag:
    """线程间取消标记。G.FMT.08：方法体换行。"""

    def __init__(self):
        self._e = threading.Event()

    def set(self):
        self._e.set()

    def is_set(self) -> bool:
        return self._e.is_set()


# =========================
# Runtime 单例（固定使用 CONFIG.device，不做自动探测）
# =========================
class _Runtime:
    tokenizer = None
    model = None
    device = None
    backend = CONFIG.device  # 'cuda' | 'npu'
    device_map_used = None


runtime = _Runtime()


def _load_tokenizer() -> AutoTokenizer:
    return AutoTokenizer.from_pretrained(CONFIG.model_path, use_fast=False, trust_remote_code=True)


def _cuda_supports_bf16() -> bool:
    try:
        # 新版 PyTorch 直接有
        if hasattr(torch.cuda, "is_bf16_supported"):
            return torch.cuda.is_available() and torch.cuda.is_bf16_supported()
        # 兜底：Ampere(8.0)/Ada(8.9) 及以上原生支持
        major, _ = torch.cuda.get_device_capability()
        return torch.cuda.is_available() and (major >= 8)
    except (RuntimeError, AttributeError) as e:
        # 具体化异常类型
        logger.debug("CUDA bf16 capability check failed: %s", e)
        return False


def _resolve_torch_dtype_for_cuda_from_config_or_default(model_path: str) -> torch.dtype:
    """
    1) 先用 AutoConfig 解析 config 里的 torch_dtype；
    2) 若是 None/'auto'/缺失 → 尝试 bf16；不支持则回落 fp16；
    3) 若解析出 float16/float32/bfloat16 → 直接用，但 bfloat16 在不支持卡上仍回落到 fp16。
    """
    try:
        cfg = AutoConfig.from_pretrained(model_path, trust_remote_code=True)
        raw = getattr(cfg, "torch_dtype", None)  # 可能是 torch.dtype 或 str 或 None
        if isinstance(raw, torch.dtype):
            dt = raw
        elif isinstance(raw, str):
            s = raw.strip().lower()
            dt = {"bfloat16": torch.bfloat16, "bf16": torch.bfloat16,
                  "float16": torch.float16, "fp16": torch.float16,
                  "float32": torch.float32, "fp32": torch.float32}.get(s)
        else:
            dt = None
    except (OSError, ValueError, ImportError) as e:
        # 具体化异常类型
        logger.warning("[dtype] AutoConfig load failed: %s", e)
        dt = None

    # 统一处理 auto/None 的情况
    if dt is None or (isinstance(dt, str) and dt == "auto"):
        if _cuda_supports_bf16():
            logger.info("[dtype] CUDA: config auto/none -> choose bfloat16")
            return torch.bfloat16
        else:
            logger.info("[dtype] CUDA: config auto/none -> choose float16 (bf16 not supported)")
            return torch.float16

    # 显式要求 bfloat16 但不支持，就回退 fp16
    if dt == torch.bfloat16 and not _cuda_supports_bf16():
        logger.info("[dtype] CUDA: config requires bfloat16, but device not supported -> fallback float16")
        return torch.float16

    return dt


def _load_model_cuda(tokenizer):
    forced = _resolve_torch_dtype_for_cuda_from_config_or_default(CONFIG.model_path)

    load_kwargs = dict(
        trust_remote_code=True,
        device_map="auto",
        low_cpu_mem_usage=True,
        torch_dtype=forced,
    )
    mm = _parse_max_memory(MAX_MEMORY_ENV)
    if mm:
        load_kwargs["max_memory"] = mm

    if NO_SPLIT_MODULE_CLASSES:
        load_kwargs["no_split_module_classes"] = NO_SPLIT_MODULE_CLASSES

    model = AutoModelForCausalLM.from_pretrained(CONFIG.model_path, **load_kwargs)
    runtime.device_map_used = "auto"
    return model


def _list_all_npu_devices() -> List[int]:
    """
    仅依据 torch.npu.device_count() 构建设备列表 [0..n-1]。
    不读取任何环境变量。
    """
    try:
        import torch_npu  # noqa: F401
    except Exception:
        logger.warning("torch_npu not imported before counting devices (will still try).")
    try:
        n = getattr(torch.npu, "device_count", lambda: 1)()
    except Exception as e:
        logger.warning("torch.npu.device_count() failed: %s; fallback to 1 device.", e)
        n = 1
    n = max(int(n), 1)
    return list(range(n))


def _setup_npu_environment():
    """设置NPU环境变量和依赖检查"""
    original_cuda_visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES")
    
    # 避免 HF/accelerate 误走 CUDA
    if "CUDA_VISIBLE_DEVICES" not in os.environ:
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
    
    multi_card = False
    dev_ids = _list_all_npu_devices()
    
    if len(dev_ids) > 1:
        multi_card = False  # 先设置为默认值
        try:
            import accelerate  # noqa: F401
        except ImportError as e:
            logger.warning("`accelerate` not available -> fallback to single NPU. reason: %s", e)
        except Exception as e:
            logger.warning("Unexpected error checking accelerate: %s", e)
        else:
            # 只有在 import 成功时才设置为 True
            multi_card = True

    # 预加载 torch_npu
    try:
        import torch_npu  # noqa: F401
        logger.debug("torch_npu imported successfully")
    except ImportError as e:
        logger.warning("Failed to import torch_npu; NPU functionality may be limited: %s", e)
    except Exception as e:
        logger.warning("Unexpected error importing torch_npu: %s", e)
    
    return dev_ids, multi_card


def _build_npu_load_kwargs(torch_dtype, dev_ids, multi_card):
    """构建NPU模型加载参数"""
    load_kwargs = dict(
        trust_remote_code=True,
        low_cpu_mem_usage=True,
        torch_dtype=torch_dtype,
    )
    
    if multi_card:
        load_kwargs["device_map"] = "auto"
        # 处理显存限制
        mm = _parse_max_memory(MAX_MEMORY_ENV)
        if mm:
            load_kwargs["max_memory"] = mm
        else:
            limit_gib = os.getenv("NPU_MAX_MEMORY_GIB")
            if limit_gib and limit_gib.isdigit():
                per = f"{limit_gib}GiB"
                load_kwargs["max_memory"] = {f"npu:{i}": per for i in dev_ids}
    
    return load_kwargs


def _load_model_with_auto_mapping(model_path, load_kwargs):
    """尝试使用自动设备映射加载模型"""
    try:
        model = AutoModelForCausalLM.from_pretrained(model_path, **load_kwargs)
    except (OSError, ValueError, RuntimeError) as e:
        error_msg = str(e)[:500]
        logger.error(
            "[startup][NPU] device_map='auto' failed, fallback to single-card. reason: %s",
            error_msg
        )
        return None
    
    # 记录设备映射信息（非关键操作，不影响主流程）
    _safe_log_device_mapping(model)
    return model


def _safe_log_device_mapping(model):
    """安全地记录设备映射信息"""
    try:
        device_map_info = getattr(model, "hf_device_map", None)
        if device_map_info:
            truncated_info = str(device_map_info)
            if len(truncated_info) > 1000:
                truncated_info = truncated_info[:1000] + "...(truncated)"
            logger.info("[startup] hf_device_map: %s", truncated_info)
    except Exception as e:
        logger.debug("Inspect hf_device_map failed (non-critical): %s", e)


def _load_model_single_npu(model_path, load_kwargs_fallback):
    """单卡NPU模型加载"""
    idx = os.getenv("LOCAL_RANK") or os.getenv("DEVICE_ID") or "0"
    dev = TORCH_DEVICE if TORCH_DEVICE.startswith("npu") else f"npu:{idx}"
    
    model = AutoModelForCausalLM.from_pretrained(model_path, **load_kwargs_fallback)
    
    try:
        try:
            device_index = int(dev.split(":", 1)[1])
            torch.npu.set_device(device_index)
        except (ValueError, IndexError, RuntimeError) as e:
            logger.debug("torch.npu.set_device(%s) failed: %s (may continue on default device)", dev, e)
        model = model.to(dev)
        logger.info("[startup][NPU] fallback single-card loaded on device: %s", dev)
    except Exception as e:
        raise RuntimeError(f"move model to {dev} failed: {e}") from e
    
    return model, dev


def _load_model_npu(tokenizer: AutoTokenizer) -> AutoModelForCausalLM:
    """在NPU设备上加载模型"""
    # 默认用 fp16（310P/310P3 更稳）
    torch_dtype = torch.float16
    
    # 设置环境和检查依赖
    dev_ids, multi_card = _setup_npu_environment()
    
    # 构建加载参数
    load_kwargs = _build_npu_load_kwargs(torch_dtype, dev_ids, multi_card)
    
    # 尝试多卡自动映射加载
    if multi_card:
        model = _load_model_with_auto_mapping(CONFIG.model_path, load_kwargs)
        if model is not None:
            runtime.device_map_used = "auto"
            return model
    
    # 回退到单卡加载
    load_kwargs_fallback = dict(
        trust_remote_code=True,
        low_cpu_mem_usage=True,
        torch_dtype=torch_dtype,
    )
    
    try:
        result = _load_model_single_npu(CONFIG.model_path, load_kwargs_fallback)
        if result is None:
            raise RuntimeError("Failed to load model on single NPU: _load_model_single_npu returned None")
        
        model, dev = result
        if model is None:
            raise RuntimeError("Failed to load model on single NPU: model is None")
            
    except Exception as e:
        logger.error("[startup][NPU] Failed to load model on single NPU: %s", e, exc_info=True)
        raise RuntimeError(f"Failed to load model on single NPU: {e}") from e


    runtime.device_map_used = None
    return model


# =========================
# lifespan（启动/关闭）
# =========================
@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    t0 = time.time()
    logger.info(f"[startup] backend selected: {runtime.backend}")

    tokenizer = _load_tokenizer()

    if runtime.backend == "cuda":
        model = _load_model_cuda(tokenizer)
    elif runtime.backend == "npu":
        model = _load_model_npu(tokenizer)
    else:
        raise RuntimeError("Invalid backend (must be 'cuda' or 'npu').")

    try:
        model.generation_config = GenerationConfig.from_pretrained(CONFIG.model_path)
    except (OSError, ValueError, ImportError) as e:
        logger.warning(
            "Failed to load generation config from %s: %s. Using default config.",
            CONFIG.model_path, e
        )
        # 可以在这里设置默认的生成配置
        model.generation_config = GenerationConfig()
    model.eval()

    runtime.tokenizer = tokenizer
    runtime.model = model
    try:
        runtime.device = str(next(model.parameters()).device)
    except Exception:
        runtime.device = "unknown"

    _device_summary(runtime.backend)
    logger.info(
        f"[startup] model loaded in {time.time()-t0:.2f}s | device={runtime.device} | "
        f"dir={CONFIG.model_path} | device_map={runtime.device_map_used}"
    )

    try:
        yield
    finally:
        logger.info("[shutdown] server is stopping…")


# =========================
# FastAPI 应用（使用 lifespan）
# =========================
app = FastAPI(title=f"LLM Inference ({CONFIG.device.upper()})", version="0.7.0", lifespan=lifespan)


# =========================
# 停止词工具
# =========================
def _make_stop_regex(stop_list: Optional[List[str]]) -> Optional[re.Pattern]:
    # 空列表或 None：不启用停止词，直接返回 None（保留原始行为）
    if not stop_list:
        return None
    escaped = [re.escape(s) for s in stop_list if s]
    if not escaped:
        return None
    return re.compile("|".join(escaped))


def _infer_chat_default_stops(messages: List[Dict[str, str]]) -> List[str]:
    return ["\nUser:", "\n用户："]


def _resolve_stops_for_chat(user_provided: Optional[List[str]], messages: List[Dict[str, str]]) -> List[str]:
    if user_provided and len(user_provided) > 0:
        return user_provided
    env_default = _default_stop_words()
    if env_default:
        return env_default
    return _infer_chat_default_stops(messages)


def _resolve_stops_for_completion(user_provided: Optional[List[str]]) -> List[str]:
    if user_provided and len(user_provided) > 0:
        return user_provided
    env_default = _default_stop_words()
    return env_default or []  # Completion 默认不开启


# =========================
# SSE 块构造
# =========================
def chat_head(model_name: str):
    return {
        "id": _gen_id(),
        "object": "chat.completion.chunk",
        "created": _now(),
        "model": model_name,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }


def chat_delta(model_name: str, content: str):
    return {
        "id": _gen_id(),
        "object": "chat.completion.chunk",
        "created": _now(),
        "model": model_name,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }


def chat_tail(model_name: str):
    return {
        "id": _gen_id(),
        "object": "chat.completion.chunk",
        "created": _now(),
        "model": model_name,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }


def cmpl_lead(model_name: str, idx: int):
    return {
        "id": _gen_id(),
        "object": "text_completion",
        "created": _now(),
        "model": model_name,
        "choices": [{"index": idx, "text": "\n\n", "logprobs": None, "finish_reason": None}],
    }


def cmpl_delta(model_name: str, idx: int, text: str):
    return {
        "id": _gen_id(),
        "object": "text_completion",
        "created": _now(),
        "model": model_name,
        "choices": [{"index": idx, "text": text, "logprobs": None, "finish_reason": None}],
    }


def cmpl_tail(model_name: str, idx: int, reason: str):
    return {
        "id": _gen_id(),
        "object": "text_completion",
        "created": _now(),
        "model": model_name,
        "choices": [{"index": idx, "text": "", "logprobs": None, "finish_reason": reason}],
    }


# =========================
# 统一生成参数映射
# =========================
def _build_gen_kwargs(max_new_tokens: Optional[int], temperature: float, top_p: float) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "temperature": float(temperature),
        "top_p": float(top_p),
        "repetition_penalty": float(DEFAULT_REPETITION_PENALTY),
        "do_sample": True if temperature > 0 else False,
    }
    kwargs["max_new_tokens"] = int(max_new_tokens) if max_new_tokens else DEFAULT_MAX_NEW_TOKENS
    return kwargs


# =========================
# Chat 适配（messages → chat_template 或回退模板）
# =========================

@dataclass
class ChatStreamRequest:
    messages: List[Dict[str, str]]
    model_name: str
    stop_list: Optional[List[str]]
    gen_kwargs: Dict[str, Any]
    cancel: CancelFlag
    trace_id: str


def _prepare_chat_prompt(messages: List[Dict[str, str]]) -> str:
    """准备聊天提示词"""
    try_template = hasattr(runtime.tokenizer, "apply_chat_template")
    if try_template:
        return runtime.tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=False
        )
    else:
        prompt_parts = [f"{m['role']}：{m['content']}" for m in messages]
        return "\n".join(prompt_parts) + "\nassistant："


def _process_stream_text(
    text: str, 
    stop_pat: Optional[Pattern], 
    request: ChatStreamRequest
) -> Tuple[str, bool]:
    """处理流式文本，检查停止条件"""
    stopped = False
    
    if not text:
        return text, stopped
        
    if stop_pat:
        m = stop_pat.search(text)
        if m:
            text = text[:m.start()]
            stopped = True
            
    text = _postprocess_text(text)
    return text, stopped


def _handle_stream_generation(
    prompt: str,
    stop_pat: Optional[Pattern],
    request: ChatStreamRequest
) -> Iterator[Union[str, Dict[str, Any]]]:
    """处理流式生成过程"""
    # 准备输入
    inputs = runtime.tokenizer(prompt, return_tensors="pt").to(runtime.model.device)
    
    # 创建流式处理器
    streamer = TextIteratorStreamer(
        runtime.tokenizer,
        skip_prompt=True,
        decode_kwargs={"skip_special_tokens": True},
    )
    
    # 启动生成线程
    th = threading.Thread(
        target=runtime.model.generate,
        kwargs=dict(inputs, streamer=streamer, **request.gen_kwargs),
        daemon=True,
    )
    th.start()
    
    try:
        # 处理流式输出
        for text in streamer:
            if request.cancel.is_set():
                break
                
            processed_text, stopped = _process_stream_text(text, stop_pat, request)
            
            if processed_text:
                _log_stream(request.trace_id, "chat", processed_text)
                yield chat_delta(request.model_name, processed_text)
                
            if stopped:
                break
    finally:
        th.join(timeout=1.0)


def _chat_stream(request: ChatStreamRequest) -> Iterator[Union[str, Dict[str, Any]]]:
    """聊天流式处理主函数"""
    # 返回聊天头部
    yield chat_head(request.model_name)

    # 准备停止条件和提示词
    stops = _resolve_stops_for_chat(request.stop_list, request.messages)
    stop_pat = _make_stop_regex(stops)
    prompt = _prepare_chat_prompt(request.messages)

    # 处理流式生成
    yield from _handle_stream_generation(prompt, stop_pat, request)

    # 返回聊天尾部
    yield chat_tail(request.model_name)


# =========================
# Completion 适配（prompt(s) → generate）
# =========================
@dataclass
class CompletionStreamRequest:
    """Completion流式请求参数"""
    prompts: List[str]
    model_name: str
    stop_list: Optional[List[str]]
    gen_kwargs: Dict[str, Any]
    cancel: CancelFlag
    trace_id: str


def _prepare_completion_prompt(prompt: str) -> str:
    """准备Completion提示词"""
    if hasattr(runtime.tokenizer, "apply_chat_template"):
        return runtime.tokenizer.apply_chat_template(
            [
                {"role": "system", "content": "只用一句话回答，不要解释。"},
                {"role": "user", "content": prompt}
            ],
            add_generation_prompt=True,
            tokenize=False,
        )
    else:
        return prompt


def _process_completion_stream_text(
    text: str,
    stop_pat: Optional[Pattern],
    request: CompletionStreamRequest,
    prompt_idx: int
) -> Tuple[str, bool]:
    """处理Completion流式文本，检查停止条件"""
    stopped = False
    
    if not text:
        return text, stopped
        
    if stop_pat:
        m = stop_pat.search(text)
        if m:
            text = text[:m.start()]
            stopped = True
            
    text = _postprocess_text(text)
    if text:
        _log_stream(request.trace_id, "cmpl", text)
    return text, stopped


def _handle_single_prompt_generation(
    prompt: str,
    prompt_idx: int,
    stop_pat: Optional[Pattern],
    request: CompletionStreamRequest
) -> Iterator[Union[str, Dict[str, Any]]]:
    """处理单个提示词的流式生成"""
    # 准备输入
    prompt_str = _prepare_completion_prompt(prompt)
    inputs = runtime.tokenizer(prompt_str, return_tensors="pt").to(runtime.model.device)
    
    # 创建流式处理器
    streamer = TextIteratorStreamer(
        runtime.tokenizer,
        skip_prompt=True,
        decode_kwargs={"skip_special_tokens": True},
    )
    
    # 启动生成线程
    th = threading.Thread(
        target=runtime.model.generate,
        kwargs=dict(inputs, streamer=streamer, **request.gen_kwargs),
        daemon=True,
    )
    th.start()

    try:
        # 处理流式输出
        for text in streamer:
            if request.cancel.is_set():
                break
                
            processed_text, stopped = _process_completion_stream_text(
                text, stop_pat, request, prompt_idx
            )
            
            if processed_text:
                yield cmpl_delta(request.model_name, prompt_idx, processed_text)
                
            if stopped:
                break
    finally:
        th.join(timeout=1.0)


def _process_all_prompts(
    request: CompletionStreamRequest,
    stop_pat: Optional[Pattern]
) -> Iterator[Union[str, Dict[str, Any]]]:
    """处理所有提示词的流式生成"""
    for idx, prompt in enumerate(request.prompts):
        # 返回提示词头部
        yield cmpl_lead(request.model_name, idx)
        
        # 处理单个提示词的生成
        yield from _handle_single_prompt_generation(prompt, idx, stop_pat, request)
        
        # 返回提示词尾部
        yield cmpl_tail(request.model_name, idx, "stop")
    
    # 返回完成标记
    yield "[DONE]"


def _completion_stream(request: CompletionStreamRequest) -> Iterator[Union[str, Dict[str, Any]]]:
    """Completion流式处理主函数"""
    # 准备停止条件
    stops = _resolve_stops_for_completion(request.stop_list)
    stop_pat = _make_stop_regex(stops)
    
    # 处理所有提示词
    yield from _process_all_prompts(request, stop_pat)


# =========================
# 路由：/v1/chat/completions
# =========================
def _build_chat_response_data(model_name: str, text: str) -> Dict[str, Any]:
    """构建聊天完成的响应数据"""
    return {
        "id": _gen_id(),
        "object": "chat.completion",
        "created": _now(),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text
                },
                "finish_reason": "stop"
            }
        ],
    }


@dataclass
class ChatResponseParams:
    """聊天响应参数封装"""
    messages: List[Dict[str, Any]]
    model_name: str
    req_stop: Optional[List[str]]
    gen_kwargs: Dict[str, Any]
    cancel: CancelFlag
    trace_id: str


def _create_chat_stream_generator(
    request: ChatResponseParams
) -> Iterator[str]:
    """创建聊天流式生成器"""
    if STREAM_LOG:
        logger.info(f"[req][{request.trace_id}] chat stream start", flush=True)
    
    try:
        for evt in _chat_stream(
            ChatStreamRequest(
                messages=request.messages,
                model_name=request.model_name,
                stop_list=request.req_stop,
                gen_kwargs=request.gen_kwargs,
                cancel=request.cancel,
                trace_id=request.trace_id,
            )
        ):
            try:
                yield _sse(evt)
            except Exception:
                request.cancel.set()
                break
        yield _sse("[DONE]")
    finally:
        request.cancel.set()
        if STREAM_LOG:
            logger.info(f"[req][{request.trace_id}] chat stream end", flush=True)


def _handle_streaming_chat_response(
    request: ChatResponseParams
) -> StreamingResponse:
    """处理流式聊天响应"""
    return StreamingResponse(
        _create_chat_stream_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@dataclass
class ChatRequestParams:
    """聊天请求参数封装"""
    messages: List[Dict[str, Any]]
    model_name: str
    req_stop: Optional[List[str]]
    gen_kwargs: Dict[str, Any]
    cancel: CancelFlag
    trace_id: str


def _handle_non_streaming_chat_response(request: ChatRequestParams) -> JSONResponse:
    """处理非流式聊天响应"""
    t_start = time.time()
    full: List[str] = []
    
    for evt in _chat_stream(
        ChatStreamRequest(
            messages=request.messages,
            model_name=request.model_name,
            stop_list=request.req_stop,
            gen_kwargs=request.gen_kwargs,
            cancel=request.cancel,
            trace_id=request.trace_id,
        )
    ):
        if isinstance(evt, dict):
            ch = evt.get("choices", [{}])[0].get("delta", {})
            if "content" in ch:
                full.append(ch["content"])
    
    text = "".join(full)
    
    if STREAM_LOG:
        logger.info(
            f"[req][{request.trace_id}] chat non-stream len={len(text)} "
            f"time={time.time()-t_start:.2f}s", 
            flush=True
        )
    
    response_data = _build_chat_response_data(request.model_name, text)
    return JSONResponse(response_data)


def _process_chat_request(
    req: CreateChatCompletion,
    cancel: CancelFlag,
    trace_id: str
):
    """处理聊天请求的核心逻辑"""
    messages = [m.model_dump() for m in req.messages]
    model_name = req.model or DEFAULT_MODEL_NAME_CHAT
    gen_kwargs = _build_gen_kwargs(req.max_tokens, req.temperature, req.top_p)

    if req.stream:
        params = ChatResponseParams(
            messages=messages,
            model_name=model_name,
            req_stop=req.stop,
            gen_kwargs=gen_kwargs,
            cancel=cancel,
            trace_id=trace_id,
        )
        return _handle_streaming_chat_response(params)
    else:
        params = ChatRequestParams(
            messages=messages,
            model_name=model_name,
            req_stop=req.stop,
            gen_kwargs=gen_kwargs,
            cancel=cancel,
            trace_id=trace_id,
        )
        return _handle_non_streaming_chat_response(params)


@app.post("/v1/chat/completions")
async def chat_completions(req: CreateChatCompletion, request: Request):
    """聊天完成接口主函数"""
    if runtime.model is None or runtime.tokenizer is None:
        raise HTTPException(500, "Model not loaded")
    if req.max_tokens and req.max_tokens > MAX_NEW_TOKENS_CAP:
        raise HTTPException(400, f"max_tokens must be <= {MAX_NEW_TOKENS_CAP}")

    await _sema.acquire()
    cancel = CancelFlag()
    trace_id = uuid.uuid4().hex
    
    try:
        return _process_chat_request(req, cancel, trace_id)
    finally:
        _sema.release()


# =========================
# 路由：/v1/completions
# =========================
def _build_completion_request(
    req: CreateCompletion,
    cancel: CancelFlag,
    trace_id: str
) -> Tuple[List[str], str, Dict[str, Any]]:
    """构建Completion请求参数"""
    prompts: List[str] = req.prompt if isinstance(req.prompt, list) else [req.prompt]
    model_name = req.model or DEFAULT_MODEL_NAME_INSTRUCT
    gen_kwargs = _build_gen_kwargs(req.max_tokens, req.temperature, req.top_p)
    return prompts, model_name, gen_kwargs


@dataclass
class CompletionResponseParams:
    """Completion响应参数封装"""
    prompts: List[str]
    model_name: str
    req_stop: Optional[List[str]]
    gen_kwargs: Dict[str, Any]
    cancel: CancelFlag
    trace_id: str


def _create_stream_generator(request: CompletionResponseParams) -> Iterator[str]:
    """创建流式生成器"""
    if STREAM_LOG:
        logger.info(f"[req][{request.trace_id}] cmpl stream start", flush=True)
    
    try:
        for evt in _completion_stream(
            CompletionStreamRequest(
                prompts=request.prompts,
                model_name=request.model_name,
                stop_list=request.req_stop,
                gen_kwargs=request.gen_kwargs,
                cancel=request.cancel,
                trace_id=request.trace_id,
            )
        ):
            try:
                yield _sse(evt)
            except Exception:
                request.cancel.set()
                break
    finally:
        request.cancel.set()
        if STREAM_LOG:
            logger.info(f"[req][{request.trace_id}] cmpl stream end", flush=True)


def _create_streaming_response(request: CompletionResponseParams) -> StreamingResponse:
    """创建流式响应"""
    return StreamingResponse(
        _create_stream_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@dataclass
class CompletionResponseParams:
    """Completion响应参数封装"""
    prompts: List[str]
    model_name: str
    req_stop: Optional[List[str]]
    gen_kwargs: Dict[str, Any]
    cancel: CancelFlag
    trace_id: str


def _process_single_prompt(
    prompt: str,
    prompt_idx: int,
    request: CompletionResponseParams
) -> Dict[str, Any]:
    """处理单个提示词的非流式响应"""
    text_parts: List[str] = []
    
    for evt in _completion_stream(
        CompletionStreamRequest(
            prompts=[prompt],
            model_name=request.model_name,
            stop_list=request.req_stop,
            gen_kwargs=request.gen_kwargs,
            cancel=request.cancel,
            trace_id=request.trace_id,
        )
    ):
        if isinstance(evt, dict):
            ch = evt.get("choices", [{}])[0]
            if "text" in ch:
                text_parts.append(ch["text"])
    
    text = "".join(text_parts)
    
    if STREAM_LOG:
        logger.info(f"[req][{request.trace_id}] cmpl non-stream idx={prompt_idx} len={len(text)}", flush=True)
    
    return {
        "index": prompt_idx, 
        "text": text, 
        "logprobs": {}, 
        "finish_reason": "stop"
    }


def _create_non_streaming_response(
    request: CompletionResponseParams
) -> JSONResponse:
    """创建非流式响应"""
    choices: List[Dict[str, Any]] = []
    
    for idx, prompt in enumerate(request.prompts):
        choice = _process_single_prompt(prompt, idx, request)
        choices.append(choice)
    
    return JSONResponse({
        "id": _gen_id(),
        "object": "text_completion", 
        "created": _now(),
        "model": request.model_name,
        "choices": choices
    })


def _handle_completion_request(
    req: CreateCompletion,
    cancel: CancelFlag,
    trace_id: str
):
    """处理Completion请求的核心逻辑"""
    prompts, model_name, gen_kwargs = _build_completion_request(req, cancel, trace_id)

    params = CompletionResponseParams(
        prompts=prompts,
        model_name=model_name,
        req_stop=req.stop,
        gen_kwargs=gen_kwargs,
        cancel=cancel,
        trace_id=trace_id,
    )

    if req.stream:
        return _create_streaming_response(params)
    else:
        return _create_non_streaming_response(params)



@app.post("/v1/completions")
async def completions(req: CreateCompletion, request: Request):
    """Completion接口主函数"""
    # 参数验证
    if runtime.model is None or runtime.tokenizer is None:
        raise HTTPException(500, "Model not loaded")
    if req.max_tokens > MAX_NEW_TOKENS_CAP:
        raise HTTPException(400, f"max_tokens must be <= {MAX_NEW_TOKENS_CAP}")

    # 获取信号量
    await _sema.acquire()
    cancel = CancelFlag()
    trace_id = uuid.uuid4().hex
    
    try:
        return _handle_completion_request(req, cancel, trace_id)
    finally:
        _sema.release()


# =========================
# 健康检查（精简版 /health：仅返回 200）
# =========================
@app.get("/health")
async def health():
    # 仅保留这一个条件：模型与分词器均已加载才算就绪
    ready = (runtime.model is not None) and (runtime.tokenizer is not None)
    payload = {"status": ready}
    return JSONResponse(content=payload, status_code=200 if ready else 503)


# =========================
# 入口
# =========================
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=CONFIG.host, port=CONFIG.port, reload=False)
