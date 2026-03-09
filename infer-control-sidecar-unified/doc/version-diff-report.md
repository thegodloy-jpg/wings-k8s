# 新旧版本功能差异分析报告

> 对比对象：
> - **旧版本**：`wings/wings/`（单体式 Python + Shell 框架）
> - **新版本**：`backend/app/`（K8s Sidecar 架构）
>
> 精细度：逐文件逐函数级别源码阅读，覆盖 15 个维度

---

## 一、总览对比

| 维度 | 旧版本 (wings/wings/) | 新版本 (backend/app/) |
|---|---|---|
| **总代码量** | ~14,050 行 | ~7,539 行 |
| **架构模式** | 单体容器：launcher + engine + proxy 同容器运行 | K8s Sidecar 双容器：launcher 生成 `start_command.sh` → 共享卷 → engine 容器执行 |
| **分布式** | 自建 Master-Worker-Monitor-Scheduler（517 行） | 完全移除 — 交由 K8s StatefulSet 原生管理 |
| **硬件检测** | 运行时通过 torch/pynvml/torch_npu 探测（274 行） | 环境变量静态声明（102 行） |
| **引擎适配器** | 4 个：vllm, sglang, mindie, wings（+xllm CLI 选项） | 3 个：vllm, sglang, mindie |
| **进程管理** | 直接 `subprocess.Popen` + PID 文件 | 生成脚本 → 共享卷 → engine 容器执行 |
| **子服务托管** | proxy 由线程/直接启动 | `ManagedProc` 封装 + 守护循环（自动重启） |
| **PD 分离** | ✅ 完整支持 | ✅ 完整支持（逻辑一致，K8s 原生部署） |
| **安全加固** | 基础 | shell 路径清理、`shlex.quote`、`set -euo pipefail`、JSON 校验 |
| **Accel 补丁** | ❌ 无 | ✅ `WINGS_ENGINE_PATCH_OPTIONS` 注入（已同步至 unified + analyse 两版本） |
| **端口方案** | 动态可配 | 固定三层：backend(17000)/proxy(18000)/health(19000) |
| **K8s 集成** | ❌ 需手动部署 | ✅ 原生支持（StatefulSet、Downward API、探针） |

---

## 二、分布式架构差异（重点）

### 2.1 旧版本：自建 Master-Worker-Monitor-Scheduler

旧版在 `distributed/` 目录中实现了 **4 个 FastAPI 微服务**，总计 517 行：

| 组件 | 文件 | 行数 | 职责 |
|---|---|---|---|
| **Master** | `distributed/master.py` | 246 | 中央控制平面，负责节点注册、引擎启动分发、推理请求路由 |
| **Worker** | `distributed/worker.py` | 144 | 计算节点代理，注册到 Master、心跳汇报、本地引擎启动 |
| **Monitor** | `distributed/monitor.py` | 88 | 节点健康监控，30 秒巡检，60 次心跳丢失则移除节点 |
| **Scheduler** | `distributed/scheduler.py` | 85 | 三种调度策略：LEAST_LOAD / ROUND_ROBIN / RANDOM，含重试机制 |

**启动流程**（`wings_start.sh`）：
1. 若 `DISTRIBUTED=true` 且本机为 Master（`MASTER_IP == RANK_IP`）→ 启动 `wings.distributed.master`
2. 所有节点 → 启动 `wings.distributed.worker`
3. 仅 Master → 启动 `wings.wings`（主入口），通过 Master API 触发全部 Worker

**关键 API**：
- `POST /api/nodes/register` — Worker 注册
- `POST /api/start_engine` — 分发引擎启动命令
- `POST /api/heartbeat` — 心跳上报
- `POST /api/inference` — 推理请求路由

### 2.2 新版本：K8s 原生 Sidecar 模式

新版 **完全移除了自建分布式层**，改为 K8s 原生机制：

| 能力 | 旧版实现 | 新版实现 |
|---|---|---|
| 节点发现 | Worker → Master HTTP 注册 | K8s DNS 自动发现（`infer-0.infer-hl`, `infer-1.infer-hl`） |
| 心跳/健康 | 自建 30 秒心跳线程 + Monitor | K8s livenessProbe / readinessProbe（端口 19000） |
| 任务调度 | `TaskScheduler`（最小负载/轮循/随机） | K8s Service 负载均衡 / Ray 内置调度 |
| Rank 分配 | `NODE_IPS` 环境变量 → 列表索引 → rank | StatefulSet 序号（`NODE_RANK` 环境变量） |
| 引擎启动 | Master API → Worker API → `start_engine_service()` | Sidecar 写 `start_command.sh` → 共享卷 → engine 容器执行 |
| 分布式后端 | Ray 或 dp_deployment（运行时决定） | 同样支持 Ray/DP，但由 K8s 编排 |

### 2.3 Ray 分布式差异

| 功能 | 旧版 (`vllm_adapter.py`) | 新版 (`vllm_adapter.py`) |
|---|---|---|
| Ray 启动 | `_start_ray_node()` → `subprocess.Popen(ray_cmd)` | `build_start_script()` 生成含 `ray start --head` / `ray start --address` 的 bash 脚本 |
| Ray 等待 | `wait_until_ray_head_ready()`: 循环 `ray.init(address='auto')` | 脚本内联 for 循环: `for i in $(seq 1 60); do COUNT=$(python3 -c "import ray; ..."); break; done` |
| Worker 加入 | `wait_until_all_workers_joined()`, `check_node_joined()` | 同样通过 `ray.nodes()` 检查，但逻辑在生成的 bash 脚本中 |
| Resource 标签 | `--num-gpus=1` (NVIDIA) | NVIDIA: `--num-gpus=1`；Ascend: `--resources='{"NPU": 1}'` |
| Head IP (Ascend) | 使用 `netifaces` 库 `detect_network_interface()` | `$POD_IP` (K8s Downward API)，fallback Python UDP trick |
| Worker 探测 Head (Ascend) | 直接使用 `MASTER_IP` 连接 | 动态扫描 `NODE_IPS` 逐个 TCP 尝试 Ray port |
| enforce_eager (Ascend) | 未强制 | rank0 启动 vllm 时追加 `--enforce-eager`（Triton NPU driver 检测在 k3s 中失败） |

