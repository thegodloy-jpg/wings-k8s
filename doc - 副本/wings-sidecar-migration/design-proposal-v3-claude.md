# Wings Sidecar 迁移方案 v3-claude（审查优化版）

> 基于 `design-proposal-v3.md`，经源码交叉验证后修订。  
> 审查范围：`wings_start.sh`(615行)、`wings.py`(314行)、`proxy/gateway.py`(750行)、  
> `proxy/health.py`(669行)、`proxy/health_service.py`(110行)、`proxy/settings.py`(136行)、  
> `wings_proxy.py`、`core/config_loader.py`(1163行)、`engines/vllm_adapter.py`(631行)、  
> 以及 sidecar 现有 `main.py`、`settings.py`、`engine_manager.py`、`command_builder.py`。

---

## 0. 原 v3 文档审查发现（共 12 项）

| # | 类别 | 问题描述 | 影响程度 |
|---|------|---------|---------|
| 1 | **架构缺陷** | PID 健康检测不可用：`health.py` 通过 `BACKEND_PID_FILE` + `/proc/<pid>` 判定引擎存活，但 sidecar 与引擎位于**不同容器**，PID 空间隔离，此机制完全失效 | **高** |
| 2 | **架构缺陷** | 进程模型未定义：文档提出 3 个端口（17000/18000/19000）对应 3 个服务，但未说明 `main.py` 自身监听哪个端口、谁来启动 proxy 和 health_server | **高** |
| 3 | **接口不兼容** | `proxy/settings.py` 在模块级调用 `argparse.parse_args()`（第27行），sidecar 无 CLI 参数，直接 import 将崩溃 | **高** |
| 4 | **逻辑遗漏** | `LaunchArgs` dataclass 只列了 5 个字段，实际 `wings.py parse_arguments()` 定义了 23+ 个参数 | **中** |
| 5 | **概念混淆** | `WINGS_START_ARGS` 被称为"与 `wings_start.sh` 对齐"，但 `wings_start.sh` 从未使用此变量——它通过 `$1 $2 $3...` 接收参数。`WINGS_START_ARGS` 是 sidecar 新增概念 | **中** |
| 6 | **端口歧义** | `settings.py` 现有 `ENGINE_PORT=8000` + `WINGS_PORT=9000`，v3 方案改为 `17000+18000+19000` 但未说明向后兼容策略和变量名映射关系 | **中** |
| 7 | **映射缺失** | 未提及 `model_name` → `served_model_name` 的参数名映射（由 `engine_parameter_mapping.json` 完成），这是命令正确性的关键 | **中** |
| 8 | **复用细节缺失** | 列出了 `config_loader.py` 但未说明哪些函数直接复用、哪些需要改造（v2 文档有详细标注，v3 反而退步） | **中** |
| 9 | **端口派生不完整** | `PortPlan.backend_port` 注释写"固定 17000 when proxy enabled"，但缺少 `enable_proxy=False` 时值为 `PORT or 18000` 的完整逻辑 | **低** |
| 10 | **proxy 导入路径** | "直接复用 `wings/wings/proxy`" 但所有 import 都是 `from wings.proxy import ...`，需要全部改为 `from app.proxy import ...` | **低** |
| 11 | **默认值偏差** | 文档称 `MODEL_PATH=/weights`，但 sidecar 现有 `MODEL_PATH=/models`，wings.py 的 `model_path` 参数无默认值（为 `None`，由 config_loader 处理） | **低** |
| 12 | **未知参数处理** | 文档要求"遇到未知参数直接失败"，但 `wings.py` 用 `parse_known_args()` 允许未知参数透传给引擎 | **低** |

---

## 1. 目标与约束

### 1.1 目标

- `main.py` 仍为主入口
- 输入参数语义与 `wings_start.sh` 对齐
- 优先直接复用 `wings/wings/proxy` 的 gateway + health 逻辑
- 替换 `command_builder.py` 为 wings 的配置合并 + adapter 命令构建

### 1.2 端口约定

| 角色 | 端口 | 说明 |
|------|------|------|
| 引擎后端 | `17000` | vLLM 实际监听端口 |
| 业务入口 | `18000` | 对外暴露，proxy 反代到 17000 |
| 健康探针 | `19000` | K8s livenessProbe / readinessProbe |

### 1.3 MVP 边界

- 仅支持：vLLM、单机、非分布式
- 暂不支持：sglang/mindie/wings 引擎、distributed master/worker
- proxy 的 sglang/mindie 特殊分支保留代码但 MVP 不启用

---

## 2. 进程模型（v3 缺失，本版补充）

v3 原文提出三端口三服务但未定义进程模型。以下明确：

