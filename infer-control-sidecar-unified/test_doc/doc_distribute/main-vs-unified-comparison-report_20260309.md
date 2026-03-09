# Main vs Unified 项目对比分析报告

**日期**: 2026-03-09  
**对比目标**: 分析 `infer-control-sidecar-main`（早期昇腾验证版本）与 `infer-control-sidecar-unified`（当前统一版本）在 ST 场景中的单机和分布式处理逻辑差异  
**对比范围**: MindIE Adapter、vLLM-Ascend Adapter、config_loader、K8s 部署模板

---

## 1. 项目结构差异

| 维度 | **main** (早期版本) | **unified** (当前版本) |
|------|---------------------|------------------------|
| 后端代码目录 | `backend-dist-nv-20260303/app/` (场景专用目录) | `backend/app/` (统一目录) |
| K8s 资源 | 扁平 `k8s/` 目录，独立 YAML 文件 | 结构化 `k8s/base/` + `k8s/overlays/` (Kustomize) |
| 多场景支持 | 一个 backend 对应一个场景，暗示需多个 backend-* 目录 | 单一 backend 覆盖所有场景 |
| 标签方案 | 自定义 `app: infer-st-dist` | 标准 `app.kubernetes.io/name` Labels |

---

## 2. MindIE Adapter 对比

### 2.1 环境脚本加载 (`_build_env_commands`)

**main** 版本加载 2 个环境脚本：
```python
# main: mindie_adapter.py
"[ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ] && source ..."
"[ -f /usr/local/Ascend/mindie/set_env.sh ] && source ..."
```

**unified** 版本加载 **4 个**环境脚本 + 额外的 LD_LIBRARY_PATH 和 GRPC_POLL_STRATEGY：
```python
# unified: mindie_adapter.py
"[ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ] && source ..."
"[ -f /usr/local/Ascend/mindie/set_env.sh ] && source ..."
"[ -f /usr/local/Ascend/atb-models/set_env.sh ] && source ..."     # ← 新增
"[ -f /usr/local/Ascend/nnal/atb/set_env.sh ] && source ..."       # ← 新增
"export LD_LIBRARY_PATH=\"/usr/local/Ascend/driver/lib64/driver"    # ← 新增
    ":/usr/local/Ascend/driver/lib64/common:${LD_LIBRARY_PATH:-}\""
"export GRPC_POLL_STRATEGY=poll"                                     # ← 新增
```

**影响**: unified 额外加载了 `atb-models/set_env.sh` 和 `nnal/atb/set_env.sh`（MindIE 官方 boot.sh 所需），并显式设置了驱动 lib 路径和 gRPC 策略。main 版本缺少这些，可能导致某些 MindIE 版本找不到 ATB 模型库或驱动 so。

### 2.2 分布式环境变量 (`_build_distributed_env_commands`)

| 差异点 | **main** | **unified** |
|--------|----------|-------------|
| HCCL_SOCKET_IFNAME | 硬编码 `eth0` | `os.getenv('HCCL_SOCKET_IFNAME', 'eth0')` |
| GLOO_SOCKET_IFNAME | 硬编码 `eth0` | `os.getenv('GLOO_SOCKET_IFNAME', 'eth0')` |
| MINDIE_MASTER_PORT | 常量 `27070` | `os.getenv("MINDIE_MASTER_PORT", "27070")` |

**影响**: unified 版本在非 `eth0` 网络接口环境中更灵活。

### 2.3 MindIE 守护进程启动方式（关键差异！）

**main** 使用 `exec` 直接替换 shell 进程：
```bash
cd '{MINDIE_WORK_DIR}'
exec ./bin/mindieservice_daemon
```

**unified** 使用后台启动 + wait 模式（仿照 MindIE 官方 boot.sh）：
```bash
cd '{MINDIE_WORK_DIR}'
./bin/mindieservice_daemon &
MINDIE_PID=$!
echo "[mindie] Daemon started as PID $MINDIE_PID"
wait $MINDIE_PID
exit_code=$?
if [ $exit_code -ne 0 ]; then
    echo "[mindie] ERROR: daemon exited with code $exit_code"
fi
exit $exit_code
```

**影响**: 
- main 的 `exec` 方式在容器环境中会导致 daemon fork 后容器退出（CrashLoopBackOff）
- unified 的 `& wait` 模式更健壮，可以捕获退出码并打印错误信息
- 此修复来源于对 MindIE 官方 `boot.sh` 的分析

### 2.4 安全加固（unified 新增）

unified 版本引入了 `_sanitize_shell_path()` 函数，对路径做正则过滤：
```python
def _sanitize_shell_path(path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9/_.-]", "", path)
```
应用于 `MINDIE_WORK_DIR` 和 `MINDIE_CONFIG_PATH`，防止命令注入。main 版本无此防护。