### 2.4 dp_deployment 分布式差异

| 功能 | 旧版 | 新版 |
|---|---|---|
| 触发条件 | DeepSeekV3 + vllm_ascend 或 PD 角色为 P/D | 通过 `--distributed-executor-backend dp_deployment` CLI 参数 |
| Ascend 参数 | `dp=4, dp_local=2, dp_start_rank=2`（硬编码） | `--data-parallel-size {nnodes} --data-parallel-size-local 1 --data-parallel-rank {node_rank}` |
| RPC 端口 | `distributed_config.json` 中 `rpc_port: 13355` | `os.getenv('VLLM_DP_RPC_PORT', '13355')` |

### 2.5 MindIE 分布式差异

| 功能 | 旧版 | 新版 |
|---|---|---|
| 分布式配置 | 无 `_handle_mindie_distributed()` | **新增**：注入 `MASTER_ADDR`/`MASTER_PORT` + `distributed_config.json` 中 `mindie_distributed.master_port` |
| Rank Table | `RANK_TABLE_PATH` 由环境变量直接指定已有文件 | `_build_rank_table_commands()`: **动态生成** HCCL rank table JSON（解析 `HCCL_DEVICE_IPS` → JSON → `chmod 640`） |
| 分布式环境变量 | 在 `set_mindie_multi_env.sh` 中设置 | `_build_distributed_env_commands()`: 设置 `MASTER_ADDR`, `MASTER_PORT`, `RANK`, `WORLD_SIZE`, `HCCL_CONNECT_TIMEOUT` |
| multiNodesInferEnabled | 无特殊处理 | 显式设为 `false`（由 ms_coordinator 控制） |

### 2.6 SGLang 分布式差异

| 功能 | 旧版 | 新版 |
|---|---|---|
| 分布式参数 | `--nnodes`, `--node-rank`, `--dist-init-addr`, `NCCL_SOCKET_IFNAME`/`GLOO_SOCKET_IFNAME` | **同样保留**，参数名一致 |
| 环境变量 | 在 `set_sglang_env.sh` 中设置 | adapter 中 `_build_distributed_env_commands()` 内联设置 |

### 2.7 差异总结

> **旧版**属于 "应用层编排"——在 Python 中重新实现了一套节点管理 + 调度 + 心跳的分布式系统。  
> **新版**属于 "平台层编排"——将这些能力完全下沉到 K8s StatefulSet / Service / Probe 等原生能力，Python 层只负责 **生成启动脚本**。

---

## 三、硬件支持差异（精细对比）

### 3.1 硬件检测机制

| 细节 | 旧版 | 新版 |
|---|---|---|
| **检测方式** | `hardware_detect.py` → `device_utils.get_device_info()` → 真实调用 `torch_npu.npu.device_count()` + `torch_npu.npu.mem_get_info()` + `torch_npu.npu.get_device_name()` 或 `pynvml` | 仅读取环境变量 `WINGS_DEVICE`/`DEVICE`、`WINGS_DEVICE_COUNT`/`DEVICE_COUNT`、`WINGS_DEVICE_NAME`；不导入 torch/pynvml |
| **device 映射** | `get_device_info()` 返回 `device="npu"` → `hardware_detect` 映射为 `"ascend"` | `_normalize_device()` 支持 `ascend/npu/NPU` → `"ascend"` |
| **内存信息** | 返回每张卡的 `total_memory`/`used_memory`/`free_memory` (GB) | **不返回内存信息**（sidecar 容器无 GPU 访问权限）|
| **device_name** | 来自 `torch_npu.npu.get_device_name(0)`（如 `"Ascend910B"`） | 来自 `WINGS_DEVICE_NAME` 环境变量 |
| **支持设备类型** | nvidia, ascend, cpu | nvidia, ascend（**无 cpu 回退**） |
| **依赖库** | torch, pynvml, torch_npu | 无外部依赖 |

> **影响**：新版无法在运行时自动获取 NPU/GPU 型号和显存，依赖 K8s 部署模板通过环境变量注入。

### 3.2 Ascend 310 特殊逻辑

| 功能 | 旧版 | 新版 | 状态 |
|---|---|---|---|
| **引擎强制选择** | `_select_ascend_engine()`: `if "310" in device_name → return 'mindie'` | 同样存在 | ✅ 保留 |
| **embedding/rerank 拒绝** | `if "310" in device_name and model_type in ["embedding", "rerank"]: raise ValueError` | 同样存在 | ✅ 保留 |
| **bfloat16 类型检查** | `check_torch_dtype()`: 读取模型 `config.json`，若 `torch_dtype == "bfloat16"` → `raise ValueError("Ascend310 does not support bfloat16")` | 完全保留在 `file_utils.py` 中 | ✅ 保留 |
| **config.json 权限** | `check_permission_640()`: MindIE 要求 640 权限，不满足则 `os.chmod(config_json_file, 0o640)` | 完全保留 | ✅ 保留 |
| **环境脚本** | `set_mindie_single_env.sh` / `set_mindie_multi_env.sh`：激活 CANN + ATB + MindIE | adapter 中**内联** Ascend env 设置 | ✅ 等效保留 |

### 3.3 Ascend 910/910B 特殊逻辑

| 功能 | 旧版 | 新版 | 状态 |
|---|---|---|---|
| **Soft FP8** | `_set_soft_fp8()`: 仅 Ascend + DeepseekV3 + fp8 量化时启用；设置 `quantization='ascend'`, `tensor_parallel_size=4`, `enable_expert_parallel=False`, `no_enable_prefix_caching=True`, `additional_config`(ascend_scheduler/torchair_graph) | 同样保留 | ✅ 保留 |
| **算子加速** | `_set_operator_acceleration()`: `get_operator_acceleration_env()` + `device=="ascend"` → `use_kunlun_atb=True` | 同样保留 | ✅ 保留 |
| **vllm_ascend 命名** | `_handle_ascend_vllm()`: `device_type=="ascend" and engine=="vllm"` → 自动重命名为 `"vllm_ascend"` | 同样保留（`engine_manager.py` 别名映射 `"vllm_ascend" → "vllm"`） | ✅ 保留 |
| **Triton NPU dummy driver** | ❌ 不存在 | ✅ `vllm_adapter.py`: Ray 分布式 + vllm_ascend 场景，注入 inline Python 修补 `triton.runtime.driver.py`，替换 `raise RuntimeError("0 active drivers")` 为 `_NpuDummyDrv` | ⭐ 新版独有 |