```
容器启动
  │
  └─▶ uvicorn "app.main:app" --host 0.0.0.0 --port 19000
        │
        ├─ lifespan startup:
        │   ├─ 1. resolve_launch_args()        → 参数解析
        │   ├─ 2. resolve_engine_params()       → 配置合并
        │   ├─ 3. build_command() + write_to_volume()
        │   ├─ 4. wait_for_engine_ready()       → 轮询 127.0.0.1:17000/health
        │   └─ 5. launch_proxy_subprocess()     → 启动 proxy 子进程
        │         └─ uvicorn "app.proxy.gateway:app"
        │              --host 0.0.0.0 --port 18000
        │
        └─ main.py app 本身:
            ├─ GET  /health      → HTTP 探测 127.0.0.1:17000/health（非 PID）
            ├─ GET  /ready       → engine_ready 标志
            └─ GET  /status      → 返回引擎状态详情
```

**关键设计决策：**

1. **`main.py` (port 19000)** 作为控制面 + K8s 探针面，承担 lifespan 初始化和健康接口
2. **proxy (port 18000)** 作为业务数据面，由 lifespan 内 `subprocess` 启动（与 `wings_start.sh` 中 `nohup wings_proxy` 同模式）
3. **健康检测改为 HTTP**：sidecar 与引擎跨容器，不共享 PID namespace，PID 探活不可用。改为 HTTP 探测 `127.0.0.1:17000/health`（两个容器共享 Pod 网络栈）

---

## 3. 输入参数设计

### 3.1 参数集合（与 `wings_start.sh` + `wings.py` 交叉验证）

以下为 `wings_start.sh` 和 `wings.py parse_arguments()` 共同支持的完整参数列表：

**基础参数**
| 参数 | 类型 | 默认值 | wings.py 定义 |
|------|------|--------|---------------|
| `--host` | str | `get_local_ip()` | `_add_engine_common_arguments` |
| `--port` | int | `18000` | `get_server_port() or 18000` |
| `--model-name` | str | None (必填) | `_add_engine_common_arguments` |
| `--model-path` | str | None | `_add_engine_common_arguments` |
| `--engine` | str | None | `_add_core_arguments`, choices=[sglang,vllm,mindie,wings,transformers,xllm] |
| `--config-file` | str | None | `_add_core_arguments` |
| `--gpu-usage-mode` | str | `"full"` | `_add_core_arguments` |
| `--device-count` | int | `1` | `_add_core_arguments` |
| `--model-type` | str | `"auto"` | `_add_core_arguments`, choices=[auto,llm,embedding,rerank,mmum,mmgm] |
| `--save-path` | str | `/opt/wings/outputs` | `_add_engine_common_arguments` |
| `--distributed` | flag | False | `_add_core_arguments` |

**引擎通用参数**
| 参数 | 类型 | 默认值 |
|------|------|--------|
| `--input-length` | int | None |
| `--output-length` | int | None |
| `--trust-remote-code` | flag | None |
| `--dtype` | str | None |
| `--kv-cache-dtype` | str | None |
| `--quantization` | str | None |
| `--quantization-param-path` | str | None |
| `--gpu-memory-utilization` | float | None |
| `--enable-chunked-prefill` | flag | None |
| `--block-size` | int | None |
| `--max-num-seqs` | int | None |
| `--seed` | int | None |
| `--enable-expert-parallel` | flag | None |
| `--max-num-batched-tokens` | int | None |
| `--enable-prefix-caching` | flag | None |
| `--enable-auto-tool-choice` | flag | None |

**特性开关**
| 参数 | 类型 | 默认值 |
|------|------|--------|
| `--enable-speculative-decode` | flag | None |
| `--speculative-decode-model-path` | str | None |
| `--enable-rag-acc` | flag | None |

### 3.2 输入通道与优先级

| 优先级 | 来源 | 说明 |
|--------|------|------|
| 最高 | `WINGS_START_ARGS` env | 原始参数串，`shlex.split` 后用同构 argparse 解析 |
| 中 | 结构化环境变量 | 如 `MODEL_NAME`、`DTYPE` 等单独 env |
| 最低 | 默认值 | 代码内硬编码 |

> **注意**：`WINGS_START_ARGS` 是 sidecar 新增概念，`wings_start.sh` 自身不使用此变量。  
> 它解决的问题是：K8s `command/args` 不方便传递 `--` 风格参数时，改用一个 env 传入完整参数串。

### 3.3 未知参数处理

`wings.py` 使用 `parse_known_args()` 允许未知参数透传给引擎（记录 warning 日志）。  
sidecar 需保持相同行为——**不要直接失败**，而是将未知参数记录日志并传入 `engine_config` 的 `extra_args` 字段。

