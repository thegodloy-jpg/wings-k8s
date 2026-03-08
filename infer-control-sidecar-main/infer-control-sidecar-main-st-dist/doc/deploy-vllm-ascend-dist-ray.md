# vLLM-Ascend 分布式推理部署文档（Ray TP=2, Ascend 910B2C）

> **版本**：2026-03-04  
> **作者**：zhanghui  
> **状态**：已验证通过  

---

## 1. 部署概览

### 1.1 架构

```
 ┌── Node .110 (910b-47) ──────────────────────┐     ┌── Node .170 (root) ──────────────────────────┐
 │ Pod: infer-vllm-0 (rank 0, hostNetwork)      │     │ Pod: infer-vllm-1 (rank 1, hostNetwork)      │
 │                                               │     │                                               │
 │  ┌─ wings-infer ─────────────┐                │     │  ┌─ wings-infer ─────────────┐                │
 │  │ 生成 start_command.sh     │                │     │  │ 生成 start_command.sh     │                │
 │  │ proxy → 127.0.0.1:17000  │ :18100         │     │  │ (不启动 proxy)            │                │
 │  │ health check              │ :19100         │     │  └─────────────────────────────┘                │
 │  └─────────────────────────────┘                │     │                                               │
 │                                               │     │                                               │
 │  ┌─ vllm-ascend engine ─────┐                │     │  ┌─ vllm-ascend engine ─────┐                │
 │  │ Ray Head :6379            │                │     │  │ Ray Worker → .110:6379   │                │
 │  │ vllm serve :17000        │                │     │  │ (--block, 不启动 serve)   │                │
 │  │ NPU 0 (davinci0)         │                │     │  │ NPU 0 (davinci0)         │                │
 │  └─────────────────────────────┘                │     │  └─────────────────────────────┘                │
 └─────────────────────────────────────────────────┘     └─────────────────────────────────────────────────┘
```

### 1.2 关键参数

| 参数 | 值 |
|------|-----|
| K8s 集群 | k3s v1.30.6+k3s1（Docker-in-Docker 方式运行） |
| 节点数 | 2（.110 rank-0, .170 rank-1） |
| 每节点 NPU | 1 张 Ascend 910B2C |
| Tensor Parallel | TP=2（跨 2 节点） |
| 分布式框架 | Ray |
| 引擎镜像 | `quay.io/ascend/vllm-ascend:v0.14.0rc1`（CANN 8.5.0, Python 3.11.14） |
| Sidecar 镜像 | `wings-infer:zhanghui-ascend-st-dist` |
| 模型 | DeepSeek-R1-Distill-Qwen-1.5B |
| 模型路径 | `/mnt/cephfs/models/DeepSeek-R1-Distill-Qwen-1.5B` |
| Namespace | `wings-verify-st-dist` |
| 网络模式 | hostNetwork |

### 1.3 端口规划

| 端口 | 用途 | 服务 |
|------|------|------|
| 6379 | Ray Head GCS | Ray cluster |
| 17000 | vLLM OpenAI API | vllm serve |
| 18100 | Wings 代理端口 | wings-infer proxy |
| 19100 | 健康检查端口 | wings-infer health |

> 端口选择为 18100/19100 而非默认的 18000/19000，是为了避免与同机器上运行的 mindie pods 发生 hostNetwork 端口冲突。

---

## 2. 前提条件

### 2.1 硬件

- 2 台服务器，各有 Ascend 910B2C NPU
- NPU 驱动已安装在宿主机 `/usr/local/Ascend/driver/`
- 设备文件存在：`/dev/davinci0`、`/dev/davinci_manager`、`/dev/hisi_hdc`

### 2.2 K3s 集群

集群通过 Docker-in-Docker 方式运行，k3s 运行在 Docker 容器内：

```
.110: k3s container = k3s-verify-server-ascend-zhanghui (server)
.170: k3s container = k3s-verify-agent-ascend-zhanghui  (agent)
```

验证集群就绪：

```bash
# 在 .110 上
docker exec k3s-verify-server-ascend-zhanghui kubectl get nodes
```

预期输出：

```
NAME      STATUS   ROLES                  AGE   VERSION
910b-47   Ready    control-plane,master   ...   v1.30.6+k3s1
root      Ready    <none>                 ...   v1.30.6+k3s1
```

### 2.3 模型文件

模型已通过 CephFS 挂载到两台机器的 `/mnt/cephfs/models/DeepSeek-R1-Distill-Qwen-1.5B`。