### 3.4 NVIDIA H20 GPU 差异

| 维度 | 旧版 | 新版 |
|---|---|---|
| **检测方式** | `is_h20_gpu(total_memory, tolerance_gb=10.0)`: 根据实际 VRAM 判断 `abs(total-96)<=10 → "H20-96G"`、`abs(total-141)<=10 → "H20-141G"` | `_get_h20_model_hint()`: 读取 `WINGS_H20_MODEL` 环境变量，**不再从 VRAM 推断** |
| **调用位置** | `_get_model_specific_config()` 中 `is_h20_gpu(main_gpu.get("total_memory", 0), 10.0)` | `_get_model_specific_config()` 中 `_get_h20_model_hint()` |
| **影响** | 虚拟化/MIG 场景下 VRAM 大小可能误判 | 需要 K8s 模板显式设置 `WINGS_H20_MODEL` |

> **注意**：`device_utils.py` 中的 `is_h20_gpu()` 函数在新版中**仍然存在但未被引用**（死代码）。

#### H20 专属模型配置（两版均保留）

| 模型 | H20 型号 | 引擎 | 关键参数 |
|---|---|---|---|
| DeepSeek-R1 | H20-96G | sglang | `mem_fraction_static=0.9`, `grammar_backend=xgrammar` |
| DeepSeek-R1 | H20-141G | sglang | `dp=8`, `mem_fraction_static=0.95`, `enable_dp_attention=true`, `enable_mixed_chunk=true`, `attention_backend=flashinfer`, `enable_nccl_nvls=true`, `enable_torch_compile=true`, `torch_compile_max_bs=32` |
| DeepSeek-V3.1 | H20-96G | sglang | 同 R1 H20-96G |
| DeepSeek-V3.1 | H20-141G | sglang | 同 R1 H20-141G |

### 3.5 GPU 显存 / NPU 内存自动调参

| 功能 | 旧版 | 新版 | 差异影响 |
|---|---|---|---|
| **VRAM 需求检查** | `_check_vram_requirements()`: 计算模型权重大小 vs 实际可用显存，输出 warning | 同样保留，但 `hardware_env["details"]` 为空 → 直接跳过（"Cannot get VRAM details"） | ⚠️ 新版总是跳过 VRAM 检查 |
| **CUDA Graph 自动计算** | `_set_cuda_graph_sizes()`: 根据 `total_memory` 和 `num_hidden_layers` 计算 | 同样保留，但新版 `details` 为空 → fallback 使用 `total_memory = 12` | ⚠️ 新版始终使用 12GB fallback |
| **gpu_memory_utilization 默认** | CLI 默认 `None`，最终由 JSON 配置决定 | CLI 默认 `0.9`，JSON fallback 默认 `0.8` | 不同默认值 |
| **npu_memory_fraction** | `set_mindie_single_env.sh` 中 `0.96`，multi 中 `0.97` | ~~`mindie_default.json` 中 `0.8`~~ → ✅ 已调整为 `0.9` | 差距缩小 |

### 3.6 引擎支持矩阵

| 引擎 | 旧版 | 新版 | 说明 |
|---|---|---|---|
| **vllm** (NVIDIA) | ✅ 543 行 | ✅ 694 行（增强） | Ray 分布式 + DP 分布式均支持 |
| **vllm_ascend** (昇腾 910/910B) | ✅ vllm_adapter 内处理 | ✅ 同上，engine_manager 中注册别名 | HCCL rank table 生成 |
| **sglang** | ✅ 136 行 | ✅ 181 行 | 单机/分布式均支持 |
| **mindie** (昇腾 310/910) | ✅ 254 行 | ✅ 531 行（大幅扩展） | 新版新增多节点完整支持 |
| **wings** (HunyuanVideo) | ✅ 493 行 + `servers/` 1,402 行 | ❌ **已移除** | 多模态视频生成，支持 CUDA + NPU |
| **wings** (Transformers LLM) | ✅ `servers/transformers_server.py` 1,347 行 | ❌ **已移除** | 通用 HF 模型推理，SSE 流式 |
| **xllm** | CLI 中有选项 | ❌ 不支持 | 无完整适配器 |

---

## 四、量化支持 (GPTQ / AWQ / FP8 / Soft FP8)

| 功能 | 旧版 | 新版 | 状态 |
|---|---|---|---|
| **通用量化传参** | `engine_parameter_mapping.json`: `quantization → quantization` | 完全相同的映射文件 | ✅ 保留 |
| **Soft FP8 检测** | `get_soft_fp8_env()` → `SOFT_FP8` 环境变量 | 同样保留 | ✅ 保留 |
| **Soft FP8 条件** | Ascend only + DeepseekV3ForCausalLM + `model_quantize=="fp8"` | 同样保留 | ✅ 保留 |
| **Soft FP8 参数** | `quantization='ascend'`, `additional_config` (ascend_scheduler + torchair_graph), `no_enable_prefix_caching=True`, `enable_expert_parallel=False`, `tp=4`, `use_kunlun_atb=False` | 同样保留 | ✅ 保留 |
| **模型量化识别** | `ModelIdentifier.identify_model_quantize()`: 从 `config.json` 的 `quantize`/`quantization_config.quant_method`/`torch_dtype` 提取 | 完全相同 | ✅ 保留 |

---

## 五、PD（Prefill-Decode）分离对比

### 两版均完整支持 PD 分离 ✅

#### 5.1 实现逻辑（两版一致）

