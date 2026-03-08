# engines/ 全部适配器逐行 Diff 分析报告

> **A（原版）**: `wings/wings/engines/` — vllm 631行 + sglang 170行 + mindie 297行 = **1098 行**  
> **B（统一版）**: `infer-control-sidecar-unified/backend/app/engines/` — vllm 694行 + sglang 235行 + mindie 625行 = **1554 行**  
> **增量**: +456 行（+41.5%），主要来自 `build_start_script()` 引入 + docstring 补全

---

## 一、架构级核心变更：`start_engine()` → `build_start_script()`

这是 engines/ 模块迁移中**最本质的变化**，贯穿全部 3 个适配器。

### A（原版）的执行模型
```
Wings 进程  →  subprocess.Popen(["/bin/bash", "-c", cmd])  →  引擎进程（在同一容器内）
```
- 引擎进程是 Wings 进程的子进程
- 通过 `wait_for_process_startup()` 轮询 stdout 等待启动
- 通过 `log_stream()` 转发子进程日志

### B（统一版）的执行模型
```
Sidecar 容器  →  build_start_script()  →  /shared/start_command.sh  →  Engine 容器读取并 exec
```
- Sidecar **永不启动引擎进程**
- 仅生成 bash 脚本写入共享卷
- Engine 容器的 entrypoint 执行该脚本
- `start_engine()` 抛出 `RuntimeError` 防止误调用

### 影响范围

| 函数/概念 | A（原版） | B（统一版） |
|-----------|---------|-----------|
| `start_engine()` | 启动子进程 | `raise RuntimeError` |
| `build_start_script()` | 不存在 | **核心入口**，返回 bash 脚本体 |
| `build_start_command()` | 不存在 | 兼容接口，返回核心命令 |
| `subprocess.Popen` | 大量使用 | 完全移除 |
| `wait_for_process_startup()` | 用于等待引擎就绪 | 不使用（由 health_service 替代） |
| `log_stream()` | 转发子进程日志 | 不使用（容器自有日志） |
| `log_process_pid()` | 记录子进程 PID | 不使用 |
| `import subprocess` | 是 | 移除 |
| `import time` | 是 | 移除 |

---

## 二、vllm_adapter.py 详细 Diff

### 文件概况

| 指标 | A | B | 变化 |
|------|---|---|------|
| 总行数 | 631 | 694 | +63 (+10%) |
| 函数数 | 23 | 13 | -10 |
| 进程管理函数 | 10 | 0 | 全部移除 |
| 脚本生成函数 | 0 | 2 | 全部新增 |

### 2.1 移除的函数（仅 A）

以下 10 个函数涉及直接进程管理，B 中全部移除：

| 函数名 | 职责 | 行数 |
|--------|------|------|
| `_start_vllm_single()` | 单机启动 subprocess | 25 |
| `_start_vllm_api_server()` | API Server subprocess + 日志 | 20 |
| `_start_ray_node()` | Ray 节点 subprocess | 35 |
| `_build_ray_command()` | Ray CLI 命令构建 | 20 |
| `start_vllm_distributed()` | 分布式入口分发 | 12 |
| `_start_vllm_with_ray()` | Ray 模式启动编排 | 35 |
| `_start_ray_head_node()` | Head 节点启动 + 等待 | 35 |
| `_start_ray_worker_node()` | Worker 节点启动 | 25 |
| `_start_vllm_with_dp_deployment()` | DP 模式启动 | 20 |
| `detect_network_interface()` | netifaces 网络接口检测 | 10 |
| `wait_until_ray_head_ready()` | 阻塞等待 Ray Head | 15 |
| `wait_until_all_workers_joined()` | 阻塞等待全部 Worker | 15 |
| `check_node_joined()` | 检查节点加入集群 | 15 |

### 2.2 新增的函数（仅 B）

| 函数名 | 职责 | 行数 |
|--------|------|------|
| `_sanitize_shell_path()` | 防止命令注入 | 8 |
| `build_start_command()` | 兼容接口，返回命令字符串 | 10 |
| `build_start_script()` | **核心入口**，生成完整 bash 脚本 | ~200 |

### 2.3 `build_start_script()` 详解

B 中最重要的新函数，支持 5 种部署模式的脚本生成：