```python
args, unknown_args = parser.parse_known_args(raw_args)
if unknown_args:
    logger.warning(f"Unknown args will be passed to engine: {unknown_args}")
```

---

## 4. 端口派生逻辑

### 4.1 完整派生规则（与 `wings_start.sh` 第349-357行对齐）

```python
@dataclass
class PortPlan:
    enable_proxy: bool
    backend_port: int     # 引擎实际监听端口
    proxy_port: int       # proxy 监听端口（仅 enable_proxy=True 时有效）
    health_port: int      # 固定 19000

def derive_port_plan(port: int = 18000, enable_proxy: bool = True) -> PortPlan:
    """
    与 wings_start.sh 端口派生逻辑完全对齐：
    
    wings_start.sh 第349-357行:
        if ENABLE_REASON_PROXY == false:
            BACKEND_PORT = PORT or 18000     # 引擎直接暴露
        else:
            PROXY_PORT = PORT or 18000       # proxy 对外
            BACKEND_PORT = 17000             # 引擎固定内部端口
    """
    if not enable_proxy:
        return PortPlan(
            enable_proxy=False,
            backend_port=port,         # 引擎取 PORT 值
            proxy_port=0,              # 不启动 proxy
            health_port=19000
        )
    else:
        return PortPlan(
            enable_proxy=True,
            backend_port=17000,        # 引擎固定 17000
            proxy_port=port,           # proxy 取 PORT 值
            health_port=19000
        )
```

### 4.2 环境变量名映射

sidecar 现有变量名与本方案的对应关系：

| sidecar 现有 | 本方案新名 | 值变化 | 说明 |
|-------------|-----------|--------|------|
| `ENGINE_PORT=8000` | `BACKEND_PORT` → `17000` | 变 | 由端口派生逻辑决定 |
| `WINGS_PORT=9000` | 不再使用 | 删 | main.py 固定 19000 |
| — | `PORT=18000` | 新 | 对外业务端口 |
| — | `ENABLE_REASON_PROXY=true` | 新 | 是否启动 proxy |
| — | `HEALTH_PORT=19000` | 新 | 健康探针端口 |

---

## 5. 参数兼容模块

### 5.1 `app/core/start_args_compat.py`