| 步骤 | 实现位置 | 说明 |
|---|---|---|
| 角色检测 | `env_utils.get_pd_role_env()` | 读取 `PD_ROLE` 环境变量（"P" 或 "D"） |
| KV 传输配置 | `config_loader._get_pd_config()` | 昇腾：`LLMDataDistCMgrConnector` / NVIDIA：`NixlConnector` |
| 多级缓存 | `config_loader._set_kv_cache_config()` | PD + LMCache 通过 `MultiConnector` 组合 |
| 环境变量注入 | `vllm_adapter._build_pd_role_env_commands()` | NVIDIA：`VLLM_NIXL_SIDE_CHANNEL_HOST` 等；昇腾：HCCL 接口、`VLLM_LLMDD_RPC_PORT` 等 |
| QAT 压缩 | `get_qat_env()` → `LMCACHE_QAT` | 两版完全相同 |

#### 5.2 差异点

| 方面 | 旧版 | 新版 |
|---|---|---|
| 部署方式 | 通过自建 Master-Worker 分发 PD 角色 | K8s 原生：为 P 和 D 创建独立 StatefulSet |
| 测试脚本 | ✅ `test/wings_start_for_pd.sh`（241 行） | ❌ 无独立测试脚本（依赖 K8s 部署） |
| `dp_deployment` 后端 | PD + 分布式自动切换为 dp_deployment | 同逻辑保留 |
| Ascend PD KV 端口 | 硬编码 `"20001"` | `os.getenv("PD_KV_PORT", "20001")`（可通过环境变量覆盖） |
| NPU PD 环境变量 | 在 adapter 中硬编码 14 个环境变量 | 部分改为 env 覆盖：`OMP_NUM_THREADS`、`NPU_MAX_SPLIT_SIZE_MB` |

---

## 六、KV Cache Offload (LMCache)

| 功能 | 旧版 | 新版 |
|---|---|---|
| 检测 | `get_lmcache_env()` → `LMCACHE_OFFLOAD` 环境变量 | 完全相同 |
| kv_transfer_config 生成 | `_set_kv_cache_config()`: LMCache+PD=MultiConnector, LMCache-only=LMCacheConnectorV1, PD-only=_get_pd_config | 同样保留 |
| LMCache 库路径 | 硬编码 `/opt/vllm_env/lib/lmcache/...` / `/opt/ascend_env/lib/lmcache/...` | 通过 `KV_AGENT_LIB_PATH` / `LMCACHE_LIB_PATH` 环境变量 |

---

## 七、引擎适配器详细差异

### 7.1 vLLM 适配器

| 特性 | 旧版 (543 行) | 新版 (694 行) |
|---|---|---|
| 进程启动方式 | `subprocess.Popen` + PID 跟踪 + 日志流 | **关闭** — 仅生成 shell 脚本，`start_engine()` 始终 `raise RuntimeError` |
| Ray 分布式 | 完整 Python 实现 | Ray 脚本生成 + bash 探测循环 |
| DP 分布式 | 直接 `subprocess.Popen` | 脚本生成 + `--data-parallel-*` 参数 |
| 网络接口发现 | `netifaces` 库匹配 IP 前缀 | `NCCL_SOCKET_IFNAME` 环境变量 或 `NETWORK_INTERFACE` 或 `/proc/net/route` 解析 |
| 昇腾 Triton 补丁 | ❌ 无 | ✅ 内联 Python 补丁修复 `triton.runtime.driver` |
| Shell 安全 | ❌ 无 | ✅ `_sanitize_shell_path()` 正则移除 `;|&\`$(){}` |
| HOST_IP 获取 | `netifaces` + `RANK_IP` | K8s Downward API `POD_IP` + Python UDP trick |

### 7.2 SGLang 适配器

| 特性 | 旧版 (136 行) | 新版 (181 行) |
|---|---|---|
| 进程启动方式 | `subprocess.Popen` 直接启动 | 仅脚本生成 |
| 分布式参数 | `NODE_IPS` → 列表索引 → node_rank | `nnodes`, `node_rank`, `head_node_addr` 独立参数 |
| Shell 安全 | ❌ 无 | ✅ `shlex.quote()` + `_sanitize_shell_path()` |
| 环境脚本 | `set_sglang_env.sh` 硬编码路径 | `SGLANG_ENV_SCRIPT` 环境变量指定（更灵活） |

### 7.3 MindIE 适配器

| 特性 | 旧版 (254 行) | 新版 (531 行) |
|---|---|---|
| 进程启动方式 | `subprocess.Popen` + config.json 合并 | 仅脚本生成 |
| 配置合并 | Python 直接读写 `conf/config.json` | bash 脚本中内联 Python 合并 |
| 分布式支持 | 基础（环境变量配置） | **完整** — rank table 动态生成、HCCL 设备 IP、多节点脚本 |
| Rank Table | `RANK_TABLE_PATH` 指定已有文件 | `_build_rank_table_commands()` 动态生成 + `chmod 640` |

### 7.4 Wings 适配器（旧版独有）

`wings_adapter.py` (493 行) — **新版完全不存在**：

- **HunyuanVideo (mmgm)**：多模态视频生成
  - 单卡 + 多卡（`torchrun`）支持
  - 自动发现 DIT weights、VAE、text encoder、CLIP 路径
  - Ring/Ulysses 并行度配置
  - 支持 CUDA 和 Ascend NPU 双设备

- **Transformers LLM**：通用 HuggingFace 模型
  - `transformers_server.py` (1,347 行)：SSE 流式 + chat/completions 端点
  - `device_map="auto"` 多 GPU 自动分配
  - 支持 cuda 和 npu 设备

- **HunyuanVideo Server**：`servers/model/hunyuanvideo_server/` (5 文件, ~1,402 行)
  - `app.py` (839 行): FastAPI 视频生成 API
  - `distributed.py` (180 行): 多 GPU 分布式推理
  - `sample_video.py` (174 行): 视频采样流水线

---

## 八、环境脚本差异

### 8.1 旧版环境脚本（7 个独立 shell 文件）