```
if distributed && nnodes > 1:
    if backend == "ray":
        if node_rank == 0:    → Ray head 脚本（启动 ray, 等待 worker, exec vllm）
        else:                 → Ray worker 脚本（探测 head, exec ray start --block）
    else (dp_deployment):
        if node_rank == 0:    → DP rank0 脚本
        else:                 → DP rankN 脚本
elif engine == "vllm_ascend":   → 单机 Ascend 脚本（含 CANN env）
else:                           → 单机 NVIDIA 脚本（直接 exec）
```

**Ascend 特有增强**：
- **Triton NPU 驱动补丁**：80+ 行内联 Python 脚本，在 worker 启动前修补 `triton/runtime/driver.py`，解决 "0 active drivers" 崩溃
- **HCCL 环境变量**：`HCCL_IF_IP`, `HCCL_WHITELIST_DISABLE`, `HCCL_SOCKET_IFNAME`
- **Ray NPU 资源**：使用 `--resources='{"NPU": 1}'` 代替 `--num-gpus=1`
- **POD_IP 检测**：使用 K8s Downward API `${POD_IP}` 代替 `hostname -i`（vllm-ascend 容器无 `ip` 命令）
- **Worker 动态 Head 发现**：扫描 `NODE_IPS` 尝试 socket 连接，替代 A 中硬编码 head IP

### 2.4 已有函数变更

#### `_build_base_env_commands()`

```diff
  # A — 硬编码路径
- env_commands.append(f"source {root}/wings/config/set_vllm_env.sh")
  # B — 动态检测 + 回退
+ local_script = os.path.join(config_dir, "set_vllm_env.sh")
+ if os.path.exists(local_script):
+     env_commands.append(f"source {local_script}")
+ # vllm_ascend 不存在本地脚本时回退到容器标准路径
+ else:
+     env_commands.append("source /usr/local/Ascend/ascend-toolkit/set_env.sh")
```

#### `_build_cache_env_commands()`

```diff
  # A — 硬编码库路径
- lib_path = "/opt/vllm_env/lib/python3.10/site-packages/kv_agent/lib"
  # B — 环境变量可配 + 路径清洗
+ lib_path = _sanitize_shell_path(os.getenv("KV_AGENT_LIB_PATH", "..."))
```

#### `_build_pd_role_env_commands()`

```diff
  # A — 硬编码值
- f"export OMP_NUM_THREADS=100",
- "export PYTORCH_NPU_ALLOC_CONF=max_split_size_mb:256"
  # B — 环境变量可配
+ f"export OMP_NUM_THREADS={os.getenv('OMP_NUM_THREADS', '100')}",
+ f"export PYTORCH_NPU_ALLOC_CONF=max_split_size_mb:{os.getenv('NPU_MAX_SPLIT_SIZE_MB', '256')}"
```

#### `_build_distributed_env_commands()`

```diff
  # A — 完整实现（NCCL/HCCL 环境变量设置）
- def _build_distributed_env_commands(...): # ~40 行逻辑
  # B — 空实现（分布式逻辑已移入 build_start_script 内联处理）
+ def _build_distributed_env_commands(...):
+     return []
```

#### `_build_vllm_cmd_parts()`

```diff
  # A
- cmd_parts = ["python", "-m", "vllm.entrypoints.openai.api_server"]
  # B — python3 确保可找到正确的解释器
+ cmd_parts = ["python3", "-m", "vllm.entrypoints.openai.api_server"]
  # B 新增 — 空字符串过滤 + max_num_batched_tokens 校验
+ if isinstance(value, str) and not value.strip():
+     continue  # 避免 --quantization '' 之类的无效参数
+ if arg == "max_num_batched_tokens":
+     try:
+         if int(value) <= 0: continue
+     except (TypeError, ValueError): continue
```

#### `_build_vllm_command()`

```diff
  # A — 使用 netifaces SDK
- network_interface = detect_network_interface(current_ip)
  # B — 环境变量替代 SDK
+ network_interface = os.getenv("NETWORK_INTERFACE", os.getenv("GLOO_SOCKET_IFNAME", "eth0"))
```

---

## 三、sglang_adapter.py 详细 Diff

### 文件概况

| 指标 | A | B | 变化 |
|------|---|---|------|
| 总行数 | 170 | 235 | +65 (+38%) |
| 函数数 | 4 | 7 | +3 |
| 进程管理函数 | 2 | 0 | 全部移除 |
| 脚本生成函数 | 0 | 2 | 全部新增 |

