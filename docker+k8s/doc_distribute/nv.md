L20资源:7.6.52.148,账户root,密码:xfusion@1234!
7.6.16.150,账户root,密码:Xfusion@2026
工作目录：
- 7.6.52.148: /home/zhanghui
- 7.6.16.150: /home/zhanghui

容器命名约定：
- 所有本次验证新起 Docker 容器统一追加 `-zhanghui` 后缀
- server: `k3s-verify-server-zhanghui`
- agent: `k3s-verify-agent-zhanghui`

GPU 与模型约束：
- 两台机器均仅使用 1 张 L20
- 模型统一为 deepseek1.5b（示例路径：/mnt/models/DeepSeek-R1-Distill-Qwen-1.5B）
- 7.6.52.148 已有 vLLM/SGLang 镜像，优先复用，不重复拉取


任务：在这台机器上各选择一张L20，来验证nv场景下的分布式场景，具体来说采用方案a ，针对sglang,vllm(dp,ray)方案进行分布式代码的验证和开发，
方案 A 本质：把 wings/wings 裸机分布式逻辑"搬"到 K8s，用 StatefulSet Pod 序号替代 IP 地址判断角色。


每个 Pod  └─ wings-infer 读 NODE_RANK       ├─ rank=0 → 生成 head 脚本 → ray start --head + vllm serve       └─ rank=N → 生成 worker 脚本 → ray start --address=head --blockPod 间通信靠 Headless Service 提供固定 DNS，head 地址写死为 infer-0.infer-hl

步骤:

## 阶段一：基础环境准备

### 1. 配置两台机器无密码互访
```bash
# 在 .148 上生成密钥并推送到 .150
ssh-keygen -t rsa -N "" -f ~/.ssh/id_rsa   # 如已存在跳过
ssh-copy-id root@7.6.16.150

# 在 .150 上同样推送到 .148
ssh-copy-id root@7.6.52.148

# 验证
ssh root@7.6.16.150 "hostname"
ssh root@7.6.52.148 "hostname"
```

### 2. 确认两台机器的 Docker 环境 和 驱动库
参考机器：7.6.52.148（root / xfusion@1234!）

```bash
# 在 .148 和 .150 上分别执行，检查基础组件
docker --version

# 检查 GPU 可用性（每台各取 1 张 L20）并寻找驱动库版本
nvidia-smi 
ls -l /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.*
```

### 3. 构建基于 Docker 的 k3s 双节点集群
我们使用 `rancher/k3s` 容器来隔离 K8s 环境，保证和单机验证方案一致的环境底座。

> **注意：如果现网环境中已经有运行中的 k3s 容器，请使用新的容器名避免冲突。**

```bash
# 在 .148 作为 server（master），启动 k3s 容器
docker run -d --name k3s-verify-server-zhanghui --privileged --restart=unless-stopped \
  -p 6443:6443 \
  -v /mnt/models:/mnt/models \
  rancher/k3s:v1.30.6-k3s1 server --node-external-ip=7.6.52.148 

# 等待启动后，获取 join token
docker exec k3s-verify-server-zhanghui cat /var/lib/rancher/k3s/server/node-token

# 在 .150 作为 agent（worker）加入集群
docker run -d --name k3s-verify-agent-zhanghui --privileged --restart=unless-stopped \
  -v /mnt/models:/mnt/models \
  rancher/k3s:v1.30.6-k3s1 agent --server https://7.6.52.148:6443 \
  --token <上面获取的token> \
  --node-external-ip=7.6.16.150

# 验证节点状态（在 .148 执行）
docker exec k3s-verify-server-zhanghui kubectl get nodes -o wide
# 预期：两个节点均 Ready
```

### 4. 准备 nvidia 驱动库以供 Pod 挂载（替代 device plugin）
由于 k3s 运行在 Alpine 容器中，且不支持 nvidia-container-runtime，通过复制共享库+特权挂载方式透传 GPU。

```bash
# 在 .148 执行（针对 server 容器）
export CONT="k3s-verify-server-zhanghui"
export NV_VER="550.90.07" # 请替换为实际的 nvidia 驱动版本

docker exec $CONT mkdir -p /mnt/nvidia-libs
docker cp /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.$NV_VER $CONT:/mnt/nvidia-libs/
docker cp /usr/lib/x86_64-linux-gnu/libcuda.so.$NV_VER $CONT:/mnt/nvidia-libs/
docker exec $CONT sh -c "cd /mnt/nvidia-libs && ln -sf libnvidia-ml.so.$NV_VER libnvidia-ml.so.1 && ln -sf libnvidia-ml.so.1 libnvidia-ml.so"
docker exec $CONT sh -c "cd /mnt/nvidia-libs && ln -sf libcuda.so.$NV_VER libcuda.so.1 && ln -sf libcuda.so.1 libcuda.so"

# 在 .150 执行（针对 agent 容器），同理
export CONT="k3s-verify-agent-zhanghui"
export NV_VER="550.90.07"
# ... 执行上述相同的 docker cp 和 ln 命令 ...
```

