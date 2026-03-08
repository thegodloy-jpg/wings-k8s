# infer-control-sidecar-main 启动测试步骤（隔离环境）

适用范围：`7.6.52.148` 远程机器，`/home/zhanghui` 工作目录。  
目标：在不破坏服务器原有逻辑和现网容器的前提下，完成项目启动链路验证。

## 1. 约束与隔离原则

1. 不修改现网容器和现网目录。
2. 验证仅使用：
   - 独立 Docker 容器：`k3s-verify`
   - 独立 K8s 命名空间：`wings-verify`
   - 独立远程目录：`/home/zhanghui/infer-control-sidecar-verify`
3. 所有部署和镜像变更仅作用于验证集群。

## 2. 远程基础检查

```bash
ssh root@7.6.52.148 "hostname; whoami; docker --version"
ssh root@7.6.52.148 "docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | head"
```

## 3. 启动/检查 k3s 验证集群

```bash
ssh root@7.6.52.148 "/home/zhanghui/docker-k8s-verify/manage-k3s.sh status"
ssh root@7.6.52.148 "/home/zhanghui/docker-k8s-verify/manage-k3s.sh kubectl get nodes -o wide"
ssh root@7.6.52.148 "/home/zhanghui/docker-k8s-verify/manage-k3s.sh kubectl get pods -n kube-system"
```

如果需要重建（并挂载模型目录）可执行：

```bash
ssh root@7.6.52.148 "bash /home/zhanghui/infer-control-sidecar-verify/recreate_k3s_with_models.sh"
```

## 4. 同步项目到隔离目录

本地执行：

```bash
scp -r F:/zhanghui/wings-k8s/infer-control-sidecar-main/infer-control-sidecar-main/. \
  root@7.6.52.148:/home/zhanghui/infer-control-sidecar-verify/project-src
```

远程确认：

```bash
ssh root@7.6.52.148 "ls -la /home/zhanghui/infer-control-sidecar-verify/project-src | head -n 40"
```

## 5. 构建验证镜像（隔离 tag）

远程执行：

```bash
ssh root@7.6.52.148 "
cd /home/zhanghui/infer-control-sidecar-verify/project-src
TS=$(date +%Y%m%d-%H%M%S)
echo $TS > /home/zhanghui/infer-control-sidecar-verify/TAG_TS

docker build -t wings-accel:verify-$TS -t wings-accel:verify-latest ./wings-accel
# 使用标准 Dockerfile（python:3.10-slim 基础镜像，不含 GPU 依赖）
docker build -f Dockerfile -t wings-infer:verify-$TS -t wings-infer:verify-latest .
"
```

## 6. 导入镜像到 k3s containerd

```bash
ssh root@7.6.52.148 "
TS=$(cat /home/zhanghui/infer-control-sidecar-verify/TAG_TS)

# 导入带时间戳 tag
docker save wings-accel:verify-$TS | docker exec -i k3s-verify ctr -n k8s.io images import -
docker save wings-infer:verify-$TS | docker exec -i k3s-verify ctr -n k8s.io images import -

# 同步导入 verify-latest tag（deployment.verify.yaml 使用该静态 tag）
docker save wings-accel:verify-latest | docker exec -i k3s-verify ctr -n k8s.io images import -
docker save wings-infer:verify-latest | docker exec -i k3s-verify ctr -n k8s.io images import -

docker image inspect vllm/vllm-openai:latest >/dev/null 2>&1 || docker tag vllm/vllm-openai:v0.15.0 vllm/vllm-openai:latest
docker save vllm/vllm-openai:latest | docker exec -i k3s-verify ctr -n k8s.io images import -
"
```

## 7. 应用验证清单（独立命名空间）

```bash
ssh root@7.6.52.148 "docker exec k3s-verify kubectl create ns wings-verify >/dev/null 2>&1 || true"

ssh root@7.6.52.148 "
cat /home/zhanghui/infer-control-sidecar-verify/project-src/k8s/service.verify.yaml \
| docker exec -i k3s-verify kubectl apply -f -

cat /home/zhanghui/infer-control-sidecar-verify/project-src/k8s/deployment.verify.yaml \
| docker exec -i k3s-verify kubectl apply -f -

docker exec k3s-verify kubectl -n wings-verify rollout status deploy/wings-infer --timeout=240s
"
```

## 8. 启动链路验证项（重点）

### 8.1 资源状态

```bash
ssh root@7.6.52.148 "docker exec k3s-verify kubectl -n wings-verify get all -o wide"
```

### 8.2 sidecar 启动产物

```bash
ssh root@7.6.52.148 "
POD=$(docker exec k3s-verify kubectl -n wings-verify get pod -l app=wings-infer -o jsonpath='{.items[0].metadata.name}')
docker exec k3s-verify kubectl -n wings-verify exec $POD -c wings-infer -- cat /shared-volume/start_command.sh
"
```

### 8.3 容器日志

```bash
ssh root@7.6.52.148 "
POD=$(docker exec k3s-verify kubectl -n wings-verify get pod -l app=wings-infer -o jsonpath='{.items[0].metadata.name}')

docker exec k3s-verify kubectl -n wings-verify logs $POD -c wings-infer --tail=200
docker exec k3s-verify kubectl -n wings-verify logs $POD -c vllm-engine --tail=260
"
```

### 8.4 接口探测

```bash
ssh root@7.6.52.148 "
POD=$(docker exec k3s-verify kubectl -n wings-verify get pod -l app=wings-infer -o jsonpath='{.items[0].metadata.name}')

docker exec k3s-verify kubectl -n wings-verify exec $POD -c wings-infer -- curl -sS -m 5 http://127.0.0.1:19000/health
docker exec k3s-verify kubectl -n wings-verify exec $POD -c wings-infer -- curl -sS -i -m 8 http://127.0.0.1:18000/v1/models
"
```

## 9. 当前已验证结论（本轮）

1. sidecar 启动链路正常：`start_command.sh` 成功生成，engine 容器成功读取并执行。
2. 控制面/代理进程正常启动：`wings-infer` 容器运行稳定。
3. ~~阻塞点在引擎后端：`vllm/vllm-openai:latest` 在当前验证拓扑下报 `Failed to infer device type`~~  
   **已修复（k8s/deployment.verify.yaml）**：
   - `vllm-engine` 容器注入 `VLLM_DEVICE=cpu`，强制 CPU 后端，跳过 NVML/CUDA 检测。
   - `wings-infer` 容器注入 `WINGS_DEVICE=cpu`，launcher 生成 CPU 兼容启动命令。
   - 去除 `nvidia.com/gpu` 资源请求，允许在无 GPU 节点正常调度。
4. 代理 `/v1/models` 500 错误已修复（`backend/app/proxy/gateway.py` 参数类型 bug）。
5. 下一步验证目标：CPU 模式下 `/health` 返回 200、`/v1/models` 返回模型列表、chat 端到端成功。

> **注意**：CPU 推理速度极慢，冷启动可能需数分钟；`readinessProbe.failureThreshold=60` 已放宽至约 10 分钟。

## 10. 可选清理（仅清理验证资源）

```bash
ssh root@7.6.52.148 "docker exec k3s-verify kubectl delete ns wings-verify --wait=false || true"
# 若需停止验证集群：
ssh root@7.6.52.148 "/home/zhanghui/docker-k8s-verify/manage-k3s.sh stop"
```

## 11. 不要执行的操作

1. 不要删除现网业务容器。
2. 不要修改 `/home` 下现网业务工程目录。
3. 不要在非 `wings-verify` 命名空间执行删除类操作。