### 2.5 config.json 合并逻辑

两个版本的 config.json **合并策略完全一致**——都采用 merge-update：
1. 读取镜像原始 config.json
2. 加载 overrides JSON
3. 仅更新指定字段，保留 LogConfig、ScheduleConfig.templateType 等未修改字段
4. 写回

覆盖的配置区块完全相同：ServerConfig、BackendConfig、ModelDeployConfig、ModelConfig[0]、ScheduleConfig。

---

## 3. vLLM-Ascend Adapter 对比

### 3.1 单机模式

两个版本对单机 vllm_ascend 的环境设置**完全一致**：
```bash
set +u
[ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ] && source ... || echo 'WARN: ...'
[ -f /usr/local/Ascend/nnal/atb/set_env.sh ] && source ... || echo 'WARN: ...'
set -u
exec python3 -m vllm.entrypoints.openai.api_server ...
```

### 3.2 Ray 分布式逻辑

两个版本的 Ray 分布式逻辑**几乎完全一致**，包括：
- Ascend CANN 环境加载块
- Triton NPU 驱动补丁（完全相同的 `_NpuDummyDrv` patch）
- rank-0: `ray start --head --resources='{"NPU": 1}' ...`
- rank-0: 等待 worker 加入（60 轮 × 5 秒 = 5 分钟超时）
- rank-0: `exec cmd --enforce-eager --distributed-executor-backend ray`
- rank>0: NODE_IPS 扫描探测 Ray Head IP（120 轮 × 5 秒 = 10 分钟超时）
- rank>0: `exec ray start --address=$HEAD_IP:6379 --resources='{"NPU": 1}' --block`

**关键差异汇总：**

| 差异点 | **main** | **unified** |
|--------|----------|-------------|
| Ray 端口 | 硬编码 `6379` | `os.getenv("RAY_PORT", "6379")` |
| DP RPC 端口 | 硬编码 `13355` | `os.getenv('VLLM_DP_RPC_PORT', '13355')` |
| NV Worker VLLM_HOST_IP | **未设置** | 新增 `VLLM_HOST_IP=${POD_IP:-...}` + socket 探测 |
| NV Head VLLM_HOST_IP | `hostname -i` | `${POD_IP:-socket_trick}` (与 Ascend 对齐) |
| NCCL_SOCKET_IFNAME | 硬编码 `eth0` | `os.getenv('NCCL_SOCKET_IFNAME', 'eth0')` |
| OMP_NUM_THREADS | 硬编码 `100` | `os.getenv('OMP_NUM_THREADS', '100')` |
| NPU_MAX_SPLIT_SIZE_MB | 硬编码 `256` | `os.getenv('NPU_MAX_SPLIT_SIZE_MB', '256')` |
| KVCache lib path | 硬编码路径 | 从 env `KV_AGENT_LIB_PATH` / `LMCACHE_LIB_PATH` 可覆盖 |
| 安全加固 | 无 | `_sanitize_shell_path()` |

**unified 重要修复**: NV Worker 节点新增了 `VLLM_HOST_IP` 设置，修复了 hostNetwork 模式下 Ray Worker 注册使用错误 IP 的潜在问题。

### 3.3 PD 分离环境变量（unified 增强）

unified 版本在 PD 分离的 vllm_ascend 场景下增加了可配置性：
- `VLLM_LLMDD_RPC_PORT` 从 env 读取（硬编码 → 可覆盖）
- `OMP_NUM_THREADS` 从 env 读取
- `PYTORCH_NPU_ALLOC_CONF` 的 `max_split_size_mb` 从 env 读取
- PD KV 端口从 `os.getenv("PD_KV_PORT", "20001")` 读取

---

## 4. config_loader.py 对比

### 4.1 `_merge_mindie_params` 函数

两个版本的 MindIE 参数合并逻辑**完全一致**：
- 参数映射表加载
- MTP（mtp.safetensors 检测）
- MOE（deepseek-r1-671b 检测）
- 分布式模式: `worldSize = device_count`，`multiNodesInferEnabled = False`
- 单机模式: `worldSize` 由 `_adjust_tensor_parallelism` 自动设置

### 4.2 `_get_pd_config` 差异

| 差异点 | **main** | **unified** |
|--------|----------|-------------|
| PD KV 端口 | 硬编码 `"20001"` | `os.getenv("PD_KV_PORT", "20001")` |

### 4.3 `_set_soft_fp8` 代码修复

main 版本有缩进不规范问题（虽然 Python 按括号匹配实际结果相同），unified 修复为清晰的字典结构。

---

## 5. K8s YAML 差异

### 5.1 MindIE 分布式 StatefulSet