---

## 阶段二：代码改造（backend-ascend-st-2603030944）

### 5. 改造目标文件清单
需修改以下四个文件以支持分布式：

| 文件 | 改动内容 |
|---|---|
| `app/core/start_args_compat.py` | 增加 `--nnodes`、`--node-rank`、`--head-node-addr`、`--distributed-executor-backend` 参数，移除 distributed 报错封锁 |
| `app/core/wings_entry.py` | 将新参数注入 merged dict，rank>0 时不注入 host/port |
| `app/engines/vllm_adapter.py` | `build_start_script()` 按 rank 和 backend 生成 head/worker 两套脚本 |
| `app/engines/sglang_adapter.py` | `build_start_script()` 增加 `--nnodes --node-rank --dist-init-addr` 分支 |
| `app/main.py` | rank>0 时跳过 proxy 启动 |

### 6. 各引擎分布式脚本生成逻辑

#### vLLM Ray（rank-0 head）
```bash
# start_command.sh 内容
export VLLM_HOST_IP=$(hostname -i)
export NCCL_SOCKET_IFNAME=eth0
export GLOO_SOCKET_IFNAME=eth0

ray start --head --port=6379 --num-gpus=1 --dashboard-host=0.0.0.0

# 等待 worker 加入
for i in $(seq 1 60); do
  COUNT=$(python3 -c "import ray; ray.init(address='auto',ignore_reinit_error=True); \
    print(len([n for n in ray.nodes() if n['alive']])); ray.shutdown()" 2>/dev/null || echo 0)
  [ "$COUNT" -ge "2" ] && break
  sleep 5
done

exec python3 -m vllm.entrypoints.openai.api_server \
  --model /models/<model_name> \
  --host 0.0.0.0 --port 17000 \
  --tensor-parallel-size 2 \
  --distributed-executor-backend ray
```

#### vLLM Ray（rank-1 worker）
```bash
# start_command.sh 内容
export NCCL_SOCKET_IFNAME=eth0
export GLOO_SOCKET_IFNAME=eth0

# 等待 head 可达
for i in $(seq 1 60); do
  python3 -c "import socket; s=socket.socket(); \
    s.settimeout(2); s.connect(('infer-0.infer-hl',6379)); s.close()" 2>/dev/null && break
  sleep 5
done

exec ray start --address=infer-0.infer-hl:6379 --num-gpus=1 --block
```

#### vLLM DP（rank-0）
```bash
exec python3 -m vllm.entrypoints.openai.api_server \
  --model /models/<model_name> \
  --host 0.0.0.0 --port 17000 \
  --data-parallel-address infer-0.infer-hl \
  --data-parallel-rpc-port 13355 \
  --data-parallel-size 2 \
  --data-parallel-size-local 1
```

#### vLLM DP（rank-1）
```bash
exec python3 -m vllm.entrypoints.openai.api_server \
  --model /models/<model_name> \
  --data-parallel-address infer-0.infer-hl \
  --data-parallel-rpc-port 13355 \
  --data-parallel-size 2 \
  --data-parallel-size-local 1 \
  --headless \
  --data-parallel-start-rank 1
```

#### SGLang（rank-0 和 rank-1）
```bash
# rank-0
exec python3 -m sglang.launch_server \
  --model-path /models/<model_name> \
  --host 0.0.0.0 --port 17000 \
  --nnodes 2 --node-rank 0 \
  --dist-init-addr infer-0.infer-hl:28030

# rank-1
exec python3 -m sglang.launch_server \
  --model-path /models/<model_name> \
  --host 0.0.0.0 \
  --nnodes 2 --node-rank 1 \
  --dist-init-addr infer-0.infer-hl:28030
```

---

## 阶段三：镜像构建与推送

### 7. 构建镜像并导入 k3s 的 containerd 中
因为 k3s 在独立的容器中，需要导入镜像才能被 k8s 使用。