```python
import argparse
import os
import shlex
import logging
from dataclasses import dataclass, field
from typing import Optional, List

logger = logging.getLogger(__name__)


@dataclass
class LaunchArgs:
    """与 wings.py parse_arguments() 完整对齐的参数对象"""
    # 基础参数
    model_name: Optional[str] = None
    model_path: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    engine: Optional[str] = None
    config_file: Optional[str] = None
    gpu_usage_mode: str = "full"
    device_count: int = 1
    model_type: str = "auto"
    save_path: str = "/opt/wings/outputs"
    distributed: bool = False
    
    # 引擎通用参数
    input_length: Optional[int] = None
    output_length: Optional[int] = None
    trust_remote_code: Optional[bool] = None
    dtype: Optional[str] = None
    kv_cache_dtype: Optional[str] = None
    quantization: Optional[str] = None
    quantization_param_path: Optional[str] = None
    gpu_memory_utilization: Optional[float] = None
    enable_chunked_prefill: Optional[bool] = None
    block_size: Optional[int] = None
    max_num_seqs: Optional[int] = None
    seed: Optional[int] = None
    enable_expert_parallel: Optional[bool] = None
    max_num_batched_tokens: Optional[int] = None
    enable_prefix_caching: Optional[bool] = None
    enable_auto_tool_choice: Optional[bool] = None
    
    # 特性开关
    enable_speculative_decode: Optional[bool] = None
    speculative_decode_model_path: Optional[str] = None
    enable_rag_acc: Optional[bool] = None
    
    # 未知参数（透传给引擎）
    extra_args: List[str] = field(default_factory=list)


@dataclass
class PortPlan:
    enable_proxy: bool
    backend_port: int
    proxy_port: int
    health_port: int = 19000


def _build_parser() -> argparse.ArgumentParser:
    """
    构建与 wings.py _add_core_arguments() + _add_engine_common_arguments() 
    同构的 argparse parser
    """
    parser = argparse.ArgumentParser()
    
    # 核心参数 — 对齐 wings.py _add_core_arguments()
    parser.add_argument("--engine", type=str, default=None,
                        choices=["sglang", "vllm", "mindie", "wings", "transformers", "xllm"])
    parser.add_argument("--distributed", action="store_true")
    parser.add_argument("--config-file", type=str, default=None)
    parser.add_argument("--gpu-usage-mode", type=str, default="full")
    parser.add_argument("--device-count", type=int, default=1)
    parser.add_argument("--model-type", type=str, default="auto",
                        choices=["auto", "llm", "embedding", "rerank", "mmum", "mmgm"])
    parser.add_argument("--enable-speculative-decode", action="store_true", default=None)
    parser.add_argument("--speculative-decode-model-path", type=str, default=None)
    parser.add_argument("--enable-rag-acc", action="store_true", default=None)
    
    # 引擎通用参数 — 对齐 wings.py _add_engine_common_arguments()
    parser.add_argument("--host", type=str, default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--model-name", type=str, default=None)
    parser.add_argument("--model-path", type=str, default=None)
    parser.add_argument("--save-path", type=str, default="/opt/wings/outputs")
    parser.add_argument("--input-length", type=int, default=None)
    parser.add_argument("--output-length", type=int, default=None)
    parser.add_argument("--trust-remote-code", action="store_true", default=None)
    parser.add_argument("--dtype", type=str, default=None)
    parser.add_argument("--kv-cache-dtype", type=str, default=None)
    parser.add_argument("--quantization", type=str, default=None)
    parser.add_argument("--quantization-param-path", type=str, default=None)
    parser.add_argument("--gpu-memory-utilization", type=float, default=None)
    parser.add_argument("--enable-chunked-prefill", action="store_true", default=None)
    parser.add_argument("--block-size", type=int, default=None)
    parser.add_argument("--max-num-seqs", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--enable-expert-parallel", action="store_true", default=None)
    parser.add_argument("--max-num-batched-tokens", type=int, default=None)
    parser.add_argument("--enable-prefix-caching", action="store_true", default=None)
    parser.add_argument("--enable-auto-tool-choice", action="store_true", default=None)
    
    return parser


def _env_fallback(args: argparse.Namespace) -> None:
    """用结构化环境变量填充 WINGS_START_ARGS 未覆盖的字段"""
    env_map = {
        "model_name":   ("MODEL_NAME", str),
        "model_path":   ("MODEL_PATH", str),
        "host":         ("HOST", str),
        "port":         ("PORT", int),
        "engine":       ("ENGINE_TYPE", str),
        "dtype":        ("DTYPE", str),
        "device_count": ("DEVICE_COUNT", int),
        "gpu_memory_utilization": ("GPU_MEMORY_UTILIZATION", float),
        "max_num_seqs": ("MAX_NUM_SEQS", int),
        "model_type":   ("MODEL_TYPE", str),
    }
    for attr, (env_key, cast) in env_map.items():
        if getattr(args, attr, None) is None:
            env_val = os.getenv(env_key)
            if env_val is not None:
                setattr(args, attr, cast(env_val))


def resolve_launch_args() -> tuple:
    """
    解析入口。返回 (LaunchArgs, PortPlan)。
    
    优先级: WINGS_START_ARGS > 结构化 env > 默认值
    """
    parser = _build_parser()
    
    # 1. 解析 WINGS_START_ARGS
    raw = os.getenv("WINGS_START_ARGS", "")
    if raw.strip():
        tokens = shlex.split(raw)
        args, unknown = parser.parse_known_args(tokens)
    else:
        args = argparse.Namespace(**{a.dest: a.default for a in parser._actions if a.dest != "help"})
        unknown = []
    
    # 2. env 兜底
    _env_fallback(args)
    
    # 3. 必填校验
    if not getattr(args, "model_name", None):
        raise ValueError("model_name is required (via --model-name or MODEL_NAME env)")
    
    if unknown:
        logger.warning(f"Unknown args (will be passed to engine): {unknown}")
    
    # 4. 构建 LaunchArgs
    launch_args = LaunchArgs(
        model_name=args.model_name,
        model_path=args.model_path or "/weights",
        host=args.host,
        port=args.port,
        engine=args.engine,
        config_file=getattr(args, "config_file", None),
        gpu_usage_mode=getattr(args, "gpu_usage_mode", "full"),
        device_count=getattr(args, "device_count", 1),
        model_type=getattr(args, "model_type", "auto"),
        save_path=getattr(args, "save_path", "/opt/wings/outputs"),
        distributed=getattr(args, "distributed", False),
        input_length=args.input_length,
        output_length=args.output_length,
        trust_remote_code=args.trust_remote_code,
        dtype=args.dtype,
        kv_cache_dtype=args.kv_cache_dtype,
        quantization=args.quantization,
        quantization_param_path=getattr(args, "quantization_param_path", None),
        gpu_memory_utilization=args.gpu_memory_utilization,
        enable_chunked_prefill=args.enable_chunked_prefill,
        block_size=args.block_size,
        max_num_seqs=args.max_num_seqs,
        seed=args.seed,
        enable_expert_parallel=args.enable_expert_parallel,
        max_num_batched_tokens=args.max_num_batched_tokens,
        enable_prefix_caching=args.enable_prefix_caching,
        enable_auto_tool_choice=args.enable_auto_tool_choice,
        enable_speculative_decode=args.enable_speculative_decode,
        speculative_decode_model_path=getattr(args, "speculative_decode_model_path", None),
        enable_rag_acc=args.enable_rag_acc,
        extra_args=unknown,
    )
    
    # 5. 端口派生
    enable_proxy = os.getenv("ENABLE_REASON_PROXY", "true").lower() != "false"
    user_port = launch_args.port or 18000
    
    if not enable_proxy:
        port_plan = PortPlan(
            enable_proxy=False,
            backend_port=user_port,
            proxy_port=0,
            health_port=19000,
        )
    else:
        port_plan = PortPlan(
            enable_proxy=True,
            backend_port=17000,
            proxy_port=user_port,
            health_port=19000,
        )
    
    return launch_args, port_plan
```

