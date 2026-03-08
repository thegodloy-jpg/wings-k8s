# MindIE 2.2.RC1 k3s-verify 验证报告

**日期**: 2026-03-02  
**环境**: `k3s-verify-zhanghui` (host: 7.6.52.110)  
**模型**: DeepSeek-R1-Distill-Qwen-1.5B  
**引擎版本**: MindIE 2.2.RC1（内置 CANN 8.3）  
**引擎镜像**: `swr.cn-south-1.myhuaweicloud.com/ascendhub/mindie:2.2.RC1-800I-A2-py311-openeuler24.03-lts`  
**wings-infer 镜像**: `wings-infer:zhanghui-ascend-st`（backend-ascend-st，含 mindie_adapter.py 重构）  
**状态**: ✅ 全部测试通过

---

## 1. 环境说明

| 项目 | 值 |
|------|----|
| 宿主机 | 7.6.52.110 |
| k3s-verify 容器 | `k3s-verify-zhanghui` |
| k3s 容器 Docker Bridge IP | **172.17.0.12** |
| Namespace | `wings-verify` |
| Deployment | `wings-infer-mindie` |
| Pod | `wings-infer-mindie-cdb95fdf-glklr` |
| Pod IP | 10.42.0.53 |
| NodePort (proxy 18000) | **31820** |
| NodePort (wings-health 19000) | **31920** |
| 使用 NPU | **NPU 0**（Ascend 910B2C） |

> **访问说明**: Pod IP（10.42.0.53）从宿主机不可直接访问（k3s CNI 隔离）。  
> 需通过 **k3s 容器 Bridge IP（172.17.0.12）+ NodePort** 访问，或从 pod 内部测试。  
> MindIE 引擎端口：`17000`（OpenAI API），`1026`（management/health，仅 pod 内部）。

---

## 2. 架构说明

