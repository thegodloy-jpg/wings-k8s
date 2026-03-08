# 故障排查指南

## 快速诊断流程

```
1. kubectl get pods -n wings-infer -w          # Pod 状态
2. curl http://<NODE_IP>:19000/health          # 健康检查
3. kubectl logs <pod> -c wings-infer -n wings-infer   # Sidecar 日志
4. kubectl logs <pod> -c engine -n wings-infer         # 引擎日志
```

---

## 常见问题

### 1. Pod 持续 CrashLoopBackOff

**症状**: Pod 反复重启

**排查**:
```bash
kubectl describe pod <pod> -n wings-infer
kubectl logs <pod> -c wings-infer --previous -n wings-infer
```

**常见原因**:

| 原因 | 日志特征 | 解决 |
|------|----------|------|
| 镜像拉取失败 | `ImagePullBackOff` | 检查镜像名和仓库可达性 |
| 模型路径错误 | `model_path not found` | 检查 `MODEL_PATH` 和 hostPath 挂载 |
| NPU 驱动缺失 | `cannot find npu-smi` | 检查 Ascend 驱动挂载 (`/usr/local/Ascend/driver`) |
| GPU 不可见 | `CUDA_VISIBLE_DEVICES` empty | 检查 `nvidia.com/gpu` 资源配额 |

### 2. 健康检查返回 201 (启动中)

**症状**: `curl :19000/health` 长时间返回 201

**说明**: 201 是 **正常的启动过渡状态**，表示引擎正在加载模型。

**排查**:
```bash
# 查看详细健康信息
curl -s http://<NODE_IP>:19000/health | python -m json.tool

# 预期响应 (启动中):
# {"s": 0, "p": "starting", "backend_ok": false, "ever_ready": false}

# 预期响应 (就绪):
# {"s": 1, "p": "ready", "backend_ok": true, "ever_ready": true}
```

**常见原因**:
- 大模型加载耗时长（正常，等待即可）
- 引擎启动脚本未执行（检查 engine 容器的 entrypoint 是否等待 `start_command.sh`）
- `start_command.sh` 未写入共享卷

```bash
# 检查共享卷
kubectl exec <pod> -c engine -n wings-infer -- ls -la /shared-volume/
kubectl exec <pod> -c engine -n wings-infer -- cat /shared-volume/start_command.sh
```

### 3. 健康检查返回 502 (启动失败)

**症状**: 超过宽限期后返回 502

**原因**: 在 `STARTUP_GRACE_MS`（默认 60 分钟）内引擎未成功响应 `/health`

**排查**:
```bash
# 检查引擎是否启动
kubectl exec <pod> -c engine -n wings-infer -- ps aux | grep -E 'vllm|sglang|mindie'

# 检查引擎端口是否监听
kubectl exec <pod> -c wings-infer -n wings-infer -- curl -s http://127.0.0.1:17000/health

# 查看引擎日志
kubectl logs <pod> -c engine -n wings-infer --tail=100
```

**常见原因**:
| 原因 | 解决 |
|------|------|
| OOM | 降低 `GPU_MEMORY_UTILIZATION` 或 `MAX_MODEL_LEN` |
| Triton 初始化失败 (Ascend) | 检查 Triton 补丁日志: `grep triton` |
| Ray 集群未连通 (分布式) | 检查 headless service DNS 和节点间网络 |

### 4. 健康检查返回 503 (降级)

**症状**: 曾经就绪 (200)，后变为 503

**原因**: 引擎进程崩溃或连续探测失败超过阈值

**排查**:
```bash
# PID 检查
kubectl exec <pod> -c wings-infer -n wings-infer -- cat /var/log/wings/wings.txt

# 引擎进程是否存活
kubectl exec <pod> -c engine -n wings-infer -- ps aux
```

### 5. 代理返回 502/504

**症状**: 通过 :18000 请求推理返回 502 或 504

**原因**: Proxy 无法连通后端引擎

**排查**:
```bash
# 直接访问引擎
kubectl exec <pod> -c wings-infer -n wings-infer -- \
  curl -s http://127.0.0.1:17000/v1/models

# 检查 BACKEND_URL
kubectl exec <pod> -c wings-infer -n wings-infer -- env | grep BACKEND_URL
```

**常见错误**:
- `BACKEND_URL` 指向了错误的地址 → 检查 `NODE_IPS` 和 `NODE_RANK`
- 引擎监听在非 0.0.0.0 地址 → 检查引擎启动参数中的 `--host`