### 2.4 引擎镜像

`quay.io/ascend/vllm-ascend:v0.14.0rc1` 需在两个节点的 k3s 容器内都可用：

```bash
# 方式一：从 quay.io 拉取（需网络访问）
docker exec k3s-verify-server-ascend-zhanghui crictl pull quay.io/ascend/vllm-ascend:v0.14.0rc1

# 方式二：导出/导入 tar 文件
# 在已有镜像的节点上：
docker exec k3s-verify-server-ascend-zhanghui ctr -n k8s.io images export /tmp/vllm-ascend.tar quay.io/ascend/vllm-ascend:v0.14.0rc1
# 复制到另一节点后：
docker exec k3s-verify-agent-ascend-zhanghui ctr -n k8s.io images import /tmp/vllm-ascend.tar
```

---

## 3. 构建 wings-infer Sidecar 镜像

### 3.1 目录结构

```
infer-control-sidecar-main-st-dist/
├── Dockerfile                                  # 构建 wings-infer 容器
├── backend-dist-nv-20260303/
│   ├── requirements.txt
│   └── app/
│       ├── main.py                             # 入口
│       ├── core/
│       │   └── launcher.py                     # 生成 start_command.sh
│       ├── engines/
│       │   └── vllm_adapter.py                 # vLLM 命令组装（核心）
│       └── proxy/
│           └── gateway.py                      # 反向代理（BACKEND_URL）
└── k8s/
    └── statefulset-vllm-ascend-dist.yaml       # K8s StatefulSet 部署文件
```

### 3.2 构建命令

```bash
# 在 .110 的构建目录下
cd /data3/zhanghui/infer-control-sidecar-main-st-dist

# 构建镜像
docker build -f Dockerfile -t wings-infer:zhanghui-ascend-st-dist .
```

### 3.3 导入到 k3s

```bash
# 导出
docker save wings-infer:zhanghui-ascend-st-dist -o /tmp/wings-infer-st-dist.tar

# 导入到 .110 k3s
docker cp /tmp/wings-infer-st-dist.tar k3s-verify-server-ascend-zhanghui:/tmp/
docker exec k3s-verify-server-ascend-zhanghui ctr -n k8s.io images import /tmp/wings-infer-st-dist.tar

# 导入到 .170 k3s
scp /tmp/wings-infer-st-dist.tar 7.6.52.170:/tmp/
ssh 7.6.52.170 docker cp /tmp/wings-infer-st-dist.tar k3s-verify-agent-ascend-zhanghui:/tmp/
ssh 7.6.52.170 docker exec k3s-verify-agent-ascend-zhanghui ctr -n k8s.io images import /tmp/wings-infer-st-dist.tar
```

---

## 4. 核心机制详解

### 4.1 Sidecar 工作流

1. **wings-infer 容器**先启动，根据环境变量（`NODE_RANK`、`NNODES`、`ENGINE` 等）调用 `vllm_adapter.py` 的 `build_start_script()` 生成 `/shared-volume/start_command.sh`
2. **engine 容器**轮询 `/shared-volume/start_command.sh`，发现后执行
3. rank-0 的 engine 启动 Ray Head + vLLM serve；rank-1 的 engine 启动 Ray Worker（`--block`）
4. rank-0 的 wings-infer 启动反向代理（18100→17000）和健康检查（19100）

### 4.2 Triton Driver 补丁（关键）

vllm-ascend 的 worker.py 无条件导入 `torch_npu._inductor`，触发 `triton.runtime.driver._create_driver()`。Ascend NPU 没有 Triton 后端，会报 `RuntimeError: 0 active drivers`。

**解决方案**：在 `start_command.sh` 中通过 Python heredoc 在磁盘上修补 `triton/runtime/driver.py`，注入 `_NpuDummyDrv` 类：

```python
class _NpuDummyDrv:
    def get_current_target(self):
        # arch 必须是包含 "Ascend910B" 的字符串
        # torch_npu/_inductor/config.py:75 用 if ("Ascend910B" in target.arch)
        return SimpleNamespace(backend='npu', arch='Ascend910B', warp_size=0)

    def get_current_device(self): return 0

    def get_device_properties(self, device=0):
        # 返回 dict（非 SimpleNamespace）
        # 因为 config.py 用 prop["num_aicore"] 下标访问
        # 查询真实 NPU 核心数，避免 triton_utils.py 断言 _NUM_AICORE > 0 失败
        n = torch_npu.npu.get_device_name(device)
        c = 20 if '910B' in str(n) else 30
        return {'num_aicore': c, 'num_vectorcore': c}

    def __getattr__(self, name):
        # 递归返回自身实例（非 lambda）
        # 支持 driver.active.utils.get_device_properties() 链式访问
        return _NpuDummyDrv()

    def __call__(self, *a, **k): return self
    def __repr__(self): return '<NpuDummy>'
    def __int__(self): return 0
    def __bool__(self): return False
```