---

## 6. Proxy 复用改造要点（v3 遗漏的关键细节）

原 v3 文档仅写"直接复用 `wings/wings/proxy`"，但以下 4 项不兼容问题必须先处理：

### 6.1 `proxy/settings.py` 模块级 argparse 崩溃

**问题**：`settings.py` 第27行 `args = parse_args()`  在 import 时立即执行，而 sidecar 进程无 `--backend` 等 CLI 参数。

**修复方案**：改为惰性初始化或环境变量驱动：

```python
# 改前（wings 原版）
args = parse_args()
BACKEND_URL = args.backend.strip()

# 改后（sidecar 适配）
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:17000").strip()
HOST = os.getenv("PROXY_HOST", "0.0.0.0")
PORT = int(os.getenv("PROXY_PORT", "18000"))
```

### 6.2 `health.py` PID 探活在跨容器架构下失效

**问题**：`health.py` 通过 `_read_pid_from_file()` + `_is_pid_alive()` (`/proc/<pid>`) 判定引擎进程存活。sidecar 容器看不到引擎容器的 `/proc`。

**修复方案**：将 PID 相关逻辑替换为 HTTP 探活：

```python
# 改前（wings 原版）
def _is_pid_alive(pid):
    return os.path.exists(f"/proc/{pid}")

# 改后（sidecar 适配）
async def _is_engine_alive(client: httpx.AsyncClient) -> bool:
    """通过 HTTP 探测引擎健康（Pod 内共享网络栈）"""
    try:
        resp = await client.get(
            build_backend_url("/health"),
            timeout=httpx.Timeout(HEALTH_TIMEOUT_MS / 1000)
        )
        return resp.status_code == 200
    except Exception:
        return False
```

同时：
- `_is_mindie()` / `_is_sglang()` 不再读 PID 文件，改为从环境变量 `ENGINE_TYPE` 获取引擎类型
- `BACKEND_PID_FILE` 相关常量标记为不适用

### 6.3 Import 路径全量替换

所有 `from wings.proxy import ...` 改为 `from app.proxy import ...`。

涉及文件：`gateway.py`、`health.py`、`health_service.py`、`tags.py`、`queueing.py`、`http_client.py`、`speaker_logging.py`。

### 6.4 `health_service.py` 作为独立 app

- 原 wings 中 `health_service.py` 是独立的 FastAPI app（端口 19000）
- 在 sidecar 方案中，**建议将健康接口合并到 `main.py`**（端口 19000），避免额外进程
- `main.py` 的 lifespan 直接启动后台健康循环任务

---

## 7. 配置合并链路（补充 v3 遗漏的复用细节）

### 7.1 函数级复用清单

| wings 源函数 | sidecar 操作 | 说明 |
|-------------|-------------|------|
| `config_loader._merge_configs()` | **直接复制** | 纯 dict 深合并 |
| `config_loader._merge_cmd_params()` | **薄改造** | 入参从 `hardware_env, defaults, cmd_params, model_info` 结构不变，只改调用者 |
| `config_loader._merge_vllm_params()` | **直接复制** | 只保留非分布式路径 |
| `config_loader._set_common_params()` | **直接复制** | 使用 engine_parameter_mapping.json 做参数名转换 |
| `config_loader._set_sequence_length()` | **直接复制** | 纯逻辑 |
| `config_loader._adjust_tensor_parallelism()` | **直接复制** | TP 参数调整 |
| `config_loader._set_parallelism_params()` | **简化复制** | 只保留单机分支 |
| `config_loader._merge_final_config()` | **直接复制** | trivial |
| `config_loader.load_and_merge_configs()` | **薄改造** | 签名改为接收 `LaunchArgs` 替代 `argparse.Namespace` |
| `config_loader._process_cmd_args()` | **替换** | → `_launch_args_to_cmd_params(launch_args)` |
| `config_loader._auto_select_engine()` | **简化** | MVP 直接返回 "vllm" |
| `vllm_adapter._build_vllm_cmd_parts()` | **直接复制** | 核心命令构建，纯字符串逻辑 |
| `vllm_adapter._build_vllm_command()` | **薄改造** | 去掉 env_commands（sidecar 不 source 脚本） |
| `vllm_adapter._start_vllm_single()` | **删除** | sidecar 写文件不启进程 |
| `vllm_adapter._build_env_commands()` | **删除** | sidecar 不需要 source 脚本 |
| `config/engine_parameter_mapping.json` | **直接复制** | 仅保留 vllm 段 |
| `config/nvidia_default.json` → `llm.default.vllm` | **裁剪复制** → `vllm_default.json` | 默认参数模板 |