| 脚本 | 用途 | 关键操作 |
|---|---|---|
| `set_vllm_env.sh` | NVIDIA vLLM | 激活 `/opt/vllm_env`，patch `faulthandler.enable()` in Ray worker |
| `set_vllm_ascend_env.sh` | Ascend vLLM | 激活 `/opt/ascend_env`，source CANN + ATB |
| `set_sglang_env.sh` | NVIDIA SGLang | 激活 `/opt/sglang_env`，patch `faulthandler` in sglang scheduler |
| `set_mindie_single_env.sh` | MindIE 单机 | CANN + ATB + MindIE + atb-models，`NPU_MEMORY_FRACTION=0.96` |
| `set_mindie_multi_env.sh` | MindIE 分布式 | 同上 + HCCL settings + `ATB_LLM_*` flags + `NPU_MEMORY_FRACTION=0.97` |
| `set_wings_ascend_env.sh` | Wings Ascend | CANN + ATB + MindIE + atb-models，`TOKENIZERS_PARALLELISM=false` |
| `set_wings_nvidia_env.sh` | Wings NVIDIA | 仅激活 `/opt/vllm_env` |

### 8.2 新版环境设置

| 引擎 | 方式 | 差异 |
|---|---|---|
| **vllm (NVIDIA)** | 无特殊 env 设置，`exec python3 -m vllm...` | 假定 engine 容器已预装好环境 |
| **vllm_ascend** | adapter 内联：`set +u; [ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ] && source ... ; set -u` | `set +u/set -u` 保护未绑定变量 |
| **sglang** | `_build_base_env_commands()`: 检查 `SGLANG_ENV_SCRIPT` 环境变量指定的脚本 | 脚本路径可配置 |
| **mindie** | `_build_env_commands()`: 内联 source CANN + ATB + MindIE | 同样有 `set +u/set -u` 保护 |

> **关键差异**：旧版硬编码绝对路径（`/opt/vllm_env`），新版假定 engine 容器已配置基础环境，仅做最小设置。

---

## 九、网络与通信

### 9.1 网络接口检测

| 功能 | 旧版 | 新版 |
|---|---|---|
| NVIDIA | `detect_network_interface()`: `netifaces` 库匹配 IP 前缀 | `NCCL_SOCKET_IFNAME` / `NETWORK_INTERFACE` 环境变量 |
| Ascend | `HCCL_SOCKET_IFNAME` 在 env 脚本中设置 | `awk '$2=="00000000"{print $1;exit}' /proc/net/route` 动态检测默认路由接口 |

### 9.2 HCCL 配置

| 功能 | 旧版 | 新版 |
|---|---|---|
| `HCCL_WHITELIST_DISABLE` | env 脚本中设置 | adapter 内联 `export HCCL_WHITELIST_DISABLE=1` |
| `HCCL_IF_IP` | `RANK_IP` 环境变量 | `$POD_IP` → Python UDP trick → `hostname -i` fallback 链 |
| Rank Table | `RANK_TABLE_PATH` 指定已有文件 | `_build_rank_table_commands()` 动态生成（解析 `HCCL_DEVICE_IPS`） |

### 9.3 Wings Router (NATS) — 两版一致

| 功能 | 状态 |
|---|---|
| `_set_router_config()` | ✅ 保留 |
| `get_router_env()` → `WINGS_ROUTER_ENABLE` | ✅ 保留 |
| router 启用时强制 vllm / vllm_ascend | ✅ 保留 |

---

## 十、模型特定配置

### 10.1 配置文件结构

| 维度 | 旧版 | 新版 |
|---|---|---|
| **文件组织** | `nvidia_default.json` + `ascend_default.json`（按设备类型分开） | ~~`vllm_default.json`（统一 NVIDIA+Ascend）+ 引擎 fallback~~ → ✅ 已迁移：`nvidia_default.json` + `ascend_default.json` 已复制到新版 config 目录，通过 `_load_default_config()` 自动合并 `model_deploy_config` |
| **索引路径** | `DEFAULT_CONFIG_FILES = {"nvidia": "nvidia_default.json", "ascend": "ascend_default.json"}` | `DEFAULT_CONFIG_FILES = {"nvidia": "vllm_default.json", "ascend": "vllm_default.json"}`（共用，通过 legacy fallback 自动加载旧版设备配置） |
| **引擎 fallback** | 无 | **新增** `_load_engine_fallback_defaults()`: 加载 `sglang_default.json` / `mindie_default.json` 作为引擎级 fallback |

### 10.2 支持的模型列表（两版一致）

| 架构 | 模型 |
|---|---|
| DeepseekV3ForCausalLM | DeepSeek-R1, R1-0528, V3, V3-0324, V3.1, 各 w8a8 变体 |
| Glm4ForCausalLM | GLM-4-9B-0414 |
| Qwen2ForCausalLM | DeepSeek-R1-Distill-Qwen-{1.5B,7B,14B,32B}, Qwen2.5-32B-Instruct, QwQ-32B |
| Qwen3ForCausalLM | Qwen3-32B |
| Qwen3MoeForCausalLM | Qwen3-30B-A3B, Qwen3-235B-A22B |
| LlamaForCausalLM | LLaMA3-8B, DeepSeek-R1-Distill-Llama-{8B,70B} |
| Qwen2_5_VLForConditionalGeneration | Qwen2.5-VL-{7B,72B}-Instruct |
| XLMRobertaModel | bge-m3 |
| BertModel | bge-large-zh-v1.5 |
| Qwen3ForCausalLM (embedding) | Qwen3-Embedding-0.6B |
| XLMRobertaForSequenceClassification | bge-reranker-v2-m3, bge-reranker-large |

### 10.3 ~~仅存在于旧版的模型扩展~~ ✅ 已迁移

| 模型 | 引擎 | 配置文件 | 说明 |
|---|---|---|---|
| Qwen3-Coder-480B-A35B-Instruct | vllm | `nvidia_default.json` | `tool_call_parser: "qwen3_xml"` |
| Qwen3-Coder-30B-A3B-Instruct | vllm | `nvidia_default.json` | `tool_call_parser: "qwen3_xml"` |
| DeepSeek-R1-w8a8 | mindie_distributed | `ascend_default.json` | MoE 并行参数 + 大 batch 优化 |
| Qwen3-235B-A22B | mindie_distributed | `ascend_default.json` | MoE 并行参数 + 大 batch 优化 |

