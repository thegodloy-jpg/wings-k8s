# infer-control-sidecar GPU 验证部署完整手册

**适用日期**：2026-02-28  
**目标机器**：`root@7.6.52.148`（hostname: `a100`）  
**GPU 环境**：NVIDIA A100-PCIE-40GB (`/dev/nvidia0`)，NVIDIA L20 (`/dev/nvidia1`)，驱动版本 `550.90.07`  
**验证模型**：`DeepSeek-R1-Distill-Qwen-1.5B`（路径 `/mnt/models/DeepSeek-R1-Distill-Qwen-1.5B`）  
**隔离原则**：全部操作在 `wings-verify` 命名空间和 `k3s-verify` 容器内进行，不影响现网。

---

## 背景与架构说明

### 为什么用 privileged + hostPath 而不用 runtimeClassName: nvidia

k3s-verify 容器基于 `rancher/k3s:v1.30.6-k3s1`（Alpine Linux / musl libc）。  
宿主机上的 `nvidia-container-runtime` 是 glibc 编译的二进制，无法在 Alpine 容器内执行（ELF 解释器 `/lib64/ld-linux-x86-64.so.2` 缺失）。  
验证命令：
```bash
docker exec k3s-verify /usr/local/bin/nvidia-container-runtime --version
# → exec /usr/local/bin/nvidia-container-runtime: no such file or directory
```

**替代方案（已验证可行）**：
1. 将宿主机 `libnvidia-ml.so` 和 `libcuda.so` 复制进 k3s-verify 的 `/mnt/nvidia-libs/`。
2. vllm-engine 容器用 `hostPath` 卷挂载该目录，并在启动时追加 `LD_LIBRARY_PATH`。
3. `securityContext.privileged: true` 打通 `/dev/nvidia*` 设备访问。

### 端口规划

| 端口 | 用途 |
|------|------|
| 17000 | vLLM 引擎（容器内） |
| 18000 | wings-proxy 代理（对外） |
| 19000 | wings-health 健康端口（探针） |

---

## 前置准备

### A. 代码 Bug 修复（一次性，已完成）

以下三个 bug 需在部署前确认已修复：

**Bug 1（CRITICAL）** — `backend/app/proxy/gateway.py` `/v1/models` 路由 500 错误：
```python
# 错误：传入 request.headers（Headers 对象），导致 AttributeError → 500
upstream_headers = make_upstream_headers(request.headers)

# 修复：传入完整 request 对象
upstream_headers = make_upstream_headers(request)
```

**Bug 2（MINOR）** — `backend/app/proxy/health.py` URL 空格 typo：
```python
# 错误（空格导致无效 URL）
url = f"http://127.0.0.1: {proxy_port}/v1/chat/completions"

# 修复
url = f"http://127.0.0.1:{proxy_port}/v1/chat/completions"
```

**Bug 3（MINOR）** — `backend/app/proxy/health_service.py` 重复 cancel：shutdown 时手动 cancel 后又调 `teardown_health_monitor`（内部再次 cancel），移除手动 cancel 块，改为：
```python
await teardown_health_monitor(app)
await app.state.client.aclose()
```

### B. 确认镜像已存在

```bash
# 确认 wings-infer / wings-accel 镜像在 Docker 中（用于 ctr import）
ssh root@7.6.52.148 "docker images | grep -E 'wings-infer|wings-accel|vllm'"
```

---

## Step 1：远程基础检查

```bash
ssh root@7.6.52.148 "hostname; whoami; docker --version"
ssh root@7.6.52.148 "docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'"
```

确认 k3s-verify 容器在运行：
```bash
ssh root@7.6.52.148 "docker inspect k3s-verify --format '{{.State.Status}}'"
# 期望输出: running
```

---

## Step 2：检查 k3s 验证集群

```bash
ssh root@7.6.52.148 "docker exec k3s-verify kubectl get nodes -o wide"
ssh root@7.6.52.148 "docker exec k3s-verify kubectl get pods -n kube-system"
```

检查 node 状态为 `Ready`，kube-system pod 均 `Running`。

---

## Step 3：准备 nvidia 驱动库（仅首次需要）

这是 GPU 访问的关键步骤。需要将宿主机的 nvidia 驱动共享库复制进 k3s-verify 容器。