### 7.2 参数名映射示意

`engine_parameter_mapping.json` 的 `default_to_vllm_parameter_mapping` 段将通用名转为 vLLM CLI 参数名：

```
model_path         → model              → --model
model_name         → served_model_name  → --served-model-name
trust_remote_code  → trust_remote_code  → --trust-remote-code
max_num_seqs       → max_num_seqs       → --max-num-seqs
gpu_memory_utilization → gpu_memory_utilization → --gpu-memory-utilization
...
```

`_build_vllm_cmd_parts()` 最终将 `engine_config` 中的 key 做 `_` → `-` 替换作为 CLI flag：  
`tensor_parallel_size` → `--tensor-parallel-size`

### 7.3 `LaunchArgs` → `cmd_params` 适配层

这是连接 sidecar 输入与 wings 配置合并逻辑的桥梁：

```python
def _launch_args_to_cmd_params(launch_args: LaunchArgs) -> dict:
    """
    对标 wings config_loader._process_cmd_args(known_args: argparse.Namespace)
    把 LaunchArgs 转为 wings 内部期望的 dict 格式
    """
    return {
        "engine":                    launch_args.engine or "vllm",
        "model_name":                launch_args.model_name,
        "model_path":                launch_args.model_path,
        "host":                      launch_args.host,
        "port":                      None,  # port 由 PortPlan 控制，不参与通用参数
        "input_length":              launch_args.input_length,
        "output_length":             launch_args.output_length,
        "trust_remote_code":         launch_args.trust_remote_code,
        "dtype":                     launch_args.dtype,
        "kv_cache_dtype":            launch_args.kv_cache_dtype,
        "quantization":              launch_args.quantization,
        "quantization_param_path":   launch_args.quantization_param_path,
        "gpu_memory_utilization":    launch_args.gpu_memory_utilization,
        "enable_chunked_prefill":    launch_args.enable_chunked_prefill,
        "block_size":                launch_args.block_size,
        "max_num_seqs":              launch_args.max_num_seqs,
        "seed":                      launch_args.seed,
        "enable_expert_parallel":    launch_args.enable_expert_parallel,
        "max_num_batched_tokens":    launch_args.max_num_batched_tokens,
        "enable_prefix_caching":     launch_args.enable_prefix_caching,
        "enable_auto_tool_choice":   launch_args.enable_auto_tool_choice,
        "config_file":               launch_args.config_file,
        "gpu_usage_mode":            launch_args.gpu_usage_mode,
        "device_count":              launch_args.device_count,
        "model_type":                launch_args.model_type,
        "distributed":               False,  # MVP 强制单机
        "save_path":                 launch_args.save_path,
    }
```

---

## 8. `main.py` 启动流程