### 3.1 架构重构

| A | B |
|---|---|
| `_build_sglang_command()` 混合 env+cmd | `_build_sglang_cmd_parts()` 纯命令 + `_build_base_env_commands()` 纯环境 |
| `start_sglang_distributed()` subprocess | `build_start_command()` + `build_start_script()` 脚本生成 |
| `start_engine()` subprocess.Popen | `start_engine()` raise RuntimeError |

### 3.2 关键变更

#### 命令构建重构

```diff
  # A — 混合环境和命令
- def _build_sglang_command(params):
-     env_commands = [f"source {root_dir}/wings/config/set_sglang_env.sh"]
-     cmd_parts = ["python", "-m", "sglang.launch_server"]
-     # ... inline distributed logic ...
-     full_command = " && ".join(env_commands) + " && " + " ".join(cmd_parts)

  # B — 分离 + 安全增强
+ def _build_sglang_cmd_parts(params):
+     cmd_parts = ["python3", "-m", "sglang.launch_server"]  # python3
+     for arg, value in engine_config.items():
+         if isinstance(value, str) and not value.strip():
+             continue  # 空字符串过滤
+         cmd_parts.extend([arg_name, shlex.quote(str(value))])  # shlex 安全转义
```

#### 分布式参数化

```diff
  # A — 运行时检测
- current_ip = get_local_ip()
- network_interface = detect_network_interface(current_ip)
- node_rank = nodes.index(current_ip)  # 从 IP 列表查找 rank
- master_ip = get_master_ip()

  # B — 参数传入
+ nnodes = params.get("nnodes", 1)
+ node_rank = params.get("node_rank", 0)
+ head_node_addr = params.get("head_node_addr", "127.0.0.1")
+ sglang_dist_port = os.getenv("SGLANG_DIST_PORT", "28030")
```

#### 环境脚本容错

```diff
  # A — 无检查
- env_commands = [f"source {root_dir}/wings/config/set_sglang_env.sh"]

  # B — 存在性检查 + 警告
+ def _build_base_env_commands(params, root):
+     env_script = os.path.join(root, "wings", "config", "set_sglang_env.sh")
+     if os.path.exists(env_script):
+         return [f"source {env_script}"]
+     logger.warning("SGLang env script not found at %s", env_script)
+     return []
```

### 3.3 移除的导入

```diff
- import subprocess
- from wings.utils.process_utils import log_process_pid, wait_for_process_startup, log_stream
- from wings.engines.vllm_adapter import detect_network_interface
- from wings.utils.env_utils import get_master_ip, get_local_ip
+ import re, shlex  # 新增：安全处理
```

---

## 四、mindie_adapter.py 详细 Diff

### 文件概况

| 指标 | A | B | 变化 |
|------|---|---|------|
| 总行数 | 297 | 625 | +328 (+110%) |
| 函数数 | 11 | 7 | -4 |
| 架构 | 直接修改文件 + subprocess | 生成 bash 脚本（含内联 Python） |

### 4.1 最关键的架构差异

**A：Sidecar 内直接修改 config.json**
```python
# A 在 sidecar 容器内读写引擎配置
with open(config_path, 'r') as f:
    config = json.load(f)
_update_distributed_config(config, params)  # 修改 dict
safe_write_file(config_path, config, is_json=True)  # 写回
# 然后 subprocess.Popen 启动引擎
```

**B：生成 bash 脚本，由引擎容器在执行时修改 config.json**
```python
# B 生成一段内联 Python 代码编入 bash 脚本
script = f"""
cat > /tmp/_mindie_overrides.json << 'OVERRIDES_EOF'
{overrides_json}
OVERRIDES_EOF

python3 << 'MERGE_SCRIPT_EOF'
import json
# 读取引擎容器内的 config.json → 合并覆盖 → 写回
config['ServerConfig'].update(ov['server'])
...
MERGE_SCRIPT_EOF

cd {MINDIE_WORK_DIR}
exec ./bin/mindieservice_daemon
"""
```

**意义**：A 中 sidecar 需要直接访问引擎容器的配置文件（同一 Pod 内共享文件系统）。B 中配置修改延迟到引擎容器执行时，更符合 Sidecar 容器分离原则——sidecar 不需要知道引擎容器的文件系统细节。

### 4.2 移除的函数（仅 A）