```bash
# 3.1 确认宿主机驱动库存在
ssh root@7.6.52.148 "ls -lh /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.550.90.07 /usr/lib/x86_64-linux-gnu/libcuda.so.550.90.07"

# 3.2 在 k3s-verify 内创建目录
ssh root@7.6.52.148 "docker exec k3s-verify mkdir -p /mnt/nvidia-libs"

# 3.3 将驱动库复制进 k3s-verify（libnvidia-ml：约 2MB，libcuda：约 28MB）
ssh root@7.6.52.148 "docker cp /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.550.90.07 k3s-verify:/mnt/nvidia-libs/"
ssh root@7.6.52.148 "docker cp /usr/lib/x86_64-linux-gnu/libcuda.so.550.90.07 k3s-verify:/mnt/nvidia-libs/"

# 3.4 创建符号链接（vLLM 按 .so.1 / .so 查找）
ssh root@7.6.52.148 "docker exec k3s-verify sh -c 'cd /mnt/nvidia-libs && ln -sf libnvidia-ml.so.550.90.07 libnvidia-ml.so.1 && ln -sf libnvidia-ml.so.1 libnvidia-ml.so'"
ssh root@7.6.52.148 "docker exec k3s-verify sh -c 'cd /mnt/nvidia-libs && ln -sf libcuda.so.550.90.07 libcuda.so.1 && ln -sf libcuda.so.1 libcuda.so'"

# 3.5 验证
ssh root@7.6.52.148 "docker exec k3s-verify ls -la /mnt/nvidia-libs/"
```

期望输出（6 个文件/链接）：
```
lrwxrwxrwx    libcuda.so -> libcuda.so.1
lrwxrwxrwx    libcuda.so.1 -> libcuda.so.550.90.07
-rwxr-xr-x    libcuda.so.550.90.07     (28MB)
lrwxrwxrwx    libnvidia-ml.so -> libnvidia-ml.so.1
lrwxrwxrwx    libnvidia-ml.so.1 -> libnvidia-ml.so.550.90.07
-rwxr-xr-x    libnvidia-ml.so.550.90.07  (2MB)
```

---

## Step 4：同步 k8s 配置文件到远程

```bash
# 本地（PowerShell）执行
scp F:\zhanghui\wings-k8s\infer-control-sidecar-main\infer-control-sidecar-main\k8s\deployment.verify.yaml `
    root@7.6.52.148:/home/zhanghui/infer-control-sidecar-verify/project-src/k8s/deployment.verify.yaml

scp F:\zhanghui\wings-k8s\infer-control-sidecar-main\infer-control-sidecar-main\k8s\service.verify.yaml `
    root@7.6.52.148:/home/zhanghui/infer-control-sidecar-verify/project-src/k8s/service.verify.yaml
```

---

## Step 5：为 containerd 打 verify-latest tag

k3s 使用自己的 containerd（非 Docker），需将镜像 import 进去。  
如果镜像已导入（带时间戳 tag），可直接打别名 tag，无需重新导入（节省大量时间）：

```bash
# 查看 containerd 内已有的 wings 相关镜像
ssh root@7.6.52.148 "docker exec k3s-verify ctr -n k8s.io images list | grep -E 'wings|vllm'"
```

如果 `verify-latest` tag 不存在，执行：
```bash
# 5.1 打 wings-accel verify-latest（替换 <TS> 为实际时间戳，如 20260228-112530）
ssh root@7.6.52.148 "docker exec k3s-verify ctr -n k8s.io images tag docker.io/library/wings-accel:verify-<TS> docker.io/library/wings-accel:verify-latest"

# 5.2 打 wings-infer verify-latest（替换 <TS> 为实际时间戳，如 20260228-120915）
ssh root@7.6.52.148 "docker exec k3s-verify ctr -n k8s.io images tag docker.io/library/wings-infer:verify-<TS> docker.io/library/wings-infer:verify-latest"
```

如果 containerd 内完全没有镜像，需从 Docker 导入：
```bash
# 从宿主机 Docker 导出并导入到 k3s containerd
ssh root@7.6.52.148 "docker save wings-infer:verify-latest | docker exec -i k3s-verify ctr -n k8s.io images import -"
ssh root@7.6.52.148 "docker save wings-accel:verify-latest | docker exec -i k3s-verify ctr -n k8s.io images import -"
ssh root@7.6.52.148 "docker save vllm/vllm-openai:latest | docker exec -i k3s-verify ctr -n k8s.io images import -"
```

---

## Step 6：应用 K8s 部署清单

```bash
# 6.1 创建命名空间（已存在则忽略）
ssh root@7.6.52.148 "docker exec k3s-verify kubectl create ns wings-verify 2>/dev/null || true"

# 6.2 应用 Service
ssh root@7.6.52.148 "cat /home/zhanghui/infer-control-sidecar-verify/project-src/k8s/service.verify.yaml | docker exec -i k3s-verify kubectl apply -f -"

# 6.3 应用 Deployment
ssh root@7.6.52.148 "cat /home/zhanghui/infer-control-sidecar-verify/project-src/k8s/deployment.verify.yaml | docker exec -i k3s-verify kubectl apply -f -"

# 6.4 等待 rollout 完成（GPU 冷启动约 3-5 分钟，超时 360s）
ssh root@7.6.52.148 "docker exec k3s-verify kubectl -n wings-verify rollout status deploy/wings-infer --timeout=360s"
```