```python
import asyncio
import subprocess
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.core.start_args_compat import resolve_launch_args
from app.core.wings_entry import resolve_engine_params
from app.engines.vllm_adapter import build_command
from app.utils.file_utils import write_command_to_volume

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. 参数解析 + 端口派生
    launch_args, port_plan = resolve_launch_args()
    app.state.launch_args = launch_args
    app.state.port_plan = port_plan
    logger.info(f"LaunchArgs: model={launch_args.model_name}, engine={launch_args.engine}")
    logger.info(f"PortPlan: backend={port_plan.backend_port}, proxy={port_plan.proxy_port}, health={port_plan.health_port}")
    
    # 2. 决策 + 配置合并
    final_params = resolve_engine_params(launch_args, port_plan)
    
    # 3. 拼命令 + 写共享卷
    command = build_command(final_params)
    logger.info(f"Engine command: {command}")
    await write_command_to_volume(command, "/shared-volume", "start_command.sh")
    
    # 4. 等待引擎就绪
    await wait_for_engine_ready(port_plan.backend_port)
    app.state.engine_ready = True
    
    # 5. 启动 proxy 子进程（如果启用）
    proxy_proc = None
    if port_plan.enable_proxy:
        proxy_proc = _start_proxy(port_plan)
    
    yield
    
    # 清理
    if proxy_proc:
        proxy_proc.terminate()
        proxy_proc.wait(timeout=5)


def _start_proxy(port_plan) -> subprocess.Popen:
    """启动 proxy 子进程（对标 wings_start.sh 中 nohup wings_proxy 逻辑）"""
    import os
    env = os.environ.copy()
    env["BACKEND_URL"] = f"http://127.0.0.1:{port_plan.backend_port}"
    env["PROXY_PORT"] = str(port_plan.proxy_port)
    env["PROXY_HOST"] = "0.0.0.0"
    
    proc = subprocess.Popen(
        ["python", "-m", "uvicorn", "app.proxy.gateway:app",
         "--host", "0.0.0.0", "--port", str(port_plan.proxy_port)],
        env=env
    )
    logger.info(f"Proxy started on port {port_plan.proxy_port}, PID={proc.pid}")
    return proc


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    """K8s 探针：HTTP 探测引擎健康"""
    import httpx
    backend_port = app.state.port_plan.backend_port
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"http://127.0.0.1:{backend_port}/health")
            if resp.status_code == 200:
                return {"status": "healthy", "engine_ready": True}
    except Exception:
        pass
    return {"status": "unhealthy", "engine_ready": getattr(app.state, "engine_ready", False)}


@app.get("/ready")
async def ready():
    return {"ready": getattr(app.state, "engine_ready", False)}
```

---

## 9. 目标目录结构

```
backend/app/
├── main.py                                # 改：控制面 + 健康面（port 19000）
├── api/
│   └── routes.py                          # 保留（经 proxy 转发，不直接对外）
├── config/
│   ├── settings.py                        # 改：新增端口/参数字段
│   ├── vllm_default.json                  # 新：裁剪自 nvidia_default.json
│   └── engine_parameter_mapping.json      # 新：直接复用 wings 版（vllm 段）
├── core/
│   ├── __init__.py
│   ├── start_args_compat.py               # 新：参数对齐模块
│   ├── wings_entry.py                     # 新：决策入口（薄改造自 wings.py main()）
│   ├── config_loader.py                   # 新：配置合并（薄改造自 wings config_loader.py）
│   └── hardware_detect.py                 # 新：硬件探测（直接复用 + env 快速路径）
├── engines/
│   ├── __init__.py
│   └── vllm_adapter.py                    # 新：命令构建（薄改造自 wings vllm_adapter.py）
├── proxy/                                 # 新：从 wings/wings/proxy 复制
│   ├── __init__.py
│   ├── gateway.py                         # 直接复用（改 import 路径）
│   ├── health.py                          # 改：PID 探活 → HTTP 探活
│   ├── health_service.py                  # 可选（合并到 main.py 更简洁）
│   ├── http_client.py                     # 直接复用
│   ├── queueing.py                        # 直接复用
│   ├── settings.py                        # 改：argparse → 纯 env 驱动
│   ├── tags.py                            # 直接复用
│   └── speaker_logging.py                 # 直接复用
├── services/
│   ├── engine_manager.py                  # 改：接入 wings_entry + adapter
│   └── proxy_service.py                   # 保留（MVP 仍可用，后续可切到 proxy/gateway）
└── utils/
    ├── device_utils.py                    # 新：复用自 wings（保护 torch 依赖）
    ├── file_utils.py                      # 保留（write_command_to_volume 已有）
    └── http_client.py                     # 保留
```

删除：`services/command_builder.py`

---

## 10. 变更清单（完整）

| 文件 | 动作 | 来源 | 说明 |
|------|------|------|------|
| `main.py` | **改** | 原有 | 改为 19000 健康面 + lifespan 启动 proxy 子进程 |
| `config/settings.py` | **改** | 原有 | 新增 WINGS_START_ARGS / ENABLE_REASON_PROXY / PORT / HEALTH_PORT |
| `services/engine_manager.py` | **改** | 原有 | 接入 wings_entry + adapter（删 CommandBuilder） |
| `services/command_builder.py` | **删** | 原有 | 被 vllm_adapter 替代 |
| `core/__init__.py` | **新** | — | |
| `core/start_args_compat.py` | **新** | — | 参数解析与端口派生 |
| `core/wings_entry.py` | **新** | 薄改造自 wings.py main() | 决策入口 |
| `core/config_loader.py` | **新** | 薄改造自 wings config_loader | 配置合并 |
| `core/hardware_detect.py` | **新** | 直接复用 wings hardware_detect | + env 快速路径 |
| `engines/__init__.py` | **新** | — | |
| `engines/vllm_adapter.py` | **新** | 薄改造自 wings vllm_adapter | 只保留命令构建 |
| `utils/device_utils.py` | **新** | 直接复用 wings device_utils | 保护 torch 可选依赖 |
| `config/vllm_default.json` | **新** | 裁剪自 nvidia_default.json | |
| `config/engine_parameter_mapping.json` | **新** | 直接复用 wings 版 | vllm 段 |
| `proxy/` 目录 (8个文件) | **新** | 复用自 wings/proxy | settings 改 env 驱动，health 改 HTTP 探活 |
| `api/routes.py` | **不变** | — | |
| `services/proxy_service.py` | **不变** | — | |
| `utils/file_utils.py` | **不变** | — | |
| `utils/http_client.py` | **不变** | — | |