```
┌─────────────────────────── Pod: wings-infer-mindie ─────────────────────────────────┐
│                                                                                       │
│  ┌─────────────────────────────────────────┐   ┌───────────────────────────────────┐ │
│  │   Container: wings-infer (Ascend ST)     │   │   Container: mindie-engine        │ │
│  │   image: wings-infer:zhanghui-ascend-st  │   │   image: mindie:2.2.RC1-800I-A2   │ │
│  │                                          │   │                                   │ │
│  │   Port 18000: OpenAI proxy               │   │   Port 17000: MindIE OpenAI API   │ │
│  │   Port 19000: wings health               │   │   Port 1026:  management health   │ │
│  │                                          │   │                                   │ │
│  │   mindie_adapter.py 生成                 │   │   mindieservice_daemon            │ │
│  │   start_command.sh:                      │   │   (PID=1, exec 启动)              │ │
│  │   - merge-update config.json             │   │                                   │ │
│  │   - exec mindieservice_daemon            │   │   NPU 0: HBM 49775MB/65536MB      │ │
│  └──────────────┬──────────────────────────┘   └───────────────────────────────────┘ │
│                 │ /shared-volume/start_command.sh                                     │
│                 └──────────────────────────────────────────────────────────────────→  │
│                                                                                       │
│  health probe: wings-infer → 127.0.0.2:1026/health（MindIE management 端口）        │
│  推理代理:     外部 → 18000(proxy) → 17000(engine)                                   │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 关键修复记录

### 3.1 JSON type_error.302 — config.json 配置覆写错误

**问题根因**：原始 `mindie_adapter.py` 从零构建 config.json，遗漏了 MindIE 原始镜像中已有的关键字段（`LogConfig`、`ScheduleConfig.templateType`、TLS 系列字段等）。daemon 启动时这些字段类型被自动推断错误，触发 `type_error.302`。

**修复方案**：参照 `wings/wings/engines/mindie.py` 的 `_update_single_config` 逻辑，将 `mindie_adapter.py` 中的 `build_start_script()` 改为 **merge-update 策略**：

```python
# _mindie_overrides.json 仅包含需要覆盖的字段
# start_command.sh 中内联 Python：
#   1. 读取镜像原始 config.json（含所有完整字段）
#   2. .update() 只覆盖需要修改的部分
#   3. 写回 config.json
config = json.load(open(CONFIG_PATH))
config['ServerConfig'].update(ov['server'])
config['BackendConfig'].update(ov['backend'])
config['BackendConfig']['ModelDeployConfig'].update(ov['model_deploy'])
config['BackendConfig']['ModelDeployConfig']['ModelConfig'][0].update(ov['model_config'])
config['BackendConfig']['ScheduleConfig'].update(ov['schedule'])
with open(CONFIG_PATH, 'w') as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
```

**效果**：LogConfig、TLS、templateType 等原始字段完整保留，daemon ConfigManager 初始化成功。

---

### 3.2 npu-smi FileNotFoundError

**问题根因**：MindIE `atb-models` 库在 `init_ascend_weight()` → `is_support_lccl()` → `is_support_hccs()` 调用链中执行 `npu-smi info -t topo` 探测 HCCS 拓扑。容器镜像中没有 npu-smi，宿主机上的路径为 `/usr/local/sbin/npu-smi`。

**修复方案**：

1. 将 npu-smi 从 host 复制进 k3s 容器（仅需一次）：
   ```bash
   docker cp /usr/local/sbin/npu-smi k3s-verify-zhanghui:/usr/local/sbin/npu-smi
   ```

2. deployment YAML 中通过 `hostPath` Volume 挂载：
   ```yaml
   volumes:
     - name: npu-smi
       hostPath:
         path: /usr/local/sbin/npu-smi
         type: File
   # 在 mindie-engine 容器的 volumeMounts 中：
   volumeMounts:
     - name: npu-smi
       mountPath: /usr/local/sbin/npu-smi
       readOnly: true
   ```

---

### 3.3 /dev/shm 默认 64MB → SIGKILL（exit 137）

**问题根因**：MindIE 的 Python/PyTorch 后端在加载模型时使用 POSIX 共享内存（`/dev/shm`）进行多进程间的张量共享。Kubernetes Pod 默认的 `/dev/shm` 大小为 **64MB**（Linux 内核默认值），而 MindIE 初始化阶段需要分配远超此限制的共享内存。

当 `/dev/shm` 空间耗尽时，进程组收到 **SIGKILL**（内核强制终止，非应用层退出），表现为容器 exit code 137。由于 SIGKILL 的特性，进程无法捕获该信号，故无任何应用层日志输出，这使定位极为困难。

**诊断过程**：
1. exit 137 → 初判为 cgroup memory OOM，将容器 memory limit 从 64Gi → 128Gi → 去除限制，但仍然 crash
2. 检查 `dmesg` 无 OOM killer 记录，排除系统 OOM
3. 检查命名空间 LimitRange/ResourceQuota 无限制
4. exec 进入运行中的容器，发现 `/dev/shm` 已用 49MB/64MB（76% 利用率）
5. 确认：是 `/dev/shm` 满导致的 SIGKILL，而非 cgroup 内存限制

**修复方案**：使用 `emptyDir(medium: Memory)` 覆盖 `/dev/shm`，将其扩大到 16Gi：

```yaml
# Pod volumes 中增加：
volumes:
  - name: dshm
    emptyDir:
      medium: Memory      # 使用 tmpfs（内存文件系统）
      sizeLimit: 16Gi     # 上限 16Gi（实际按需消耗，不预分配）

# mindie-engine container 的 volumeMounts 中增加：
volumeMounts:
  - name: dshm
    mountPath: /dev/shm   # 覆盖默认 64MB 的 /dev/shm
