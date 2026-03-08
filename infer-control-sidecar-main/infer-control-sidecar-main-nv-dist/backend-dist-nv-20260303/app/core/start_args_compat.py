# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: core/start_args_compat.py
# Purpose: Parses launcher CLI args with semantics aligned to wings_start.sh.
# Status: Active launcher compatibility parser.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Unknown args should fail fast.
# - Required args and defaults must remain consistent with design contract.
# -----------------------------------------------------------------------------
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _to_bool(raw: str | bool) -> bool:
    if isinstance(raw, bool):
        return raw
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"invalid bool: {raw}")


def _add_bool(parser: argparse.ArgumentParser, flag: str, env_name: str, default: bool) -> None:
    parser.add_argument(
        flag,
        nargs="?",
        const=True,
        default=_to_bool(_env(env_name, str(default).lower())),
        type=_to_bool,
    )


@dataclass(frozen=True)
class LaunchArgs:
    host: str
    port: int
    model_name: str
    model_path: str
    engine: str
    input_length: int
    output_length: int
    config_file: str
    gpu_usage_mode: str
    device_count: int
    model_type: str
    save_path: str
    trust_remote_code: bool
    dtype: str
    kv_cache_dtype: str
    quantization: str
    quantization_param_path: str
    gpu_memory_utilization: float
    enable_chunked_prefill: bool
    block_size: int
    max_num_seqs: int
    seed: int
    enable_expert_parallel: bool
    max_num_batched_tokens: int
    enable_prefix_caching: bool
    enable_speculative_decode: bool
    speculative_decode_model_path: str
    enable_rag_acc: bool
    enable_auto_tool_choice: bool
    distributed: bool
    nnodes: int
    node_rank: int
    head_node_addr: str
    distributed_executor_backend: str

    def to_namespace(self) -> argparse.Namespace:
        return argparse.Namespace(**self.__dict__)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wings-launcher-v4")
    p.add_argument("--host", default=_env("HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=_env_int("PORT", 18000))
    p.add_argument("--model-name", default=_env("MODEL_NAME", ""))
    p.add_argument("--model-path", default=_env("MODEL_PATH", "/weights"))
    p.add_argument("--engine", default=_env("ENGINE", "vllm"))
    p.add_argument("--input-length", type=int, default=_env_int("INPUT_LENGTH", 4096))
    p.add_argument("--output-length", type=int, default=_env_int("OUTPUT_LENGTH", 1024))
    p.add_argument("--config-file", default=_env("CONFIG_FILE", ""))
    p.add_argument("--gpu-usage-mode", default=_env("GPU_USAGE_MODE", "default"))
    p.add_argument("--device-count", type=int, default=_env_int("DEVICE_COUNT", 1))
    p.add_argument("--model-type", default=_env("MODEL_TYPE", ""))
    p.add_argument("--save-path", default=_env("SAVE_PATH", "/opt/wings/outputs"))

    _add_bool(p, "--trust-remote-code", "TRUST_REMOTE_CODE", True)
    p.add_argument("--dtype", default=_env("DTYPE", "auto"))
    p.add_argument("--kv-cache-dtype", default=_env("KV_CACHE_DTYPE", "auto"))
    p.add_argument("--quantization", default=_env("QUANTIZATION", ""))
    p.add_argument("--quantization-param-path", default=_env("QUANTIZATION_PARAM_PATH", ""))
    p.add_argument("--gpu-memory-utilization", type=float, default=_env_float("GPU_MEMORY_UTILIZATION", 0.9))
    _add_bool(p, "--enable-chunked-prefill", "ENABLE_CHUNKED_PREFILL", False)
    p.add_argument("--block-size", type=int, default=_env_int("BLOCK_SIZE", 16))
    p.add_argument("--max-num-seqs", type=int, default=_env_int("MAX_NUM_SEQS", 32))
    p.add_argument("--seed", type=int, default=_env_int("SEED", 0))
    _add_bool(p, "--enable-expert-parallel", "ENABLE_EXPERT_PARALLEL", False)
    p.add_argument("--max-num-batched-tokens", type=int, default=_env_int("MAX_NUM_BATCHED_TOKENS", 4096))
    _add_bool(p, "--enable-prefix-caching", "ENABLE_PREFIX_CACHING", False)

    _add_bool(p, "--enable-speculative-decode", "ENABLE_SPECULATIVE_DECODE", False)
    p.add_argument("--speculative-decode-model-path", default=_env("SPECULATIVE_DECODE_MODEL_PATH", ""))
    _add_bool(p, "--enable-rag-acc", "ENABLE_RAG_ACC", False)
    _add_bool(p, "--enable-auto-tool-choice", "ENABLE_AUTO_TOOL_CHOICE", False)
    _add_bool(p, "--distributed", "DISTRIBUTED", False)

    p.add_argument("--nnodes", type=int, default=_env_int("NNODES", 1))
    p.add_argument("--node-rank", type=int, default=_env_int("NODE_RANK", 0))
    p.add_argument("--head-node-addr", default=_env("HEAD_NODE_ADDR", "127.0.0.1"))
    p.add_argument("--distributed-executor-backend", default=_env("DISTRIBUTED_EXECUTOR_BACKEND", "ray"))
    return p

SUPPORTED_ENGINES = {"vllm", "vllm_ascend", "sglang", "mindie"}


def parse_launch_args(argv: list[str] | None = None) -> LaunchArgs:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.model_name:
        raise ValueError("model_name is required")
    engine = str(args.engine).lower()
    if engine not in SUPPORTED_ENGINES:
        raise ValueError(
            f"unsupported engine '{engine}'; "
            f"supported engines: {sorted(SUPPORTED_ENGINES)}"
        )

    return LaunchArgs(
        host=args.host,
        port=args.port,
        model_name=args.model_name,
        model_path=args.model_path,
        engine=args.engine,
        input_length=args.input_length,
        output_length=args.output_length,
        config_file=args.config_file,
        gpu_usage_mode=args.gpu_usage_mode,
        device_count=args.device_count,
        model_type=args.model_type,
        save_path=args.save_path,
        trust_remote_code=bool(args.trust_remote_code),
        dtype=args.dtype,
        kv_cache_dtype=args.kv_cache_dtype,
        quantization=args.quantization,
        quantization_param_path=args.quantization_param_path,
        gpu_memory_utilization=float(args.gpu_memory_utilization),
        enable_chunked_prefill=bool(args.enable_chunked_prefill),
        block_size=args.block_size,
        max_num_seqs=args.max_num_seqs,
        seed=args.seed,
        enable_expert_parallel=bool(args.enable_expert_parallel),
        max_num_batched_tokens=args.max_num_batched_tokens,
        enable_prefix_caching=bool(args.enable_prefix_caching),
        enable_speculative_decode=bool(args.enable_speculative_decode),
        speculative_decode_model_path=args.speculative_decode_model_path,
        enable_rag_acc=bool(args.enable_rag_acc),
        enable_auto_tool_choice=bool(args.enable_auto_tool_choice),
        distributed=bool(args.distributed),
        nnodes=int(args.nnodes),
        node_rank=int(args.node_rank),
        head_node_addr=str(args.head_node_addr),
        distributed_executor_backend=str(args.distributed_executor_backend),
    )