> ✅ 已迁移：`nvidia_default.json` 和 `ascend_default.json` 已复制到 `backend/app/config/` 目录，所有模型专属配置自动生效。

### 10.4 Ascend 分布式特定模型参数

| 模型 | 引擎 | 旧版 (`ascend_default.json`) | 新版 |
|---|---|---|---|
| DeepSeek-R1-w8a8 | mindie_distributed | `maxSeqLen=16384`, `maxBatchSize=130`, `maxPrefillBatchSize=10` | 应保留（合入统一配置） |
| Qwen3-235B-A22B | mindie_distributed | `maxSeqLen=16384`, `maxBatchSize=130`, `maxIterTimes=8192` | 应保留 |

### 10.5 配置合并优先级（两版一致）

| 层级 | 说明 |
|---|---|
| 1（最低） | 硬件默认 JSON |
| 2 | 模型特定配置（architecture → model_name → engine） |
| 3 | 用户 config-file（JSON 文件或字符串） |
| 4（最高） | CLI 参数 |

差异：新版新增 `WINGS_CONFIG_DIR` 环境变量覆盖配置目录，`_load_mapping()` 额外做类型检查。

---

## 十一、CLI / 入口差异

### 11.1 入口点

| 方面 | 旧版 `wings.py` (280 行) | 新版 `start_args_compat.py` (246 行) |
|---|---|---|
| 参数解析 | `argparse` + `parse_known_args()`（未知参数穿透给引擎） | `LaunchArgs` 冻结数据类（全部显式声明，无穿透） |
| 引擎选项 | sglang, vllm, mindie, wings, transformers, xllm（6 种） | vllm, vllm_ascend, sglang, mindie（4 种） |
| 分布式参数 | `RANK_IP`, `NODE_IPS`, `MASTER_IP` | `nnodes`, `node_rank`, `head_node_addr`, `distributed_executor_backend` |
| 参数来源 | CLI 优先 | CLI + 环境变量双来源 via `_env()` 辅助函数 |

### 11.2 Shell 启动器

| 方面 | 旧版 `wings_start.sh` (615 行) | 新版 `wings_start.sh` |
|---|---|---|
| Proxy 管理 | 条件启动 `wings_proxy` 进程 | `main.py` 作为子进程管理 |
| 分布式编排 | 启动 master/worker 进程、PID 管理 | 无 — K8s 管理 Pod 生命周期 |
| 信号处理 | `trap cleanup SIGTERM SIGINT` + kill PIDs | Python `signal.signal()` + `stop_event.set()` 优雅退出 |
| 日志管理 | 日志轮转（最多 5 个）、按服务分离日志 | 简化 — 依赖 K8s 日志收集 |

---

## 十二、日志系统

### 12.1 噪声过滤

| 功能 | 旧版 | 新版 | 状态 |
|---|---|---|---|
| `_DropByRegex` logging.Filter + `_LineFilterIO` stdout/stderr wrapper | ✅ | ✅ 完全相同 | ✅ 保留 |
| `/health` 访问日志、Prefill/Decode batch、pynvml FutureWarning 过滤 | ✅ | ✅ | ✅ 保留 |
| `NOISE_FILTER_DISABLE`, `HEALTH_FILTER_ENABLE`, `BATCH_NOISE_FILTER_ENABLE` 等控制变量 | ✅ | ✅ | ✅ 保留 |
| `install_noise_filters()` 在入口调用 | ✅ `wings.py` 模块加载时 | ❌ 未在 `main.py` 中调用 | ⚠️ 差异 |

### 12.2 Speaker Logging

两版完全一致：N of M workers emit INFO，其余静默；PID-hash based。

### 12.3 日志轮转

| 功能 | 旧版 | 新版 |
|---|---|---|
| 日志文件轮转 | ✅ `wings_start.sh` 内实现，最多 5 个文件循环 | ❌ 无（依赖 K8s 日志收集） |

---

## 十三、Proxy 功能

| 功能 | 旧版 | 新版 | 状态 |
|---|---|---|---|
| Gateway 路由 (`/v1/chat/completions` 等) | ~750 行 | ~1097 行（注释增强） | ✅ 保留 |
| QueueGate 双闸门排队 | ~328 行 | ~404 行（更多文档） | ✅ 保留 |
| 重试机制 (`_RETRIABLE_EXC`, `_RETRIABLE_5XX`) | ✅ | ✅ | ✅ 保留 |
| 流式刷新 (FAST_PATH_BYTES 等) | ✅ | ✅ | ✅ 保留 |
| `simple_proxy.py` 备用轻量代理 | ❌ | ✅ 新增 | ⭐ 新版独有 |
| `http_client.py` 统一客户端创建 | ❌ | ✅ 新增 | ⭐ 新版独有 |
| Health 状态机 (0→1→-1) | ~669 行 | ~629 行 | ✅ 保留 |
| Health PID 检查 | ✅ 读取 `/var/log/wings/wings.txt` | ✅ + `WINGS_SKIP_PID_CHECK` 可跳过（sidecar 场景） | ✅ 增强 |
| MindIE health 端点 | ✅ 硬编码 `127.0.0.2:1026` | ✅ + `MINDIE_HEALTH_HOST`/`MINDIE_HEALTH_PORT` 覆盖 | ✅ 增强 |

---

## 十四、进程管理 / 安全

### 14.1 进程管理

| 功能 | 旧版 | 新版 |
|---|---|---|
| PID 写入 | `process_utils.log_process_pid()` → `logs/<name>_pid.txt` | 代码保留但不再被核心链路调用 |
| 启动等待/日志流 | `wait_for_process_startup()`, `log_stream()` 后台线程 | 代码保留但不再使用 |
| 信号处理 | 无统一处理 | `SIGINT`/`SIGTERM` → `stop_event.set()` → 优雅退出 |
| 子进程守护 | 无自动重启 | `_restart_if_needed()`: 无条件重启退出的 proxy/health |
| Engine PID 文件 | `_write_engine_second_line()` 直接写 | 增加 `os.makedirs(parent, exist_ok=True)` 安全创建目录 |