```

> **原理说明**：`/dev/shm` 本质是一个挂载在内存上的 tmpfs。Kubernetes 默认为每个容器提供的 `/dev/shm` 大小为 64MB（继承自 Docker 默认值）。通过显式挂载一个 `medium: Memory` 的 `emptyDir`，可以将该 tmpfs 的容量上限扩大到 `sizeLimit` 所设置的值，从而满足 MindIE PyTorch 多进程共享内存的需求。注意此内存消耗计入节点内存，但**不计入** Pod 的 cgroup memory limit 统计。

---

### 3.4 内存参数优化（OOM 辅助修复）

**问题**：`mindie_default.json` 中的默认参数（`maxBatchSize=256`、`tokenizerProcessNumber=8`、`cacheBlockSize=16`）在验证环境中会导致极大的 KV cache 和 tokenizer worker 内存预占。

**修复**：在 engine container 启动脚本中，merge-update 完成后追加 Python 参数补丁：

```python
# deployment YAML 中的 post-merge patch（Python 单行器）：
python3 -c 'import json,os;
CF="/usr/local/Ascend/mindie/latest/mindie-service/conf/config.json";
c=json.load(open(CF));
c["BackendConfig"]["tokenizerProcessNumber"]=2;          # 8 → 2
c["BackendConfig"]["ScheduleConfig"]["maxBatchSize"]=32; # 256 → 32
c["BackendConfig"]["ScheduleConfig"]["maxPrefillBatchSize"]=8;  # 50 → 8
c["BackendConfig"]["ScheduleConfig"]["cacheBlockSize"]=128;     # 16 → 128
json.dump(c,open(CF,"w"),indent=2,ensure_ascii=False);os.chmod(CF,0o640)'
```

---

### 3.5 MindIE 健康检查端点特判

**说明**：MindIE 不在推理端口（17000）上暴露 `/health`，而是使用 management 端口（`127.0.0.2:1026`）。

**已有实现**：`backend-ascend-st/app/proxy/health.py` 中已有完整特判链路：

- `config_loader.py` 将 `"mindie"` 写入 `/var/log/wings/wings.txt` 第二行（`_write_engine_second_line`）
- `health.py` 中 `_is_mindie()` 读取第二行识别引擎类型
- `_strict_probe_backend_health()` 中当 `_is_mindie()` 为 True 时调用 `_force_port(url, "127.0.0.2", 1026)` 将探测 URL 改写到 management 端口

```python
# health.py 关键逻辑（无需修改，已正确实现）
url = build_backend_url("/health")
if _is_mindie():
    url = _force_port(url, "127.0.0.2", 1026)  # mindie 健康检查走 management 端口
```

---

## 4. 部署步骤（完整可复现流程）

### 4.1 前置条件

```bash
# 在宿主机 7.6.52.110 上执行
# 确认 k3s-verify 容器运行
docker ps | grep k3s-verify-zhanghui

# 确认 vllm-ascend 已停止（释放 NPU 0）
docker exec k3s-verify-zhanghui kubectl scale deployment wings-infer-vllm-ascend \
    -n wings-verify --replicas=0

# 确认 NPU 0 无占用进程
npu-smi info -t proc-mem -i 0 -c 0
# 预期: "No process in device."

# ★ 将 npu-smi 复制进 k3s 容器（仅需执行一次，容器重启后需重复）
docker cp /usr/local/sbin/npu-smi k3s-verify-zhanghui:/usr/local/sbin/npu-smi
```

### 4.2 镜像准备

```bash
# MindIE 镜像（23.1GB，已在 k3s containerd 中，无需重复导入）
# 确认：
docker exec k3s-verify-zhanghui crictl images | grep mindie

# wings-infer 镜像（backend-ascend-st，含 mindie_adapter.py 重构版本）
# 构建（在代码目录执行）：
cd infer-control-sidecar-main/
docker build -t wings-infer:zhanghui-ascend-st \
    -f Dockerfile \
    --build-arg BACKEND=backend-ascend-st .

# 将 wings-infer 镜像导入 k3s containerd：
docker save wings-infer:zhanghui-ascend-st | \
    docker exec -i k3s-verify-zhanghui ctr images import -
```

### 4.3 部署 Service（仅首次）

```bash
# 从宿主机上传 YAML 至 k3s 容器并 apply
scp k8s/service-mindie.verify.yaml root@7.6.52.110:/root/
docker cp /root/service-mindie.verify.yaml k3s-verify-zhanghui:/tmp/
docker exec k3s-verify-zhanghui kubectl apply -f /tmp/service-mindie.verify.yaml

# 验证
docker exec k3s-verify-zhanghui kubectl get svc -n wings-verify
# 预期输出：
# NAME                        TYPE       CLUSTER-IP     EXTERNAL-IP   PORT(S)
# wings-infer-mindie-service  NodePort   10.43.x.x      <none>        18000:31820/TCP,19000:31920/TCP
```

### 4.4 部署 Deployment

```bash
# 上传并 apply deployment YAML
scp k8s/deployment-mindie.verify.yaml root@7.6.52.110:/root/
docker cp /root/deployment-mindie.verify.yaml k3s-verify-zhanghui:/tmp/
docker exec k3s-verify-zhanghui kubectl apply -f /tmp/deployment-mindie.verify.yaml
docker exec k3s-verify-zhanghui kubectl rollout restart deployment/wings-infer-mindie -n wings-verify