| 函数名 | 职责 |
|--------|------|
| `_setup_mindie_environment()` | 设置环境变量命令 |
| `_update_mindie_config()` | 读写 config.json |
| `_update_server_config()` | 修改 ServerConfig 节 |
| `_update_model_deploy_config()` | 修改 ModelDeployConfig |
| `_update_schedule_config()` | 修改 ScheduleConfig |
| `_update_distributed_config()` | 修改分布式配置 |
| `_update_single_config()` | 修改单机配置 |
| `_start_mindie_process()` | subprocess.Popen 启动 |
| `_start_mindie_with_cmd()` | 编排环境+配置+启动 |

### 4.3 新增的函数（仅 B）

| 函数名 | 职责 | 行数 |
|--------|------|------|
| `_sanitize_shell_path()` | 路径安全清洗 | 8 |
| `_build_env_commands()` | 环境脚本加载（含 CANN 回退） | 30 |
| `_build_distributed_env_commands()` | HCCL 分布式环境变量 | 50 |
| `_build_rank_table_commands()` | 生成 HCCL rank table JSON | 55 |
| `build_start_command()` | 返回核心启动命令 | 5 |
| `build_start_script()` | **核心入口**，~180 行 | 180 |

### 4.4 新增常量（仅 B）

```python
MINDIE_WORK_DIR = _sanitize_shell_path(os.getenv("MINDIE_WORK_DIR",
    "/usr/local/Ascend/mindie/latest/mindie-service"))
MINDIE_CONFIG_PATH = _sanitize_shell_path(os.getenv("MINDIE_CONFIG_PATH",
    os.path.join(MINDIE_WORK_DIR, "conf/config.json")))
DEFAULT_SERVER_PORT = 18000
DEFAULT_MINDIE_MASTER_PORT = int(os.getenv("MINDIE_MASTER_PORT", "27070"))
```

A 中这些值全部硬编码在函数内部。

### 4.5 HCCL Rank Table 生成

B 中完全新增的功能，A 中依赖外部提供 `RANK_TABLE_PATH`：

```diff
  # A — 依赖外部提供 rank table
- rank_table_path = os.getenv('RANK_TABLE_PATH')
- if not rank_table_path:
-     raise ValueError("RANK_TABLE_PATH environment variable not set")
- os.chmod(rank_table_path, 0o640)

  # B — 自动生成 rank table（单节点子表）
+ def _build_rank_table_commands(node_ips, device_count, output_path, node_offset):
+     # 解析 HCCL_DEVICE_IPS 环境变量
+     # 生成单节点 rank table JSON
+     # 通过 heredoc 写入文件
+     return [f"cat > {output_path} << 'RANK_TABLE_EOF'", rank_table_json, "RANK_TABLE_EOF"]
```

**设计要点**：生成**单节点 rank table**（server_count=1），使得 `worldSize % n_nodes == 0` 校验始终通过。跨节点 DP 协调由 K8s StatefulSet 外部处理。

### 4.6 配置合并方式对比

| 维度 | A | B |
|------|---|---|
| 时机 | Sidecar 启动时 | Engine 容器执行脚本时 |
| 方式 | Python 直接文件 I/O | bash heredoc + 内联 Python |
| 原始配置保留 | `config[key].update()` | `config[key].update(ov)` — 相同逻辑 |
| 配置项 | 分散在 6 个 `_update_*` 函数 | 集中在 `build_start_script` 的 overrides_dict |
| 权限设置 | `os.chmod(config_path, 0o640)` | 内联 `os.chmod(CONFIG_PATH, 0o640)` |

配置项覆盖完全一致（ServerConfig / BackendConfig / ModelDeployConfig / ModelConfig / ScheduleConfig），包括 MOE/MTP 特殊处理。

### 4.7 `multiNodesInferEnabled` 取值差异

```diff
  # A — 分布式模式设为 True
- 'multiNodesInferEnabled': True,

  # B — 始终设为 False
+ "multiNodesInferEnabled": engine_config.get("multiNodesInferEnabled", False),
```

B 中有详细注释说明原因：

> MindIE's ConfigManager auto-updates worldSize when `multiNodesInferEnabled=True`, setting it to total_ranks (from rank table), which causes "Invalid DP number per node: 0" when local_devices < total_ranks. Multi-node coordination is handled by ms_coordinator/ms_controller at a higher level.

---

## 五、跨适配器的共性变更

### 5.1 安全增强