### 14.2 安全加固

| 功能 | 旧版 | 新版 |
|---|---|---|
| Shell 路径清理 | ❌ 无 | ✅ `_sanitize_shell_path()`: 正则移除 `;|&\`$(){}` |
| Shell 单引号转义 | ❌ 无 | ✅ `_shell_escape_single_quote()`: `'` → `'"'"'` |
| shlex.quote | ❌ 未使用 | ✅ `sglang_adapter.py` 参数值使用 |
| JSON 校验 | ❌ 无 | ✅ `wings_entry.py`: 验证 `WINGS_ENGINE_PATCH_OPTIONS` |
| 安全文件写入 | ✅ `safe_write_file()` 600 权限 | ✅ 完全相同 |
| 请求体大小限制 | ✅ `MAX_REQUEST_BYTES = 2MB` | ✅ 完全相同 |

---

## 十五、旧版独有功能汇总

| # | 功能 | 代码量 | 说明 |
|---|---|---|---|
| 1 | **wings 引擎（HunyuanVideo + Transformers LLM）** | ~3,242 行 | 多模态视频生成 + 通用 HF 模型推理 |
| 2 | **Master-Worker-Monitor-Scheduler 分布式** | 517 行 | 完整自建编排框架 |
| 3 | **Benchmark 测试套件** | ~2,108 行 | LLM + MMUM 性能基准测试 |
| 4 | **运行时硬件探测** (torch/pynvml/torch_npu) | ~274 行 | 实时 GPU/NPU 检测及 VRAM 采集 |
| 5 | **netifaces 网络接口检测** | adapter 内 | 改为 `/proc/net/route` + 环境变量 |
| 6 | **直接进程启动** (subprocess.Popen) | 所有 adapter | 改为 `build_start_script()` |
| 7 | **Ray Python 生命周期管理** | adapter 内 | 内联到生成的 bash 脚本 |
| 8 | **xllm 引擎选项** | CLI | 新版不支持 |
| 9 | **dp_deployment 硬编码 Ascend DeepSeek 参数** | adapter 内 | 新版简化为 `dp_size=nnodes, dp_size_local=1` |
| 10 | **VRAM 实时检查 + CUDA Graph 实测计算** | config_loader 内 | ~~新版 fallback 12GB~~ → 已修复：支持 `WINGS_DEVICE_MEMORY` 环境变量 |
| 11 | **日志轮转**（最多 5 个文件） | wings_start.sh | 新版依赖 K8s 日志 |
| 12 | **PD 测试脚本** | ~241 行 | 物理机一键 PD 部署验证 |
| 13 | ~~**`install_noise_filters()` 在入口调用**~~ | ~~wings.py~~ | ✅ 已修复：在 main.py + gateway.py + health_service.py 中调用 |

---

## 十六、新版独有功能汇总

| # | 功能 | 位置 | 用途 |
|---|---|---|---|
| 1 | **Sidecar 共享卷架构** | `main.py`, `wings_entry.py` | 跨容器命令传递 |
| 2 | **PortPlan 三层端口** | `port_plan.py` | 明确 backend/proxy/health 端口职责 |
| 3 | **LaunchArgs / LauncherPlan** | `start_args_compat.py`, `wings_entry.py` | 标准化参数传递 |
| 4 | **ManagedProc + 守护循环** | `main.py` | proxy/health 无条件自动重启 |
| 5 | **`_sanitize_shell_path()` / `shlex.quote()`** | adapter | Shell 注入防护 |
| 6 | **Triton NPU driver 补丁** | `vllm_adapter.py` | Ascend 910B + Ray 分布式 |
| 7 | **HCCL rank table 动态生成** | `mindie_adapter.py` | 解析 `HCCL_DEVICE_IPS` |
| 8 | **mindie_distributed** 配置 | `distributed_config.json` | MindIE master_port |
| 9 | **simple_proxy.py** | `proxy/` | 备用轻量代理 |
| 10 | **http_client.py** | `proxy/` | 统一 httpx 客户端创建 |
| 11 | **WINGS_SKIP_PID_CHECK** | `health.py` | sidecar 场景跳过 PID 校验 |
| 12 | **WINGS_CONFIG_DIR** | `config_loader.py` | 配置目录环境变量覆盖 |
| 13 | **ENABLE_ACCEL + WINGS_ENGINE_PATCH_OPTIONS** | `wings_entry.py` | 引擎加速补丁注入 |
| 14 | **pydantic-settings Settings** | `config/settings.py` | 全局配置单例 + `.env` 支持 |
| 15 | **`set +u / set -u` 保护** | 各 adapter | 防止 Ascend env 脚本未绑定变量报错 |
| 16 | **K8s 原生部署** | `k8s/` | 8 种 Kustomize overlay |

---

## 十七、环境变量对比（新增/变更）

