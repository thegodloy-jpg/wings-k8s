# infer-control-sidecar-main Docker + K8s 验证方案

生成时间: 2026-02-28
适用项目: `F:\zhanghui\wings-k8s\infer-control-sidecar-main\infer-control-sidecar-main`

## 1. 目标与约束

### 目标
- 在宿主机只允许安装 Docker（不允许安装 Kubernetes 组件）的前提下，完成 `infer-control-sidecar-main` 的可重复验证。
- 验证重点为 sidecar 启动链路、端口连通、健康探针、基础 API 转发能力。

### 约束
- 宿主机不可安装 `kubeadm/k3s/kind/kubectl`（可通过容器内工具规避）。
- 项目当前为 v4 sidecar 端口规划：`17000(engine) / 18000(proxy) / 19000(health)`。
- `distributed` 在当前代码中明确不支持。

## 2. 项目现状识别（与验证直接相关）

- 主部署文件: `k8s/deployment.yaml`
  - `wings-infer` + `vllm-engine` 双容器 + `accel-init` initContainer。
  - 使用共享卷 `/shared-volume/start_command.sh` 作为启动契约。
  - 使用 `hostPath: /mnt/models` 挂载模型。
  - `vllm-engine` 资源包含 `nvidia.com/gpu: 1`。
- 服务文件: `k8s/service.yaml`
  - Service 端口为 `18000`（不是 9000）。
- 运行入口: `backend/app/main.py`
  - 只写启动命令并管理 proxy/health 子进程，不直接拉起引擎。
- 端口默认值: `backend/app/config/settings.py`
  - `ENGINE_PORT=17000`, `PORT=18000`, `HEALTH_PORT=19000`。
- 已有脚本
  - `verify_e2e.sh` 已按 `17000/18000/19000` 体系做校验。
  - `deploy.sh` 仍有 `9000` 遗留（如 port-forward），不建议直接作为主验证脚本。

## 3. 方案选型评估（在“仅 Docker”前提下）

| 方案 | 可行性 | 与项目适配度 | 复杂度 | 结论 |
|---|---|---|---|---|
| k3s in Docker（推荐） | 高 | 高 | 中 | 首选，落地最快 |
| kind in Docker | 中 | 中高 | 中高 | 可做第二方案 |
| 远端现成 K8s 集群 | 高 | 最高 | 中 | 作为发布前验收 |

结论: 先用 `k3s in Docker` 完成本地验证闭环，再视需要补一次远端集群验收。

## 4. 推荐实施方案（k3s in Docker）

### 阶段 A: 环境与工件准备

1. 启动 k3s 容器集群（宿主机只需 Docker）。
2. 构建项目镜像：
   - `wings-infer:latest`（项目根 Dockerfile）
   - `wings-accel:latest`（`wings-accel/Dockerfile`）
3. 将镜像导入 k3s 的 containerd（不能只停留在宿主机 Docker）。

### 阶段 B: 部署与冒烟验证

1. `kubectl apply -f k8s/service.yaml`
2. `kubectl apply -f k8s/deployment.yaml`
3. 观察：
   - `accel-init` 是否完成
   - `wings-infer` 与 `vllm-engine` 日志是否进入预期流程
   - `/shared-volume/start_command.sh` 是否生成

### 阶段 C: 功能验证

1. 健康探针验证（19000）：
   - Pod Ready
   - `/health` 返回合理状态
2. 代理端口验证（18000）：
   - `/v1/chat/completions` 或 `/v1/completions` 可达
3. 重启恢复验证：
   - 删除 Pod 后，重建能再次形成启动链路

### 阶段 D: 稳定性与回归基线

1. 连续执行 `verify_e2e.sh verify`（或同等检查）
2. 保留关键日志与事件用于回归对比

## 5. 可直接执行的命令模板（Linux 宿主）

> 注: 下面是模板，端口/路径可按现场调整。

```bash
# 1) 起 k3s
docker run -d --name k3s --privileged -p 6443:6443 rancher/k3s:v1.30.6-k3s1 server

# 2) 集群可用性
docker exec k3s kubectl get nodes

# 3) 构建镜像（在项目目录）
docker build -t wings-infer:latest .
docker build -t wings-accel:latest ./wings-accel

# 4) 导入 k3s containerd
docker save wings-infer:latest | docker exec -i k3s ctr -n k8s.io images import -
docker save wings-accel:latest | docker exec -i k3s ctr -n k8s.io images import -

# 5) 下发清单
docker cp k8s k3s:/work/k8s
docker exec k3s kubectl apply -f /work/k8s/service.yaml
docker exec k3s kubectl apply -f /work/k8s/deployment.yaml

# 6) 观察状态
docker exec k3s kubectl get pods -A -o wide
docker exec k3s kubectl logs deploy/wings-infer -c wings-infer --tail=200
docker exec k3s kubectl logs deploy/wings-infer -c vllm-engine --tail=200
```

## 6. 风险与规避

### 风险 1: GPU 资源不可调度
- 现象: `0/1 nodes are available: insufficient nvidia.com/gpu`。
- 规避:
  - 若仅验证控制链路，可临时去掉 GPU request/limit。
  - 若验证真实推理，必须准备可用 GPU runtime 路径（含设备插件能力）。

### 风险 2: 模型路径不可用
- 现象: 引擎容器启动失败或模型加载失败。
- 原因: 当前清单使用 `hostPath: /mnt/models`，在 Docker 内 k3s 场景需确认该路径在 k3s 容器节点可见。
- 规避:
  - 将模型目录挂入 k3s 容器并与 `/mnt/models` 对齐。
  - 或改成 PVC/对象存储方案。

### 风险 3: 端口认知不一致
- 现象: 调用 9000 失败。
- 原因: 当前 v4 架构外部代理端口为 18000。
- 规避: 统一使用 18000（health 19000，engine 17000）。

## 7. 验收标准（建议）

- P0 启动链路
  - `start_command.sh` 生成成功。
  - `vllm-engine` 能读取并执行启动命令。
- P1 健康与探针
  - Pod Ready，`/health` 可返回。
- P2 业务接口
  - 至少 1 个 OpenAI 兼容接口成功返回（非 5xx）。
- P3 可恢复性
  - Pod 删除后自动恢复，仍满足 P0-P2。

## 8. 建议的验证顺序

1. 先做“部署链路验证”（P0/P1）。
2. 再做“接口联通验证”（P2）。
3. 最后做“重启恢复验证”（P3）。

这样能最快定位问题归因（环境、引擎、还是代理）。