```bash
# 在 .148 上构建（改造后的代码）
cd /home/zhanghui/infer-control-sidecar-main-nv-dist
docker build -t wings-infer:dist-nv-dev-zhanghui .

# 导出并导入到 .148 的 k3s-verify-server-zhanghui 容器中
docker save wings-infer:dist-nv-dev-zhanghui -o /tmp/wings-infer-zhanghui.tar
docker cp /tmp/wings-infer-zhanghui.tar k3s-verify-server-zhanghui:/tmp/
docker exec k3s-verify-server-zhanghui ctr -n k8s.io images import /tmp/wings-infer-zhanghui.tar

# 同样也需传到 .150 并导入其 k3s-verify-agent-zhanghui 容器中
scp /tmp/wings-infer-zhanghui.tar root@7.6.16.150:/tmp/
ssh root@7.6.16.150 "docker cp /tmp/wings-infer-zhanghui.tar k3s-verify-agent-zhanghui:/tmp/ && docker exec k3s-verify-agent-zhanghui ctr -n k8s.io images import /tmp/wings-infer-zhanghui.tar"
```

### 8. 确认引擎镜像并导入
```bash
# vLLM（优先复用 .148 本地已有镜像，例如 v0.13.0）
docker save vllm/vllm-openai:v0.13.0 | docker exec -i k3s-verify-server-zhanghui ctr -n k8s.io images import -
ssh root@7.6.16.150 "docker load < <(ssh root@7.6.52.148 docker save vllm/vllm-openai:v0.13.0)"
ssh root@7.6.16.150 "docker save vllm/vllm-openai:v0.13.0 | docker exec -i k3s-verify-agent-zhanghui ctr -n k8s.io images import -"

# SGLang（优先复用 .148 本地已有镜像）
docker save lmsysorg/sglang:latest | docker exec -i k3s-verify-server-zhanghui ctr -n k8s.io images import -
ssh root@7.6.16.150 "docker load < <(ssh root@7.6.52.148 docker save lmsysorg/sglang:latest)"
ssh root@7.6.16.150 "docker save lmsysorg/sglang:latest | docker exec -i k3s-verify-agent-zhanghui ctr -n k8s.io images import -"

# 由于启动 k3s 容器时已加了 -v /mnt/models:/mnt/models 映射
# 需确保两台机器外部实体路径下均有模型文件
ls /mnt/models/<model_name>
```

---

## 阶段四：K8s 资源部署（全部通过 docker exec k3s-verify-server-zhanghui 运行）

### 9. 创建 Namespace 和基础 K8s 资源
```bash
docker exec -i k3s-verify-server-zhanghui kubectl create namespace inference

# Headless Service（Pod 互相发现）
cat <<EOF | docker exec -i k3s-verify-server-zhanghui kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: infer-hl
  namespace: inference
spec:
  clusterIP: None
  selector:
    app: infer-dist
  ports:
    - name: ray-gcs
      port: 6379
    - name: dist-init
      port: 28030
    - name: dp-rpc
      port: 13355
EOF

# ClusterIP Service（对外 API，只打 rank-0）
cat <<EOF | docker exec -i k3s-verify-server-zhanghui kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: infer-api
  namespace: inference
spec:
  selector:
    app: infer-dist
    statefulset.kubernetes.io/pod-name: infer-0
  ports:
    - name: proxy
      port: 18000
      targetPort: 18000
EOF
```

### 10. 部署 StatefulSet（以 vLLM Ray 为例）
修改部署用到了 `securityContext: privileged: true` 和特供 `LD_LIBRARY_PATH`。