全部 3 个适配器新增 `_sanitize_shell_path()`：

```python
def _sanitize_shell_path(path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9/_.-]", "", path)
```

用于清洗所有用户输入路径，防止 shell 注入。

### 5.2 `python` → `python3`

全部 3 个适配器：

```diff
- cmd_parts = ["python", "-m", "vllm.entrypoints.openai.api_server"]
+ cmd_parts = ["python3", "-m", "vllm.entrypoints.openai.api_server"]
```

原因：官方引擎镜像保证 `python3` 可用，但 `python` 可能不存在（如仅安装了 python3.10 包而未创建 python 软链接）。

### 5.3 空字符串参数过滤

vllm 和 sglang 适配器新增：

```python
if isinstance(value, str) and not value.strip():
    continue  # 避免生成 --quantization '' 之类的无效参数
```

### 5.4 环境变量化

| 原硬编码 | 新环境变量 | 适配器 |
|---------|-----------|-------|
| lib_path 硬编码 | `KV_AGENT_LIB_PATH` / `LMCACHE_LIB_PATH` | vllm |
| `OMP_NUM_THREADS=100` | `OMP_NUM_THREADS` | vllm |
| `max_split_size_mb:256` | `NPU_MAX_SPLIT_SIZE_MB` | vllm |
| `detect_network_interface()` | `NETWORK_INTERFACE` | vllm |
| MindIE 工作目录 | `MINDIE_WORK_DIR` | mindie |
| MindIE 配置路径 | `MINDIE_CONFIG_PATH` | mindie |
| 分布式主节点端口 | `MINDIE_MASTER_PORT` | mindie |
| SGLang 分布式端口 | `SGLANG_DIST_PORT` | sglang |
| Ray 端口 | `RAY_PORT` | vllm |
| DP RPC 端口 | `VLLM_DP_RPC_PORT` | vllm |

### 5.5 统一的 API 契约

全部 3 个适配器实现相同的接口：

```python
def build_start_script(params: Dict) -> str:   # 主入口：生成 bash 脚本体
def build_start_command(params: Dict) -> str:   # 兼容接口：仅命令字符串
def start_engine(params: Dict):                 # 禁用：raise RuntimeError
```

---

## 六、总结

### 行数变化

| 适配器 | A | B | 增量 |
|--------|---|---|------|
| vllm_adapter.py | 631 | 694 | +63 (+10%) |
| sglang_adapter.py | 170 | 235 | +65 (+38%) |
| mindie_adapter.py | 297 | 625 | +328 (+110%) |
| **合计** | **1098** | **1554** | **+456 (+41.5%)** |

### 变更类型统计

| 变更类型 | vllm | sglang | mindie | 合计 |
|---------|------|--------|--------|------|
| 移除 subprocess 函数 | 10 | 2 | 3 | 15 |
| 新增脚本生成函数 | 3 | 4 | 6 | 13 |
| 安全增强 | 3 | 2 | 2 | 7 |
| 环境变量化 | 5 | 1 | 3 | 9 |
| Ascend 专项增强 | Triton patch、HCCL、Ray NPU | — | Rank table 生成 | — |

### 质量评估

| 维度 | 评分 | 说明 |
|------|------|------|
| 功能完整性 | `A+` | 所有引擎、所有部署模式（单机/Ray/DP/PD）完整支持 |
| 架构正确性 | `A+` | 完全遵循 Sidecar 契约，无进程泄漏风险 |
| 安全性 | `A` | `_sanitize_shell_path` + `shlex.quote` 防注入 |
| K8s 适配 | `A+` | POD_IP、/proc/net/route、NPU 资源声明等原生支持 |
| 可维护性 | `A` | 统一 3 个适配器 API + 详尽 docstring |

### 风险点

1. **`multiNodesInferEnabled=False`**（mindie）：与 A 的 `True` 不同。B 的注释说明了原因（避免 ConfigManager 自动修改 worldSize），但如果 MindIE SDK 版本更新改变了此行为，需重新验证。

2. **vllm_ascend Triton 补丁**：80+ 行内联 Python 直接修改 triton 库源码。当 triton/vllm-ascend 升级后补丁可能失效。

3. **环境脚本路径依赖**：B 仍尝试加载 `{root}/wings/config/set_*.sh`，但 Sidecar 容器内可能没有这些文件（已有 fallback 到容器标准路径，不会崩溃）。