共 **3 改 + 1 删 + 15 新**。

---

## 11. K8s 部署对齐

### 11.1 `deployment.yaml` 要点

```yaml
containers:
  - name: wings-infer
    ports:
      - containerPort: 18000  # 业务（proxy）
        name: proxy
      - containerPort: 19000  # 健康
        name: health
    env:
      - name: WINGS_START_ARGS
        value: "--model-name DeepSeek-R1 --model-path /weights/DeepSeek-R1 --dtype bfloat16"
      - name: ENABLE_REASON_PROXY
        value: "true"
      - name: PORT
        value: "18000"
    livenessProbe:
      httpGet:
        path: /health
        port: 19000
      initialDelaySeconds: 60
      periodSeconds: 10
    readinessProbe:
      httpGet:
        path: /health
        port: 19000
      initialDelaySeconds: 30
      periodSeconds: 5
    command: ["python", "-m", "uvicorn"]
    args: ["app.main:app", "--host", "0.0.0.0", "--port", "19000"]
```

### 11.2 `service.yaml`

```yaml
ports:
  - name: proxy
    port: 18000
    targetPort: 18000
  - name: health
    port: 19000
    targetPort: 19000
```

---

## 12. 验收用例

| # | 场景 | 输入 | 期望 |
|---|------|------|------|
| 1 | 默认 proxy | `ENABLE_REASON_PROXY=true`, PORT 未设 | proxy=18000, backend=17000 |
| 2 | 禁用 proxy | `ENABLE_REASON_PROXY=false`, PORT 未设 | backend=18000, proxy 不启动 |
| 3 | 自定义端口 | `ENABLE_REASON_PROXY=true`, `PORT=18080` | proxy=18080, backend=17000 |
| 4 | 必填校验 | 缺 `--model-name` | 启动失败，明确错误信息 |
| 5 | 未知参数 | `--custom-flag value` | 日志 warning，不崩溃（对齐 wings `parse_known_args`） |
| 6 | WINGS_START_ARGS | `WINGS_START_ARGS="--model-name X --dtype bf16"` | 正确解析，优先于结构化 env |
| 7 | 引擎健康 | `GET http://127.0.0.1:19000/health` | 当引擎就绪时返回 `{"status": "healthy"}` |
| 8 | 业务面 | `POST http://<svc>:18000/v1/chat/completions` | 返回 200 |
| 9 | 命令验证 | `cat /shared-volume/start_command.sh` | 包含 `--model`, `--served-model-name`, `--port 17000` |
| 10 | 参数映射 | 设置 `--model-name X` | 命令中出现 `--served-model-name X`（非 `--model-name`） |

---

## 13. v3 → v3-claude 变更摘要

| 项 | v3 原文 | v3-claude 修正 |
|----|---------|---------------|
| 进程模型 | 未定义 | 明确：main.py=19000(健康面) + proxy 子进程=18000(业务面) |
| 健康检测 | 未提及 PID 限制 | PID 方式不可用，改为 HTTP 探测 |
| proxy/settings.py | 直接复用 | 必须改造：argparse → env 驱动 |
| health.py | 直接复用 | 必须改造：PID 探活 → HTTP 探活 |
| LaunchArgs | 5 字段 | 完整 23+ 字段 |
| WINGS_START_ARGS | 称"对齐 wings_start.sh" | 明确标注为 sidecar 新增概念 |
| 未知参数 | 直接失败 | 保持 wings 行为：warning + 透传 |
| config_loader 复用 | 仅列文件名 | 补充函数级复用清单（v2 水平） |
| 参数名映射 | 未提及 | 补充 model_name→served_model_name 等映射逻辑 |
| 端口变量名 | ENGINE_PORT 含义不明 | 明确 ENGINE_PORT→BACKEND_PORT 映射 |
| MODEL_PATH 默认值 | `/weights` | 实现中走 `launch_args.model_path or "/weights"`，与 wings_start.sh 对齐 |
| PortPlan | 片段式 | 完整 derive_port_plan() 逻辑含两个分支 |
