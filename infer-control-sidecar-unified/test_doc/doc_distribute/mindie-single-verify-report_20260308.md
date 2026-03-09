# MindIE 单机推理验证报告

**验证日期**: 2026-03-08 ~ 2026-03-09  
**验证人**: zhanghui  
**文档路径**: `test_doc/doc_distribute/mindie-single-verify-report_20260308.md`

---

## 1. 验证概述

| 项目 | 内容 |
|------|------|
| 推理框架 | MindIE 2.2.RC1 (华为昇腾) |
| 模型 | DeepSeek-R1-Distill-Qwen-1.5B |
| 部署方式 | k3s Deployment (Pod 网络模式) |
| 节点数 | 1 (单机 TP=1) |
| NPU | 910B2C × 1 (davinci0, 65536 MB HBM) |
| 部署节点 | .170 (hostname: root, 7.6.52.170) |
| 验证结果 | **全部通过 ✅** |

---

## 2. 集群环境

### 节点信息

| 角色 | 主机 | IP | 节点名 | NPU |
|------|------|----|--------|-----|
| k3s Server | .110 | 7.6.52.110 | 910b-47 | 16× 910B2C (本次未使用) |
| k3s Agent (部署节点) | .170 | 7.6.52.170 | root | 16× 910B2C，使用 davinci0 |

### 镜像

| 镜像 | 版本 | 大小 | 说明 |
|------|------|------|------|
| `mindie:2.2.RC1` | 2.2.RC1 | ~22 GiB | MindIE 推理引擎 (openEuler24.03, py311) |
| `wings-infer:zhanghui-ascend-st-unified` | 2026-03-09 build | ~448 MB | wings-infer 代理 sidecar |

### k3s 配置 (两节点)

```yaml
# /etc/rancher/k3s/config.yaml
kubelet-arg:
  - image-gc-high-threshold=100
  - image-gc-low-threshold=99
  - eviction-hard=nodefs.available<2%,imagefs.available<2%,memory.available<100Mi
  - eviction-soft=nodefs.available<3%,imagefs.available<3%,memory.available<200Mi
  - eviction-soft-grace-period=nodefs.available=2m,imagefs.available=2m,memory.available=2m
```

---

## 3. 部署配置

### Deployment 关键参数

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: infer-mindie
  namespace: wings-infer
spec:
  replicas: 1
  template:
    spec:
      nodeSelector:
        kubernetes.io/hostname: "root"  # 固定到 .170
```

### wings-infer sidecar 关键环境变量

```yaml
WINGS_DEVICE: "ascend"
DEVICE: "ascend"
DEVICE_COUNT: "1"
ENGINE: "mindie"
MODEL_NAME: "DeepSeek-R1-Distill-Qwen-1.5B"
MODEL_PATH: "/models/DeepSeek-R1-Distill-Qwen-1.5B"
PORT: "18000"          # proxy 端口
HEALTH_PORT: "19000"   # 健康检查端口
BACKEND_URL: "http://127.0.0.1:17000"  # MindIE 引擎端口
WINGS_SKIP_PID_CHECK: "true"
```

### engine 容器关键配置

```yaml
image: mindie:2.2.RC1
securityContext:
  privileged: true
env:
  - name: ASCEND_VISIBLE_DEVICES
    value: "0"
  - name: PATH
    value: "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
volumeMounts:
  - /usr/local/Ascend/driver  (hostPath, NPU 驱动)
  - /usr/local/sbin/npu-smi   (hostPath, NPU 管理工具)
  - /dev/davinci0, /dev/davinci_manager, /dev/hisi_hdc  (NPU 设备)
  - /dev/shm (emptyDir, 2Gi)
  - /shared-volume (start_command.sh 共享)
  - /models (CephFS hostPath, 只读)
```

### MindIE config.json 关键覆盖

```json
{
  "ServerConfig": {
    "ipAddress": "0.0.0.0",
    "port": 17000,
    "httpsEnabled": false,
    "openAiSupport": "vllm"
  },
  "BackendConfig": {
    "npuDeviceIds": [[0]],
    "multiNodesInferEnabled": false,
    "ModelDeployConfig": {
      "maxSeqLen": 5120,
      "maxInputTokenLen": 4096,
      "ModelConfig": [{
        "modelName": "DeepSeek-R1-Distill-Qwen-1.5B",
        "modelWeightPath": "/models/DeepSeek-R1-Distill-Qwen-1.5B",
        "worldSize": 1,
        "backendType": "atb",
        "trustRemoteCode": true
      }]
    }
  }
}
```

---

## 4. 问题排查与修复

### 4.1 MindIE daemon 启动后立即退出 (exit code 255)

**根因**: `mindie_adapter.py` 生成的 `start_command.sh` 缺少 3 项关键环境设置：

| 缺失项 | 说明 | 修复 |
|--------|------|------|
| `atb-models/set_env.sh` | ATB 模型运行时环境脚本 | 添加 `source /usr/local/Ascend/atb-models/set_env.sh` |
| `nnal/atb/set_env.sh` | NNAL ATB 加速库环境脚本 | 添加 `source /usr/local/Ascend/nnal/atb/set_env.sh` |
| `driver/lib64/{driver,common}` | NPU 驱动共享库路径 | 添加到 `LD_LIBRARY_PATH` |

同时还修复了 daemon 启动方式：
- **旧**: `exec ./bin/mindieservice_daemon` — daemon fork 后父进程退出，容器随之终止
- **新**: `./bin/mindieservice_daemon &` + `wait $pid` — 参考官方 `boot.sh`，前台等待守护进程

**修改文件**: `backend/app/engines/mindie_adapter.py` `_build_env_commands()` 和 `build_start_script()`

### 4.2 npu-smi 未找到导致模型加载失败

**错误**:
```
FileNotFoundError: [Errno 2] No such file or directory: 'npu-smi'
```

**根因**: MindIE 模型加载时调用 `npu-smi info -t topo` 检测 LCCL/HCCL 拓扑。  
`npu-smi` 位于宿主机 `/usr/local/sbin/npu-smi`，不在 `/usr/local/Ascend/driver/` 目录下。

**修复**:
1. 在 Deployment YAML 中添加 `npu-smi` hostPath 挂载
2. 设置 `PATH` 环境变量包含 `/usr/local/sbin`

```yaml
volumes:
  - name: npu-smi
    hostPath:
      path: /usr/local/sbin/npu-smi
      type: File