```bash
cat <<EOF | docker exec -i k3s-verify-server-zhanghui kubectl apply -f -
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: infer
  namespace: inference
spec:
  serviceName: infer-hl
  replicas: 2
  selector:
    matchLabels:
      app: infer-dist
  template:
    metadata:
      labels:
        app: infer-dist
    spec:
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector:
                matchLabels:
                  app: infer-dist
              topologyKey: kubernetes.io/hostname
      volumes:
        - name: shared-vol
          emptyDir: {}
        - name: models
          hostPath:
            path: /mnt/models
        # 新增驱动库透传目录（k3s内部创建的）
        - name: nvidia-libs
          hostPath:
            path: /mnt/nvidia-libs
            type: Directory
      containers:
        - name: wings-infer
          image: wings-infer:dist-nv-dev-zhanghui
          env:
            - name: DISTRIBUTED
              value: "true"
            - name: NNODES
              value: "2"
            - name: NODE_RANK
              valueFrom:
                fieldRef:
                  fieldPath: metadata.labels['apps.kubernetes.io/pod-index']
            - name: HEAD_NODE_ADDR
              value: "infer-0.infer-hl.inference.svc.cluster.local"
            - name: DISTRIBUTED_EXECUTOR_BACKEND
              value: "ray"
            - name: ENGINE
              value: "vllm"
            - name: MODEL_NAME
              value: "DeepSeek-R1-Distill-Qwen-1.5B"
            - name: MODEL_PATH
              value: "/models/DeepSeek-R1-Distill-Qwen-1.5B"
            - name: TENSOR_PARALLEL_SIZE
              value: "1"
            - name: ENGINE_PORT
              value: "17000"
            - name: PORT
              value: "18000"
            - name: HEALTH_PORT
              value: "19000"
            - name: SHARED_VOLUME_PATH
              value: "/shared-volume"
          volumeMounts:
            - name: shared-vol
              mountPath: /shared-volume
        - name: engine
          image: vllm/vllm-openai:v0.13.0
          securityContext:
            privileged: true  # 允许读取宿主机的所有设备，跳过 nvidia-runtime
          command: ["/bin/bash", "-c"]
          args:
            - |
              export LD_LIBRARY_PATH=/usr/lib/nvidia-host:\${LD_LIBRARY_PATH:-}
              while [ ! -f /shared-volume/start_command.sh ]; do sleep 2; done
              cat /shared-volume/start_command.sh
              bash /shared-volume/start_command.sh
          env:
            - name: NVIDIA_VISIBLE_DEVICES
              value: "all"
            - name: NVIDIA_DRIVER_CAPABILITIES
              value: "compute,utility"
          # 注意：移除了 resources.limits.nvidia.com 强制配额
          volumeMounts:
            - name: shared-vol
              mountPath: /shared-volume
            - name: nvidia-libs
              mountPath: /usr/lib/nvidia-host
            - name: models
              mountPath: /models
              readOnly: true
EOF
```

---

## 阶段五：验证

### 11. 观察 Pod 启动状态
```bash
docker exec -i k3s-verify-server-zhanghui kubectl get pods -n inference -w

# 预期：
# infer-0   2/2   Running   0   ...   node=7.6.52.148 (对应容器网络)
# infer-1   2/2   Running   0   ...   node=7.6.16.150 (对应容器网络)

# 查看 wings-infer 生成的脚本内容
docker exec -i k3s-verify-server-zhanghui kubectl exec -n inference infer-0 -c wings-infer -- \
  cat /shared-volume/start_command.sh

# 查看 engine 容器日志
docker exec -i k3s-verify-server-zhanghui kubectl logs -n inference infer-0 -c engine -f
docker exec -i k3s-verify-server-zhanghui kubectl logs -n inference infer-1 -c engine -f
```

### 12. 验证推理服务可用
由于部署在容器内部，需要通过 server 容器调 K8s:
```bash
# 等待引擎就绪（健康检查）
docker exec -i k3s-verify-server-zhanghui sh -c "curl http://<infer-api ClusterIP>:18000/health"

# 发送推理请求
docker exec -i k3s-verify-server-zhanghui sh -c "curl -X POST http://<infer-api ClusterIP>:18000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    \"model\": \"<model_name>\",
    \"messages\": [{\"role\": \"user\", \"content\": \"hello\"}]
  }'"
```

### 13. 各引擎切换验证
```bash
# 切换引擎后滚动重启
docker exec -i k3s-verify-server-zhanghui kubectl rollout restart statefulset/infer -n inference
docker exec -i k3s-verify-server-zhanghui kubectl rollout status statefulset/infer -n inference
```

---

## 阶段六：单节点引擎验证（2026-03-03）

> 跳过 Ray 分布式（k3s-in-Docker 下 CoreDNS 无法到达 API Server 10.43.0.1:443），
> 改为先验证 vLLM DP（无 Ray 单进程）和 SGLang 单节点推理链路。

### 环境概况

| 项目 | 详情 |
|---|---|
| k3s namespace | `wings-verify-dist` |
| 节点 | .148 (a100)，GPU0=A100(40GB)、GPU1=L20(46GB) |
| 引擎 GPU | CUDA_VISIBLE_DEVICES=1（L20 46GB） |
| 模型 | DeepSeek-R1-Distill-Qwen-1.5B（/mnt/models/，hardlink 实体目录） |
| wings-infer 镜像 | wings-infer:dist-nv-dev-zhanghui |
| vLLM 镜像 | vllm/vllm-openai:v0.13.0 |
| SGLang 镜像 | sglang-infer:zhanghui-20260228（21.7GiB，Docker 导入 k3s） |