---

## Step 7：全链路验证

### 7.1 资源状态

```bash
ssh root@7.6.52.148 "docker exec k3s-verify kubectl -n wings-verify get all -o wide"
```

期望：Pod 状态 `2/2 Running`，Deployment `1/1`。

### 7.2 sidecar 启动产物

```bash
ssh root@7.6.52.148 "
POD=\$(docker exec k3s-verify kubectl -n wings-verify get pod -l app=wings-infer -o jsonpath='{.items[0].metadata.name}')
echo Pod: \$POD
docker exec k3s-verify kubectl -n wings-verify exec \$POD -c wings-infer -- cat /shared-volume/start_command.sh
"
```

期望：包含 `vllm` 相关命令、`--model` 参数指向 `/models/DeepSeek-R1-Distill-Qwen-1.5B`。

### 7.3 容器日志

```bash
ssh root@7.6.52.148 "
POD=\$(docker exec k3s-verify kubectl -n wings-verify get pod -l app=wings-infer -o jsonpath='{.items[0].metadata.name}')

echo '=== wings-infer 日志 ==='
docker exec k3s-verify kubectl -n wings-verify logs \$POD -c wings-infer --tail=100

echo '=== vllm-engine 日志 ==='
docker exec k3s-verify kubectl -n wings-verify logs \$POD -c vllm-engine --tail=100
"
```

vllm-engine 正常启动后日志末尾应出现：
```
(APIServer pid=8) INFO:     Application startup complete.
(APIServer pid=8) INFO:     127.0.0.1:xxxxx - "GET /health HTTP/1.1" 200 OK
```

### 7.4 健康探针（19000 端口）

```bash
ssh root@7.6.52.148 "
POD=\$(docker exec k3s-verify kubectl -n wings-verify get pod -l app=wings-infer -o jsonpath='{.items[0].metadata.name}')
docker exec k3s-verify kubectl -n wings-verify exec \$POD -c wings-infer -- curl -sS -m 5 http://127.0.0.1:19000/health
"
```

期望响应：
```json
{"s":1,"p":"ready","pid_alive":false,"backend_ok":true,"backend_code":200,"interrupted":false,"ever_ready":true}
```

### 7.5 /v1/models 接口（18000 代理端口）

```bash
ssh root@7.6.52.148 "
POD=\$(docker exec k3s-verify kubectl -n wings-verify get pod -l app=wings-infer -o jsonpath='{.items[0].metadata.name}')
docker exec k3s-verify kubectl -n wings-verify exec \$POD -c wings-infer -- curl -sS -i -m 8 http://127.0.0.1:18000/v1/models
"
```

期望：HTTP 200，JSON body 包含 `DeepSeek-R1-Distill-Qwen-1.5B`。

### 7.6 端到端推理（chat completions）

```bash
# 先把 payload 写入 pod 内（避免 Shell 多层转义问题）
ssh root@7.6.52.148 "
POD=\$(docker exec k3s-verify kubectl -n wings-verify get pod -l app=wings-infer -o jsonpath='{.items[0].metadata.name}')

# 写 payload
docker exec k3s-verify kubectl -n wings-verify exec \$POD -c wings-infer -- \
  sh -c 'printf '"'"'{\"model\":\"DeepSeek-R1-Distill-Qwen-1.5B\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}],\"max_tokens\":32}'"'"' > /tmp/chat.json'

# 经代理调用推理
docker exec k3s-verify kubectl -n wings-verify exec \$POD -c wings-infer -- \
  curl -sS -m 60 -X POST http://127.0.0.1:18000/v1/chat/completions \
  -H 'Content-Type: application/json' -d @/tmp/chat.json
"
```

**更简单的方式**（先上传脚本再执行）：

```bash
# 本地创建测试脚本
cat > /tmp/test_chat.sh <<'SCRIPT'
#!/bin/sh
printf '{"model":"DeepSeek-R1-Distill-Qwen-1.5B","messages":[{"role":"user","content":"hello"}],"max_tokens":32}' > /tmp/chat.json

echo "=== Direct vLLM (port 17000) ==="
curl -sS -m 60 -X POST http://127.0.0.1:17000/v1/chat/completions \
  -H 'Content-Type: application/json' -d @/tmp/chat.json

echo ""
echo "=== Via proxy (port 18000) ==="
curl -sS -m 60 -X POST http://127.0.0.1:18000/v1/chat/completions \
  -H 'Content-Type: application/json' -d @/tmp/chat.json
SCRIPT

# 上传并执行
scp /tmp/test_chat.sh root@7.6.52.148:/tmp/
ssh root@7.6.52.148 "
POD=\$(docker exec k3s-verify kubectl -n wings-verify get pod -l app=wings-infer -o jsonpath='{.items[0].metadata.name}')
docker cp /tmp/test_chat.sh k3s-verify:/tmp/
docker exec k3s-verify kubectl cp /tmp/test_chat.sh wings-verify/\$POD:/tmp/test_chat.sh -c wings-infer
docker exec k3s-verify kubectl -n wings-verify exec \$POD -c wings-infer -- bash /tmp/test_chat.sh
"
```