| 环境变量 | 旧版 | 新版 | 说明 |
|---|---|---|---|
| `WINGS_DEVICE` / `DEVICE` | 不使用 | 必须 | sidecar 硬件检测依赖 |
| `WINGS_DEVICE_COUNT` / `DEVICE_COUNT` | 不使用 | 必须 | sidecar 硬件检测依赖 |
| `WINGS_DEVICE_NAME` | 不使用 | 可选 | 替代 pynvml/torch_npu 检测 |
| `WINGS_H20_MODEL` | 不使用 | 可选 | 替代 VRAM-based H20 检测 |
| `WINGS_CONFIG_DIR` | 不使用 | 可选 | 配置目录覆盖 |
| `WINGS_SKIP_PID_CHECK` | 不使用 | 可选 | 跳过 PID 存活检查 |
| `WINGS_ENGINE_PATCH_OPTIONS` | 不使用 | 可选 | Accel 补丁选项 |
| `ENABLE_ACCEL` | 不使用 | 可选 | 启用引擎加速补丁 |
| `KV_AGENT_LIB_PATH` | 不使用 | 可选 | 替代硬编码 LMCache 路径 |
| `LMCACHE_LIB_PATH` | 不使用 | 可选 | 替代硬编码 LMCache 路径 |
| `PD_KV_PORT` | 不使用（硬编码 `20001`） | 可选 | PD 分离端口可覆盖 |
| `MINDIE_HEALTH_HOST/PORT` | 不使用（硬编码 `127.0.0.2:1026`） | 可选 | MindIE 健康检查覆盖 |
| `HCCL_DEVICE_IPS` | 不使用 | 可选 | 动态生成 rank table |
| `POD_IP` | 不使用 | 可选 | K8s Downward API 注入 |
| `RAY_PORT` | 不使用 | 可选 | Ray head 端口覆盖 |
| `VLLM_DP_RPC_PORT` | 不使用（硬编码 `13355`） | 可选 | dp_deployment RPC 端口覆盖 |
| `SHARED_VOLUME_PATH` | 不使用 | 必须 | 共享卷路径 |
| `START_COMMAND_FILENAME` | 不使用 | 可选 | 启动脚本文件名 |
| `PROCESS_POLL_SEC` | 不使用 | 可选 | 子进程守护轮询间隔 |
| `SGLANG_ENV_SCRIPT` | 不使用 | 可选 | SGLang env 脚本路径 |
| `MINDIE_WORK_DIR` | 不使用 | 可选 | MindIE 工作目录 |
| `MINDIE_CONFIG_PATH` | 不使用 | 可选 | MindIE 配置路径 |
| `MINDIE_NPU_DEVICE_IDS` | 不使用 | 可选 | MindIE NPU 设备 ID |

---

## 十八、迁移建议

### 18.1 无需迁移（逻辑已完整保留）
- **PD 分离**：Ascend LLMDataDist + NVIDIA NIXL，完全一致
- **Soft FP8**：昇腾 DeepSeekV3 特定逻辑完整
- **Wings Router (NATS)**：KV cache 事件配置完整
- **Ascend 310 特殊逻辑**：引擎强制选择 + bfloat16 检查 + 640 权限
- **量化支持**：GPTQ / AWQ / FP8 传参完整

### 18.2 需评估是否迁移
| 能力 | 优先级 | 建议 |
|---|---|---|
| HunyuanVideo / Transformers 引擎 | 低 | 按业务需求决定 |
| Benchmark 测试套件 | 中 | 可作为独立工具 |
| 运行时硬件探测 | 低 | K8s 环境变量注入更合理 |
| 日志轮转 | 中 | K8s 有日志管理，调试场景可能需要 |

### 18.3 已在本次同步中补齐
- ✅ Accel 加速包（K8s YAML + Python 层注入 + 文档）— 已同步到 unified 版本（`wings-accel/`、`build-accel-image.sh`、`wings_entry.py` Accel 注入逻辑、`doc/deploy-accel.md`）
- ✅ `install_noise_filters()` — 在 `main.py`（launcher 进程）、`gateway.py`（proxy 子进程）、`health_service.py`（health 子进程）中均调用
- ✅ CUDA Graph 显存 fallback — 支持 `WINGS_DEVICE_MEMORY` 环境变量，替代硬编码 12GB
- ✅ Legacy config 兼容 — `_load_default_config()` 在 `vllm_default.json` 缺少 `model_deploy_config` 时，自动从旧版 `nvidia_default.json`/`ascend_default.json` 补充模型专属配置
- ✅ `npu_memory_fraction` 默认值 — 从 0.8 调整为 0.9（旧版单机 0.96、分布式 0.97）
- ✅ 模型专属配置迁移 — 将旧版 `nvidia_default.json`（336行，含 DeepSeek/Qwen3/Qwen3-Coder/H20 卡型配置）和 `ascend_default.json`（255行，含 vllm_ascend/mindie 的 MoE 并行配置）完整复制到 `backend/app/config/` 目录，通过 `_load_default_config()` legacy fallback 自动合并
- ✅ Bug 4 修复同步 — analyse 版 `wings_entry.py` 的引擎选择逻辑已从 `launch_args.engine` 覆写改为 `merged.get("engine", launch_args.engine)`，保留 `_auto_select_engine` 结果
- ✅ `ENABLE_ACCEL` 注释统一 — unified 版 `settings.py` 注释已更新为「是否启用 Accel 加速包（注入 WINGS_ENGINE_PATCH_OPTIONS）」
- ✅ Bug 5 修复同步 — analyse 版 `queueing.py` 的 `@staticmethod _queue_disabled_raise` 移除多余 `self` 参数
- ✅ Bug 6 修复同步 — analyse 版 `proxy/__init__.py` 的 `__all__` 移除不存在的 `"warmup"` 模块
- ✅ Bug 7 修复同步 — analyse 版 `start_args_compat.py` 的 `SUPPORTED_ENGINES` 添加 `"wings"`
- ✅ Bug 8-9 修复同步 — analyse 版 `wings_start.sh` PORT 导出加条件保护、CLI `--port` 改用 `$PROXY_PORT`
- ✅ Bug 10 修复同步 — analyse 版 `Dockerfile` 添加 `sed -i 's/\r//' ./wings_start.sh`
- ✅ 分布式模式同步 — analyse 版 `main.py` 新增 master/worker/standalone 角色分支，`distributed/` 模块（master.py、worker.py、scheduler.py、monitor.py）完整复制
- ✅ `install_noise_filters()` 同步 — unified 版 `main.py` 新增 `install_noise_filters()` 调用
- ✅ config_loader.py 双向同步 — analyse 版修复 4 个 bug（free_memory KeyError、return vllm→"vllm_ascend"×2、nodes→nnodes），unified 版新增 2 个特性（legacy config fallback、WINGS_DEVICE_MEMORY）

---

*报告生成时间：基于逐文件逐函数源码分析，覆盖 wings/wings/ 全部 40+ 文件和 backend/app/ 全部对应文件。*

*最后更新：2026-03-10 — 全量双向同步完成。Accel 加速包、分布式模式、全部 Bug 修复已双向同步。*