**关键约束**（4 次迭代试错得出）：

| 约束 | 原因 | 错误表现 |
|------|------|----------|
| `target.arch` 必须是字符串 | `config.py:75` 用 `in` 运算符 | `TypeError: argument of type 'int' is not iterable` |
| `get_device_properties` 必须返回 dict | `config.py` 用 `prop["num_aicore"]` 下标访问 | `TypeError: 'SimpleNamespace' object is not subscriptable` |
| `num_aicore > 0`, `num_vectorcore > 0` | `triton_utils.py:18` 有 assert | `AssertionError: Failed to detect device properties` |
| `__getattr__` 返回 `_NpuDummyDrv()` 而非 lambda | `driver.active.utils.xxx` 链式属性访问 | `AttributeError: 'function' object has no attribute 'get_device_properties'` |

### 4.3 CANN 驱动挂载策略

**仅挂载** `/usr/local/Ascend/driver/lib64/driver`（包含 `libascend_hal.so`），**不挂载**整个 `/usr/local/Ascend/driver/`。

原因：宿主机驱动 `common/` 目录下的库（如 `libmmpa.so` v25.x）与容器内 CANN 8.5.0 的库版本冲突，会导致链接错误。

### 4.4 BACKEND_URL 配置

wings-infer 的 `gateway.py` 默认将 `BACKEND_URL` 设为 `http://172.17.0.3:17000`（Docker bridge IP）。在 hostNetwork 模式下，该 IP 不可达。

**解决**：StatefulSet 中显式设置 `BACKEND_URL=http://127.0.0.1:17000`。

### 4.5 Ray 节点发现

Ray Worker 通过扫描 `NODE_IPS` 列表中所有 IP 的 6379 端口来发现 Ray Head：

```bash
for ip in $(echo $NODE_IPS_LIST | tr ',' ' '); do
    if python3 -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('$ip',6379)); s.close()" 2>/dev/null; then
        HEAD_IP=$ip
        break 2
    fi
done
```

这种方式避免了硬编码 Head IP，支持 StatefulSet Parallel 模式下 Pod 到节点的非确定性映射。

### 4.6 IP 检测

vllm-ascend 容器内 **没有** `ip` 命令。使用以下替代方案：

- **VLLM_HOST_IP**：优先使用 K8s Downward API 注入的 `$POD_IP`（hostNetwork 模式下等于宿主机 IP），fallback 到 Python UDP socket trick
- **IFNAME**：从 `/proc/net/route` 用 awk 提取默认路由接口

---

## 5. 部署步骤

### 5.1 创建 Namespace

```bash
docker exec k3s-verify-server-ascend-zhanghui kubectl create namespace wings-verify-st-dist
```

### 5.2 部署 StatefulSet

```bash
# 将 YAML 复制到 k3s 容器内
docker cp statefulset-vllm-ascend-dist.yaml k3s-verify-server-ascend-zhanghui:/tmp/sts.yaml

# 部署
docker exec k3s-verify-server-ascend-zhanghui kubectl apply -f /tmp/sts.yaml
```

### 5.3 监控启动

```bash
# 监控 Pod 状态
docker exec k3s-verify-server-ascend-zhanghui kubectl -n wings-verify-st-dist get pods -w

# 监控 rank-0 引擎日志（等待模型加载完成）
docker exec k3s-verify-server-ascend-zhanghui kubectl -n wings-verify-st-dist logs -f infer-vllm-0 -c engine

# 监控 rank-1 worker 日志
docker exec k3s-verify-server-ascend-zhanghui kubectl -n wings-verify-st-dist logs -f infer-vllm-1 -c engine
```

预期启动序列：
1. 两个 Pod 同时创建（Parallel 模式）
2. wings-infer 容器生成 `start_command.sh`
3. engine 容器执行 Triton 补丁 + CANN env source
4. rank-0 启动 Ray Head，rank-1 连接 Ray Head
5. rank-0 等待 2 个节点加入后启动 `vllm serve`
6. 模型加载，API server 就绪