# ...
volumeMounts:
  - name: npu-smi
    mountPath: /usr/local/sbin/npu-smi
    readOnly: true
env:
  - name: PATH
    value: "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
```

### 4.3 kubelet 镜像 GC 和磁盘压力驱逐

| 问题 | 根因 | 修复 |
|------|------|------|
| 镜像被自动删除 | .110 磁盘 95%，kubelet 默认 85% 触发 GC | 设置 `image-gc-high-threshold=100` |
| Pod 被 Evict | .170 磁盘 87%，kubelet 默认 eviction 15% | 设置 `eviction-hard=nodefs.available<2%` |
| CoreDNS 镜像丢失 | GC 删除了 CoreDNS 镜像 | 从宿主机 Docker 重新导入 |

### 4.4 CephFS 阻塞 (.110)

.110 上运行 `cp -r DeepSeek-V3.1/` 进程 (PID 444830) 导致 CephFS I/O 完全阻塞。  
**解决**: 切换到 .170 部署 MindIE（.170 CephFS 正常，76.4 MB/s 读速）。

---

## 5. 验证结果

### 5.1 Pod 状态

```
NAME                            READY   STATUS    RESTARTS   AGE   IP           NODE
infer-mindie-7845896b89-tg79k   2/2     Running   0          60m   10.42.1.26   root
```

### 5.2 Engine 启动日志

```
[mindie] Loaded original config.json (3097 chars)
[mindie] config.json merge-updated successfully
[mindie] Daemon started as PID 418
g_mainPid = 418
Daemon start success!
ConfigManager: Load Config from /usr/local/Ascend/mindie/2.2.RC1/mindie-service/conf/config.json.
[ConfigManager::InitConfigManager] Successfully init config manager
```

### 5.3 模型接口验证

**GET /v1/models** ✅
```json
{
    "data": [
        {
            "id": "DeepSeek-R1-Distill-Qwen-1.5B",
            "object": "model",
            "owned_by": "MindIE Server",
            "root": "/models/DeepSeek-R1-Distill-Qwen-1.5B/"
        }
    ],
    "object": "list"
}
```

**POST /v1/completions (直连 MindIE port 17000)** ✅
```json
{
  "model": "DeepSeek-R1-Distill-Qwen-1.5B",
  "prompt": "Hello",
  "max_tokens": 16
}
→ 响应:
{
  "choices": [{"text": ", I have a question about an equation I've been trying to solve. The", "finish_reason": "length"}],
  "usage": {"prompt_tokens": 2, "completion_tokens": 16, "total_tokens": 18}
}
```

**POST /v1/chat/completions (经 wings-infer proxy port 18000)** ✅
```json
{
  "model": "DeepSeek-R1-Distill-Qwen-1.5B",
  "messages": [{"role": "user", "content": "1+1=?"}],
  "max_tokens": 16
}
→ 响应:
{
  "choices": [{"message": {"role": "assistant", "content": "I need to solve the equation 1 + 1.\n\nFirst, I recognize"}, "finish_reason": "length"}],
  "usage": {"prompt_tokens": 9, "completion_tokens": 16, "total_tokens": 25}
}
```

---

## 6. 代码变更清单

| 文件 | 变更 | 说明 |
|------|------|------|
| `backend/app/engines/mindie_adapter.py` | `_build_env_commands()` | 添加 atb-models、nnal/atb env 脚本加载；添加 driver lib64 路径；设置 GRPC_POLL_STRATEGY |
| `backend/app/engines/mindie_adapter.py` | `build_start_script()` 末尾 | daemon 启动从 `exec` 改为 `& wait` |
| `k8s/overlays/mindie-single/mindie-single-deploy.yaml` | 新增 | npu-smi hostPath 挂载、PATH 环境变量 |

---

## 7. 后续待验证

| 项目 | 状态 | 备注 |
|------|------|------|
| MindIE 分布式 (双节点 DP) | ⏳ 待测 | 需 .110 CephFS 恢复或找替代方案 |
| vllm-ascend 分布式推理 | ⏳ 待测 | Ray 集群已形成，CephFS 阻塞导致模型加载卡住 |
| vllm-ascend 单机推理 | ✅ 已验证 | 前次 session 完成 |

---

## 8. 附录：key 命令速查

```bash
# 查看 Pod 状态
kubectl get pods -n wings-infer -o wide

# 查看 engine 日志
kubectl logs -n wings-infer <pod-name> -c engine --tail=50

# 查看 sidecar 日志
kubectl logs -n wings-infer <pod-name> -c wings-infer --tail=50

# 直连 MindIE 测试
kubectl exec -n wings-infer <pod-name> -c wings-infer -- \
  curl -s http://127.0.0.1:17000/v1/models

# 经 proxy 测试推理
kubectl exec -n wings-infer <pod-name> -c wings-infer -- \
  curl -s -X POST http://127.0.0.1:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"DeepSeek-R1-Distill-Qwen-1.5B","messages":[{"role":"user","content":"hello"}],"max_tokens":16}'
```