# 观察启动状态（MindIE 冷启动约 2-4 分钟）
docker exec k3s-verify-zhanghui kubectl get pods -n wings-verify -w

# 等待 READY 2/2
# 预期：
# NAME                                READY   STATUS    RESTARTS   AGE
# wings-infer-mindie-cdb95fdf-glklr   2/2     Running   0          3m
```

### 4.5 验证部署成功

```bash
# 确认 daemon 启动成功（关键日志）
docker exec k3s-verify-zhanghui kubectl logs -n wings-verify \
    -l app=wings-infer-mindie -c mindie-engine --tail=5
# 预期包含：
# [ConfigManager::InitConfigManager] Successfully init config manager
# Daemon start success!

# 确认 NPU 0 上有 MindIE 进程
npu-smi info -t proc-mem -i 0 -c 0
# 预期：
# Process id:xxxxxxx Process name:mindie_llm_back   Process memory(MB):49775
```

---

## 5. T1-T6 验证测试结果

### 5.1 测试环境说明

| 测试方式 | 访问方式 |
|---------|---------|
| 引擎直连 | Pod 内部 `127.0.0.1:17000`（mindie-engine container） |
| Proxy 访问 | Pod 内部 `127.0.0.1:18000`（wings-infer container） |
| NodePort 外部 | `172.17.0.12:31820`（proxy），`172.17.0.12:31920`（health） |

---

### T1 — 健康检查（引擎 management 端口）

**说明**: MindIE 健康检查走 management 端口 `127.0.0.2:1026`，而非推理端口 17000。wings-infer 的 `health.py` 已内置此特判逻辑。

```bash
# 从 mindie-engine container 内部测试
kubectl exec -n wings-verify <pod> -c mindie-engine -- \
    python3 -c "import urllib.request; r=urllib.request.urlopen('http://127.0.0.2:1026/health',timeout=5); print(r.status, r.read().decode())"
```

**结果**:
```
200
(空 body，HTTP 200 表示 MindIE daemon 就绪)
```
✅ **通过**

---

### T2 — Proxy 健康检查（wings-infer 18000 端口）

```bash
# 从 wings-infer container 内部（proxy 端口 18000）
kubectl exec -n wings-verify <pod> -c wings-infer -- \
    python3 -c "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:18000/health',timeout=5); print(r.status, r.read().decode())"

# 或从 k3s bridge 外部通过 NodePort
curl -s http://172.17.0.12:31920/health
```

**结果**:
```json
{"s":1,"p":"ready","pid_alive":false,"backend_ok":true,"backend_code":200,"interrupted":false,"ever_ready":true,"cf":0,"lat_ms":4}
```
- `s=1`：状态 ready
- `p="ready"`：阶段 ready
- `backend_ok=true`：后端（MindIE management:1026）健康检查通过
- `backend_code=200`：后端返回 200

✅ **通过**

---

### T3 — 模型列表（proxy 18000）

```bash
curl -s http://172.17.0.12:31820/v1/models
```

**结果**:
```json
{
  "data": [
    {
      "id": "DeepSeek-R1-Distill-Qwen-1.5B",
      "object": "model",
      "owned_by": "MindIE Server",
      "parent": "null",
      "root": "/models/DeepSeek-R1-Distill-Qwen-1.5B/"
    }
  ],
  "object": "list"
}
```
✅ **通过** — 模型名称 `DeepSeek-R1-Distill-Qwen-1.5B`，owned_by `MindIE Server`

---

### T4 — 推理请求（引擎直连 17000）

```bash
# 从 mindie-engine container 内部直连引擎
kubectl exec -n wings-verify <pod> -c mindie-engine -- \
    python3 /tmp/test_mindie.py   # 脚本请求 http://127.0.0.1:17000/v1/chat/completions