**成功标志**（rank-0 engine 日志）：

```
INFO:     Started server process [xxx]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:17000
```

### 5.4 预期最终状态

```
NAME           READY   STATUS    RESTARTS   AGE
infer-vllm-0   2/2     Running   0          5m
infer-vllm-1   2/2     Running   0          5m
```

---

## 6. 验证测试

### 6.1 健康检查

```bash
curl -s -w '\nHTTP_CODE: %{http_code}\n' http://7.6.52.110:19100/health | python3 -m json.tool
```

**HTTP 状态码含义**：

| HTTP Code | 状态 (`p` 字段) | 含义 |
|-----------|----------------|------|
| **200** | `ready` | 引擎就绪，推理服务可用 |
| **201** | `starting` | 启动过渡期，引擎尚未完成初始化（正常现象） |
| **502** | `start_failed` | 在启动宽限期（`STARTUP_GRACE_MS`，默认 60 分钟）内仍未就绪 |
| **503** | `degraded` | 曾经就绪后变为不可用（退化状态） |

**正常启动流程**：
1. Pod 启动后，健康检查立即返回 **201**（`starting`），表示引擎正在初始化
2. 经过 Ray 集群建立 + 模型加载（约 2-5 分钟），引擎 `/health` 开始返回 200
3. wings-infer 健康监控循环检测到引擎就绪后，状态机转为 `ever_ready=true, status=1`
4. 健康检查切换为 **200**（`ready`），之后持续返回 200

**响应体字段说明**：

```json
{
  "s": 1,              // 内部三态：0=启动中/未知, 1=就绪, -1=故障
  "p": "ready",        // 阶段字符串
  "pid_alive": false,  // PID 检查（WINGS_SKIP_PID_CHECK=true 时忽略此值）
  "backend_ok": true,  // 引擎 /health 是否返回 200
  "backend_code": 200, // 引擎 /health 实际 HTTP 状态码
  "interrupted": false, // 曾就绪后变为不可用
  "ever_ready": true,  // 是否曾达到就绪状态
  "cf": 0,             // 连续失败次数
  "lat_ms": 3          // 引擎 /health 探测延迟（毫秒）
}
```

> **K8s readinessProbe 集成**：StatefulSet 中 readinessProbe 配置了 `initialDelaySeconds: 60, failureThreshold: 72`，容忍最长约 12 分钟的 201 过渡期。引擎完全就绪后（返回 200），Pod 才会被标记为 Ready。

### 6.2 直接引擎测试（端口 17000）

```bash
curl -s http://7.6.52.110:17000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "DeepSeek-R1-Distill-Qwen-1.5B",
    "messages": [{"role":"user","content":"hello"}],
    "max_tokens": 50
  }'
```

### 6.3 代理测试（端口 18100）

```bash
curl -s http://7.6.52.110:18100/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "DeepSeek-R1-Distill-Qwen-1.5B",
    "messages": [{"role":"user","content":"hello"}],
    "max_tokens": 50
  }'
```

预期返回 JSON 包含 `choices[0].message.content`。

---

## 7. 环境变量参考

### 7.1 wings-infer 容器

| 变量 | 值 | 说明 |
|------|-----|------|
| `WINGS_DEVICE` | `ascend` | 硬件类型 |
| `DISTRIBUTED` | `true` | 启用分布式模式 |
| `NNODES` | `2` | 节点总数 |
| `NODE_RANK` | Downward API `pod-index` | 当前节点序号 |
| `HEAD_NODE_ADDR` | `7.6.52.110` | Head 固定 IP |
| `NODE_IPS` | `7.6.52.110,7.6.52.170` | 所有节点 IP |
| `ENGINE` | `vllm_ascend` | 引擎类型 |
| `DISTRIBUTED_EXECUTOR_BACKEND` | `ray` | 分布式后端 |
| `TENSOR_PARALLEL_SIZE` | `1` | 单节点 TP（adapter 内部用 nnodes=2） |
| `MODEL_NAME` | `DeepSeek-R1-Distill-Qwen-1.5B` | 模型名称 |
| `MODEL_PATH` | `/models/DeepSeek-R1-Distill-Qwen-1.5B` | 模型路径 |
| `ENGINE_PORT` | `17000` | 引擎端口 |
| `PORT` | `18100` | 代理端口 |
| `HEALTH_PORT` | `19100` | 健康检查端口 |
| `BACKEND_URL` | `http://127.0.0.1:17000` | 代理后端地址 |
| `WINGS_SKIP_PID_CHECK` | `true` | K8s sidecar 跳过 PID 检查 |