期望：两个请求均返回包含 `"role":"assistant"` 和 `"content"` 的 JSON 正文，`finish_reason: "stop"`。

---

## 本次验证结果（2026-02-28）

| 检查项 | 结果 |
|--------|------|
| Pod `2/2 Running` | ✅ `wings-infer-5b7cfd676b-99fb4` |
| `GET :19000/health` | ✅ `{"s":1,"p":"ready","backend_ok":true}` |
| `GET :18000/v1/models` | ✅ HTTP 200，返回 `DeepSeek-R1-Distill-Qwen-1.5B` |
| `POST :17000/v1/chat/completions`（直连 vLLM） | ✅ GPU 推理成功 |
| `POST :18000/v1/chat/completions`（通过代理） | ✅ GPU 推理成功，回复 `"Hello! How can I assist you today?"` |

---

## 关键 YAML 配置参考

### deployment.verify.yaml 核心要点

```yaml
spec:
  template:
    spec:
      volumes:
        - name: nvidia-libs
          hostPath:
            path: /mnt/nvidia-libs   # k3s-verify 容器内的驱动库目录（Step 3 准备）
            type: Directory

      containers:
        - name: vllm-engine
          securityContext:
            privileged: true          # 打通 /dev/nvidia* 访问

          command: ["/bin/sh", "-c"]
          args:
            - |
              export LD_LIBRARY_PATH=/usr/lib/nvidia-host:${LD_LIBRARY_PATH:-}
              # ... 等待 start_command.sh 并执行

          env:
            - name: NVIDIA_VISIBLE_DEVICES
              value: "all"
            - name: NVIDIA_DRIVER_CAPABILITIES
              value: "compute,utility"

          volumeMounts:
            - name: nvidia-libs
              mountPath: /usr/lib/nvidia-host   # LD_LIBRARY_PATH 指向此路径
```

---

## 常见问题排查

### Pod 卡在 Init:0/1

```bash
ssh root@7.6.52.148 "docker exec k3s-verify kubectl -n wings-verify describe pod <POD>"
```

查看 Events 字段。常见原因：
- `failed to get sandbox runtime: no runtime for "nvidia" is configured` → 说明 `runtimeClassName: nvidia` 还在，需删除。
- `Insufficient memory` → 宿主机内存不足，减小 memory limits。

### vLLM 启动失败：NVML Shared Library Not Found

```bash
ssh root@7.6.52.148 "docker exec k3s-verify kubectl -n wings-verify logs <POD> -c vllm-engine --tail=50"
```

原因：`/mnt/nvidia-libs/` 未正确准备或 `LD_LIBRARY_PATH` 未生效。重新执行 Step 3。

### vLLM 启动失败：RuntimeError: Failed to infer device type

说明 `LD_LIBRARY_PATH` 中能找到 `libnvidia-ml.so` 但 `/dev/nvidia*` 设备不可见。确认：
```bash
ssh root@7.6.52.148 "docker exec k3s-verify kubectl -n wings-verify exec <POD> -c vllm-engine -- ls /dev/nvidia*"
```
如果无设备，确认 `securityContext.privileged: true` 已设置，且 `k3s-verify` 容器本身是 `--privileged` 启动的。

### /v1/models 返回 500

已知 bug（已修复）：`make_upstream_headers(request.headers)` 应为 `make_upstream_headers(request)`。
确认修复：
```bash
ssh root@7.6.52.148 "docker exec k3s-verify kubectl -n wings-verify exec <POD> -c wings-infer -- grep -n 'make_upstream_headers' /app/proxy/gateway.py | head"
# 应输出: make_upstream_headers(request)  不含 .headers
```

---

## 清理（仅验证资源）

```bash
# 删除验证命名空间（不影响现网）
ssh root@7.6.52.148 "docker exec k3s-verify kubectl delete ns wings-verify --wait=false || true"

# 可选：停止 k3s-verify 容器
ssh root@7.6.52.148 "docker stop k3s-verify"
```

---

## 禁止操作

1. 不要删除现网业务容器。
2. 不要修改 `/home` 下现网业务工程目录。
3. 不要在非 `wings-verify` 命名空间执行删除类操作。
4. 不要直接在宿主机上使用 `kubectl`（需通过 `docker exec k3s-verify kubectl`）。