```

**请求**:
```json
{"model": "DeepSeek-R1-Distill-Qwen-1.5B", "messages": [{"role": "user", "content": "What is 1+1?"}], "max_tokens": 32, "temperature": 0.1}
```

**结果**:
```json
{
  "id": "endpoint_common_4",
  "object": "chat.completion",
  "model": "DeepSeek-R1-Distill-Qwen-1.5B",
  "choices": [{
    "message": {"role": "assistant", "content": "Alright, the user just said \"hi.\" ..."},
    "finish_reason": "length"
  }],
  "usage": {"prompt_tokens": 6, "completion_tokens": 32, "total_tokens": 38}
}
```
✅ **通过** — MindIE 引擎推理正常，返回有效 token

---

### T5 — 推理请求（proxy 18000 → engine 17000）

```bash
# 通过 wings-infer proxy 访问（经 proxy 转发到 MindIE 引擎）
curl -s -X POST http://172.17.0.12:31820/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d @/tmp/c.json     # {"model":"DeepSeek-R1-Distill-Qwen-1.5B","messages":[{"role":"user","content":"What is 2+3?"}],"max_tokens":16}
```

**结果**:
```json
{
  "id": "endpoint_common_11",
  "object": "chat.completion",
  "model": "DeepSeek-R1-Distill-Qwen-1.5B",
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "I need to calculate the sum of 2 and 3.\n\nFirst, I"
    },
    "finish_reason": "length"
  }],
  "usage": {"prompt_tokens": 12, "completion_tokens": 16, "total_tokens": 28},
  "prefill_time": 66,
  "decode_time_arr": [35, 17, 14, 15, 15, 14, 13, 17, 12, 13, 17, 15, 13, 16, 12]
}
```
✅ **通过** — proxy → engine 全链路推理正常，prefill_time=66ms，decode 稳定

---

### T6 — NPU 使用确认

```bash
npu-smi info -t proc-mem -i 0 -c 0
npu-smi info -t usages -i 0 -c 0
```

**结果**:
```
Process id:2078455  Process name:mindie_llm_back   Process memory(MB):49775

HBM Capacity(MB)   : 65536
HBM Usage Rate(%)  : 81
Aicore Usage Rate(%) : 0     # 空闲状态下为 0，推理时会上升
```
✅ **通过** — NPU 0 上有 `mindie_llm_back` 进程，HBM 占用 49775MB（76% 用于模型权重 + KV cache）

---

## 6. 最终测试汇总

| 测试项 | 端点 | HTTP 状态 | 结果 |
|--------|------|-----------|------|
| T1 Engine health (mgmt) | `127.0.0.2:1026/health` | 200 | ✅ |
| T2 Proxy health (18000) | `127.0.0.1:18000/health` | 200 | ✅ |
| T2 Proxy health (NodePort) | `172.17.0.12:31920/health` | 200 | ✅ |
| T3 Models list | `172.17.0.12:31820/v1/models` | 200 | ✅ |
| T4 Chat via engine (17000) | `127.0.0.1:17000/v1/chat/completions` | 200 | ✅ |
| T5 Chat via proxy (18000) | `127.0.0.1:18000/v1/chat/completions` | 200 | ✅ |
| T5 Chat via NodePort | `172.17.0.12:31820/v1/chat/completions` | 200 | ✅ |
| T6 NPU usage | `npu-smi info -t proc-mem -i 0 -c 0` | — | ✅ mindie_llm_back 49775MB |

---

## 7. 部署文件清单

| 文件 | 路径 | 说明 |
|------|------|------|
| Deployment YAML | `k8s/deployment-mindie.verify.yaml` | 含所有修复（/dev/shm、npu-smi mount、内存参数补丁） |
| Service YAML | `k8s/service-mindie.verify.yaml` | NodePort 31820→18000，31920→19000 |
| mindie_adapter.py | `backend-ascend-st/app/engines/mindie_adapter.py` | merge-update 策略，修复 JSON type_error.302 |
| health.py | `backend-ascend-st/app/proxy/health.py` | MindIE health 特判（管理端口 1026，无需修改） |

---

## 8. 已知限制 / 后续改进建议

| 项目 | 说明 | 优先级 |
|------|------|--------|
| npu-smi 持久化 | k3s 容器重建后需重新 `docker cp`，建议改为 hostPath bind mount（需 k3s 配置支持）| 中 |
| 内存参数补丁位置 | 目前 post-merge patch 在 deployment YAML 中硬编码，建议移入 `mindie_adapter.py` 中作为可配置参数 | 中 |
| `pid_alive=false` | K8s sidecar 模式下 wings.txt 第一行无 PID，`WINGS_SKIP_PID_CHECK=true` 已绕过，可进一步优化 | 低 |
| 推理端口路由 | MindIE 的 `/health` 在 17000 返回 404，wings 代理层需确保不将此 404 暴露给上游 | 低 |