| 维度 | **main** | **unified（修复后）** |
|------|----------|----------------------|
| driver 挂载 | `/usr/local/Ascend/driver/lib64` (仅 lib64) | `/usr/local/Ascend/driver` (完整目录) + lib64 |
| hccn-conf 挂载 | ✓（Pod YAML 中） | ✓（已补全） |
| npu-smi 挂载 | ✓（Pod YAML 中） | ✓（已补全） |
| dshm 挂载 | ✓（Pod YAML 中） | ✓（已补全） |
| HCCL_DEVICE_IPS env | ✓（Pod YAML 中） | ✓（已补全，CHANGE_ME 占位符） |
| PATH env（engine） | 无 | ✓（已补全，包含 /usr/local/sbin） |
| livenessProbe | ✓ | ✓（已补全） |
| 引擎资源 limit | `cpu:16, mem:48Gi` | `cpu:24, mem:64Gi` |
| Namespace | 硬编码 `wings-verify-st-dist` | Kustomize 注入 |

### 5.2 vLLM-Ascend 分布式 StatefulSet

| 维度 | **main** | **unified** |
|------|----------|-------------|
| podManagementPolicy | `Parallel` ✓ | `Parallel` ✓ |
| dshm (2Gi) | ✓ | ✓ |
| POD_IP DownwardAPI | ✓ | ✓ |
| ascend-driver 路径 | `driver/lib64/driver` | 相同 ✓ |
| readiness + liveness | ✓ | ✓ |
| 端口 | 18100/19100 (避免 mindie 冲突) | 18000/19000 (标准端口) |

**结论**: vllm-ascend 分布式模板**结构完整**，两个版本一致。端口差异是因为 main 中 mindie 和 vllm-ascend 共存于同一 hostNetwork 环境。

### 5.3 vLLM-Ascend 单机 Deployment

| 维度 | **main** | **unified（修复后）** |
|------|----------|----------------------|
| dshm 挂载 | 无 | ✓（已补全） |
| 其他配置 | 一致 | 一致 |

---

## 6. 修复记录

### 已完成的修复（本次对比过程中）

| # | 修复内容 | 文件 | 说明 |
|---|---------|------|------|
| 1 | 添加 hccn-conf volume + mount | `k8s/overlays/mindie-distributed/statefulset.yaml` | HCCL 网络配置，分布式必需 |
| 2 | 添加 npu-smi volume + mount | 同上 | NPU 管理工具，MindIE 拓扑检测需要 |
| 3 | 添加 dshm volume + mount | 同上 | 共享内存 2Gi |
| 4 | 添加 HCCL_DEVICE_IPS env | 同上 wings-infer 容器 | 分布式 rank table 的 device_ip 字段 |
| 5 | 添加 PATH env | 同上 engine 容器 | 包含 /usr/local/sbin（npu-smi 路径） |
| 6 | 添加 livenessProbe | 同上 wings-infer 容器 | 180s 初始延迟，30s 周期 |
| 7 | 添加 dshm volume + mount | `k8s/overlays/vllm-ascend-single/deployment.yaml` | 与分布式版本保持一致 |

### 之前已完成的修复（MindIE 单机验证过程中）

| # | 修复内容 | 文件 | 说明 |
|---|---------|------|------|
| 1 | 新增 atb-models、nnal/atb env 脚本 | `backend/app/engines/mindie_adapter.py` | 官方 boot.sh 所需 |
| 2 | 新增 driver lib64 路径 + GRPC_POLL_STRATEGY | 同上 | daemon 运行必需 |
| 3 | exec → background + wait 启动方式 | 同上 | 修复容器 CrashLoopBackOff |
| 4 | 添加 npu-smi + PATH | `k8s/overlays/mindie-single/mindie-single-deploy.yaml` | npu-smi 拓扑检测 |

---

## 7. 总结

### main 版本的问题
1. MindIE 环境脚本不完整（缺 atb-models、nnal/atb）
2. MindIE daemon 启动方式（exec）导致容器退出
3. NV Worker 无 VLLM_HOST_IP（hostNetwork 下 Ray 注册可能用错 IP）
4. 全部硬编码端口/接口，灵活性差
5. 无路径安全防护

### unified 版本改进
1. ✅ 环境脚本完整（4 个 set_env.sh + driver 路径 + GRPC 策略）
2. ✅ daemon 启动方式修复（& wait 模式）
3. ✅ NV Worker VLLM_HOST_IP 修复
4. ✅ 所有端口/接口参数支持环境变量覆盖
5. ✅ 路径安全加固（_sanitize_shell_path）
6. ✅ K8s 模板已补全缺失的挂载（hccn-conf、npu-smi、dshm）和环境变量

### 仍需注意
- MindIE 分布式部署中 `HCCL_DEVICE_IPS` 需按实际的 RoCE 设备 IP 配置
- 分布式测试需等待 .110 CephFS 可用