### 遇到的问题及解决

| # | 问题 | 根因 | 解决方案 |
|---|---|---|---|
| 1 | `Failed to infer device type` | Pod 内 /dev/nvidia* 不可见 | YAML 中显式 hostPath CharDevice 挂载 nvidia0/1/ctl/uvm/uvm-tools/modeset |
| 2 | libcuda.so.1 找不到 | /mnt/nvidia-libs 只有 symlink 没有实体 | 复制实体驱动库(libcuda.so.550.90.07 等) + 相对 symlink |
| 3 | `OSError: config.json not found` | 模型目录是 symlink→/home/xxs/（容器内不存在） | `cp -al` hardlink 填充（零磁盘成本） |
| 4 | SGLang ENOEXEC nvidia-smi | k3s-in-Docker containerd 中直接 exec ELF 二进制报 Exec format error | engine 启动脚本中前置创建 shell 包装器 nvidia-smi（SGLang fallback torch.cuda） |
| 5 | SGLang CUDA OOM | vLLM DP 占用 L20 41GB，SGLang 也指定同卡 | 先删除已验证的 vLLM DP deployment，释放 L20 给 SGLang |

### vLLM DP 验证结果 ✅

```
Pod: infer-dp-765b6c665d-l5m6d   2/2 Running   节点: a100 (.148)
引擎: vllm/vllm-openai:v0.13.0   CUDA=1 (L20)   tp=1

模型加载: ✅ 15.5s, dtype=bfloat16, KV cache=36.23GiB, CUDA graphs captured
/v1/models: ✅ 返回 DeepSeek-R1-Distill-Qwen-1.5B
/v1/chat/completions: ✅ "2 + 2 = 4" (wings-infer:18000 → engine:17000)
```

YAML: `deployment-vllm-dp-nv-verify.yaml` + `service-vllm-dp-nv-verify.yaml`

### SGLang 验证结果 ✅

```
Pod: infer-sglang-78b8bfb786-kt8k7   2/2 Running   节点: a100 (.148)
引擎: sglang-infer:zhanghui-20260228   CUDA=1 (L20)   tp=1

模型加载: ✅ 0.82s, dtype=bfloat16, 3.51GB
KV Cache: ✅ K=18GB + V=18GB = 36GB, #tokens=1,347,841
CUDA graphs: ✅ bs=[1,2,4,8,12,16,24,32], 19.5s
Server: ✅ "The server is fired up and ready to roll!" Uvicorn on :17000

/v1/models: ✅ {"id":"DeepSeek-R1-Distill-Qwen-1.5B","owned_by":"sglang","max_model_len":5120}
/v1/chat/completions: ✅ "2 + 2 = 4" (0.34s, prompt=14, completion=50)
   wings-infer:18000 → sglang:17000 链路通
```

YAML: `deployment-sglang-nv-verify.yaml`（含 nvidia-smi shell 包装器 workaround）

### vLLM 单机验证（unified sidecar）✅

```
Pod: infer-0   2/2 Running   节点: a100 (.148)
引擎: vllm/vllm-openai:v0.13.0   CUDA=1 (L20)   tp=1
Sidecar: wings-infer:unified-zhanghui (infer-control-sidecar-unified)
GPU 占用: L20 41479 MiB / 46068 MiB

模型加载: ✅ 19.2s, dtype=bfloat16, 3.35GiB
Attention: FLASH_ATTN
torch.compile: ✅ 4.0s (Dynamo bytecode transform) + CUDA graph cache
Health:   ✅ {"s":1,"p":"ready","backend_ok":true,"backend_code":200}
/v1/models: ✅ DeepSeek-R1-Distill-Qwen-1.5B max_model_len=5120
/v1/chat/completions (17000 直连): ✅ "1+1=2" (12 tokens)
/v1/chat/completions (18000 代理): ✅ "2+2=4" thinking + answer (50 tokens)
```

关键 env vars:
- `CUDA_VISIBLE_DEVICES=1` + `CUDA_DEVICE_ORDER=PCI_BUS_ID` — 指定 L20
- `VLLM_HOST_IP=127.0.0.1` — 解决 k3s-in-Docker 下 c10d hostname 解析 hang
- `NCCL_SOCKET_IFNAME=lo` / `GLOO_SOCKET_IFNAME=lo` — NCCL 使用 loopback

YAML: `tmp-vllm-single.yaml`（StatefulSet, hostNetwork, privileged engine）