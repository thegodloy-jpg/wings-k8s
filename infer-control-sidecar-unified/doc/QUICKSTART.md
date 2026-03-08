# 快速上手

本文以 **vLLM-Ascend 分布式 (2 节点)** 为例，演示从零部署到推理验证的完整流程。其他场景步骤类似，替换对应 overlay 即可。

## 前提条件

- K8s 集群已就绪 (k3s / k8s 均可)
- 节点已安装对应加速卡驱动 (Ascend CANN / NVIDIA CUDA)
- Docker / containerd 可用
- 模型文件已下载到节点本地路径

## 步骤 1: 构建 Sidecar 镜像

```bash
cd infer-control-sidecar-unified/

# 构建
docker build -t wings-infer:latest .

# 如果是多节点，推送到私有仓库或各节点本地加载
docker save wings-infer:latest | ssh user@node2 'docker load'
```

## 步骤 2: 修改配置

编辑 overlay YAML，替换所有 `CHANGE_ME`:

```bash
cd k8s/overlays/vllm-ascend-distributed/

# 需要修改的文件:
# - statefulset.yaml: 镜像、模型路径、节点 IP、NodePort
# - service.yaml: NodePort (如需自定义)
# - kustomization.yaml: namespace (如需自定义)
```

**关键配置项**:

```yaml
# statefulset.yaml 中:
env:
  - name: HEAD_NODE_ADDR
    value: "192.168.1.110"       # ← rank-0 节点 IP
  - name: NODE_IPS
    value: "192.168.1.110,192.168.1.170"  # ← 所有节点 (逗号分隔)
  - name: MODEL_NAME
    value: "DeepSeek-R1-Distill-Qwen-1.5B"
  - name: MODEL_PATH
    value: "/models/DeepSeek-R1-Distill-Qwen-1.5B"

# 引擎镜像
- name: engine
  image: quay.io/ascend/vllm-ascend:v0.7.3   # ← 实际镜像

# Sidecar 镜像
- name: wings-infer
  image: wings-infer:latest

# 模型 hostPath
volumes:
  - name: model-vol
    hostPath:
      path: /data/models/DeepSeek-R1-Distill-Qwen-1.5B  # ← 节点实际路径
```

## 步骤 3: 部署

```bash
# 预览生成的 YAML
kubectl kustomize k8s/overlays/vllm-ascend-distributed/

# 部署
kubectl apply -k k8s/overlays/vllm-ascend-distributed/

# 观察 Pod 启动
kubectl -n wings-infer get pods -w
```

预期输出:
```
NAME                    READY   STATUS    RESTARTS   AGE
vllm-ascend-dist-0      2/2     Running   0          2m
vllm-ascend-dist-1      2/2     Running   0          2m
```

## 步骤 4: 等待就绪

```bash
# 健康检查 (201=启动中, 200=就绪)
watch -n 5 'curl -s http://192.168.1.110:30190/health'
```

首次加载模型可能需要数分钟，从 201 转为 200 即表示就绪:
```json
{"s": 1, "p": "ready", "backend_ok": true, "ever_ready": true}
```

## 步骤 5: 推理测试

```bash
# 通过代理端口 (推荐)
curl http://192.168.1.110:30180/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "DeepSeek-R1-Distill-Qwen-1.5B",
    "messages": [{"role": "user", "content": "你好"}],
    "max_tokens": 50,
    "stream": false
  }'

# 直接访问引擎 (仅调试)
curl http://192.168.1.110:30170/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "DeepSeek-R1-Distill-Qwen-1.5B",
    "messages": [{"role": "user", "content": "hello"}],
    "max_tokens": 50
  }'
```

## 步骤 6: 清理

```bash
kubectl delete -k k8s/overlays/vllm-ascend-distributed/
```

---

## 其他场景快速切换

```bash
# vLLM 单机 (NVIDIA GPU)
kubectl apply -k k8s/overlays/vllm-single/

# SGLang 单机
kubectl apply -k k8s/overlays/sglang-single/

# MindIE 分布式 (Ascend NPU)
kubectl apply -k k8s/overlays/mindie-distributed/

# vLLM 分布式 (NVIDIA GPU, Ray)
kubectl apply -k k8s/overlays/vllm-distributed/
```

每个 overlay 的 YAML 中都标有 `# CUSTOMIZE` 注释，按需修改即可。