### 6. 分布式: Ray Worker 连接失败

**症状**: rank>0 节点无法加入 Ray 集群

**排查**:
```bash
# 从 worker 节点检查 head 可达性
kubectl exec <pod>-1 -c engine -n wings-infer -- \
  python -c "import socket; print(socket.getaddrinfo('<sts>-0.<svc>', 6379))"

# Ray 状态
kubectl exec <pod>-0 -c engine -n wings-infer -- ray status
```

**常见原因**:
| 原因 | 解决 |
|------|------|
| Headless Service 未创建 | 检查 `clusterIP: None` 的 Service |
| DNS 未解析 | 检查 CoreDNS Pod 和 resolv.conf |
| 防火墙 | 开放 6379 (Ray GCS) 和 8265 (Ray Dashboard) |
| Ray 版本不一致 | 确保所有节点使用相同镜像 |

### 7. 分布式: Ascend HCCL 通信失败

**症状**: MindIE 分布式模式下节点间通信错误

**排查**:
```bash
# 检查 HCCL 端口
kubectl exec <pod> -c engine -n wings-infer -- \
  ss -tlnp | grep 27070

# 检查 ranktable.json
kubectl exec <pod> -c engine -n wings-infer -- \
  cat /shared-volume/ranktable.json
```

**常见原因**:
- 缺少 `privileged: true`
- 驱动路径未挂载（`/usr/local/Ascend/driver`、`/usr/local/dcmi`）
- HCCL 端口 27070 被防火墙阻断

### 8. Triton NPU 补丁相关

**症状**: vLLM-Ascend 启动时报 Triton 错误

**日志特征**:
```
ImportError: cannot import name 'driver' from 'triton.runtime'
```

**说明**: `vllm_adapter.py` 会自动尝试补丁，在日志中搜索:
```bash
kubectl logs <pod> -c engine -n wings-infer | grep -i triton
```

**如果自动补丁失败**:
```bash
# 手动验证 Triton 路径
kubectl exec <pod> -c engine -n wings-infer -- \
  python -c "import triton; print(triton.__file__)"
```

### 9. CANN 库冲突 (Ascend)

**症状**: `libascendcl.so` 或 `libhccl.so` 加载报错

**排查**:
```bash
# 检查 LD_LIBRARY_PATH 是否包含宿主机驱动和容器内 CANN
kubectl exec <pod> -c engine -n wings-infer -- \
  bash -c 'echo $LD_LIBRARY_PATH | tr ":" "\n" | grep -i ascend'

# 检查是否有重复库
kubectl exec <pod> -c engine -n wings-infer -- \
  find / -name "libascendcl.so*" 2>/dev/null
```

**解决**: 确保宿主机驱动版本与容器内 CANN 版本兼容，LD_LIBRARY_PATH 中驱动路径在 CANN 路径之前。

---

## 环境变量调优

### 健康检查参数

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `STARTUP_GRACE_MS` | 3600000 (60min) | 启动宽限期，大模型可适当增加 |
| `POLL_INTERVAL_MS` | 5000 | 探测间隔 (ms) |
| `FAIL_THRESHOLD` | 5 | 连续失败次数阈值 |
| `HEALTH_TIMEOUT_MS` | 5000 | 单次探测超时 (ms) |
| `WINGS_SKIP_PID_CHECK` | false | K8s sidecar 模式设为 true |

### 代理参数

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MAX_CONN` | (见 settings.py) | httpx 最大连接数 |
| `RETRY_TRIES` | (见 settings.py) | 重试次数 |
| `RETRY_INTERVAL_MS` | (见 settings.py) | 重试间隔 (ms) |

### SGLang 专用

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SGLANG_FAIL_BUDGET` | 6.0 | 失败预算 (权重) |
| `SGLANG_PID_GRACE_MS` | 30000 | PID 宽限期 |
| `SGLANG_SILENCE_MAX_MS` | 60000 | 静默最大时间 → 503 |

---

## 日志位置

| 组件 | 日志来源 | 前缀 |
|------|----------|------|
| Launcher | Sidecar 容器 stdout | `[launcher]` |
| Proxy | Sidecar 容器 stdout | uvicorn 日志 |
| Health | Sidecar 容器 stdout | `[health]` |
| Engine | Engine 容器 stdout | 引擎原生日志 |
| PID 文件 | `/var/log/wings/wings.txt` | 第一行为 PID |

```bash
# 一次性查看所有容器日志
kubectl logs <pod> -n wings-infer --all-containers=true --tail=50
```