### 7.2 engine 容器

| 变量 | 值 | 说明 |
|------|-----|------|
| `ASCEND_VISIBLE_DEVICES` | `0` | NPU 设备编号（k3s 内无实际效果） |
| `POD_IP` | Downward API `status.podIP` | Pod IP（hostNetwork = 宿主机 IP） |

### 7.3 start_command.sh 内部设置的变量

| 变量 | 说明 |
|------|------|
| `VLLM_HOST_IP` | 从 `$POD_IP` 获取，用于 Ray 和 vLLM |
| `HCCL_IF_IP` | HCCL 通信 IP |
| `HCCL_SOCKET_IFNAME` | HCCL socket 接口名（从 /proc/net/route 获取） |
| `GLOO_SOCKET_IFNAME` | Gloo 通信接口名 |
| `HCCL_WHITELIST_DISABLE` | `1`，禁用 HCCL 白名单限制 |

---

## 8. 故障排查

### 8.1 常见错误及解决

| 错误 | 原因 | 解决 |
|------|------|------|
| `RuntimeError: 0 active drivers` | Triton 找不到 NPU 驱动 | 检查 Triton 补丁是否成功执行 |
| `TypeError: 'SimpleNamespace' not subscriptable` | Triton 补丁返回格式错误 | `get_device_properties` 必须返回 dict |
| `TypeError: argument of type 'int' is not iterable` | `target.arch` 是整数 | `arch` 必须是包含 `Ascend910B` 的字符串 |
| `AssertionError: Failed to detect device properties` | NPU 核心数为 0 | `num_aicore` 和 `num_vectorcore` 必须大于 0 |
| `Backend connect error: All connection attempts failed` | wings-infer 代理连不上引擎 | 检查 `BACKEND_URL=http://127.0.0.1:17000` |
| `libmmpa.so` 版本冲突 | 宿主机整个 driver 目录挂载 | 仅挂载 `lib64/driver` 子目录 |
| Ray Worker 找不到 Head | 网络或端口问题 | 检查 hostNetwork 模式、`NODE_IPS` 正确性 |
| engine 容器不断重启 | `start_command.sh` 执行失败 | 查看 engine 容器日志和 wings-infer 日志 |

### 8.2 常用调试命令

```bash
# 查看 Pod 状态和事件
kubectl -n wings-verify-st-dist describe pod infer-vllm-0

# 查看 wings-infer 生成的启动脚本
kubectl -n wings-verify-st-dist exec infer-vllm-0 -c engine -- cat /shared-volume/start_command.sh

# 查看 Triton 补丁是否生效
kubectl -n wings-verify-st-dist exec infer-vllm-0 -c engine -- grep -c "PATCHED_NPU" /usr/local/lib/python3.11/site-packages/triton/runtime/driver.py

# Ray 集群状态（在 rank-0 上）
kubectl -n wings-verify-st-dist exec infer-vllm-0 -c engine -- python3 -c "import ray; ray.init(address='auto'); print(ray.nodes())"

# 引擎健康检查
kubectl -n wings-verify-st-dist exec infer-vllm-0 -c engine -- curl -s http://127.0.0.1:17000/health
```

### 8.3 重新部署

```bash
# 删除并重建
kubectl -n wings-verify-st-dist delete statefulset infer-vllm
kubectl apply -f /tmp/sts.yaml
```

---

## 9. 已知限制

1. **k3s 无 Ascend Device Plugin**：`ASCEND_VISIBLE_DEVICES` 环境变量在 k3s 内无效，NPU 的分配依赖 privileged 模式和 hostPath 设备挂载
2. **k3s 无 Ascend Docker Runtime**：无法自动注入设备，需手动挂载 `/dev/davinci*`
3. **Triton 补丁为运行时修改**：每次 Pod 重启都需要重新执行补丁，补丁嵌入在 `start_command.sh` 中自动执行
4. **hostNetwork 端口冲突**：同一节点上的多个推理服务需要使用不同端口
5. **TP 仅等于 nnodes**：当前设计每节点使用 1 张 NPU，TP = 节点数（2），不支持节点内多卡并行
6. **Node 亲和性未硬编码**：rank-0 到 .110 的绑定依赖 PodAntiAffinity + 调度结果，生产环境建议增加 nodeSelector
