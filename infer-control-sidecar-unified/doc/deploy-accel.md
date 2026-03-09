# Accel 加速特性部署指南

## 概述

Wings-Accel 是一个**可选的加速增强组件**，通过 K8s initContainer 模式将 `wings_engine_patch` Python 包注入到推理引擎容器中，在引擎启动前完成安装。

### 工作原理

```
┌─── K8s Pod ────────────────────────────────────────────────────────┐
│                                                                     │
│  [1] initContainer: accel-init                                      │
│       wings-accel:latest                                            │
│       cp -r /accel/* → /accel-volume/                               │
│                                                                     │
│  [2] wings-infer (sidecar)                                          │
│       ENABLE_ACCEL=true 时：                                        │
│       → 向 start_command.sh 注入                                    │
│         export WINGS_ENGINE_PATCH_OPTIONS='{"vllm":["test_patch"]}' │
│       → 告诉 wings_engine_patch 激活哪些补丁                        │
│                                                                     │
│  [3] engine 容器                                                    │
│       等待 start_command.sh                                         │
│       ENABLE_ACCEL=true → cd /accel-volume && bash install.sh       │
│       执行 start_command.sh 启动引擎（含 PATCH_OPTIONS 环境变量）   │
│                                                                     │
│  共享卷:                                                            │
│    /shared-volume  (emptyDir)  ← 启动脚本                           │
│    /accel-volume   (emptyDir)  ← 加速包文件                         │
└─────────────────────────────────────────────────────────────────────┘
```

### 目录结构

```
wings-accel/
├── Dockerfile                        # Alpine 镜像，打包 /accel/ 目录
├── install.sh                        # 入口安装脚本
├── supported_features.json           # 引擎兼容性矩阵
└── wings_engine_patch/
    ├── install.sh                    # pip install *.whl
    ├── wings_engine_patch-*.whl      # 核心补丁包
    └── wrapt-*.whl                   # 依赖包
```

## 构建 Accel 镜像

```bash
# 使用构建脚本 (推荐)
bash build-accel-image.sh              # 默认 tag: latest
bash build-accel-image.sh v1.0.0       # 自定义 tag

# 或手动构建
cd wings-accel/
docker build -t wings-accel:latest .
```

产出镜像：`wings-accel:<TAG>`（基于 Alpine 3.18，体积极小）

多节点环境需推送到私有仓库：
```bash
docker tag wings-accel:latest your-registry/wings-accel:latest
docker push your-registry/wings-accel:latest
```

## 启用 Accel

所有 8 个 K8s overlay（4 引擎 × 单机/分布式）均已内置 Accel 支持。默认通过 `ENABLE_ACCEL` 环境变量控制。

### 启用（默认）

在 deployment/statefulset YAML 中，确认以下配置：

```yaml
# wings-infer 容器
env:
  - name: ENABLE_ACCEL
    value: "true"

# engine 容器
env:
  - name: ENABLE_ACCEL
    value: "true"
```

### 禁用

将两个容器的 `ENABLE_ACCEL` 都设为 `"false"`：

```yaml
env:
  - name: ENABLE_ACCEL
    value: "false"
```

> **注意**：`wings-infer` 和 `engine` 容器的 `ENABLE_ACCEL` 值应保持一致。

## 适用引擎

| 引擎 | 单机 | 分布式 | Accel 支持 |
|------|------|--------|-----------|
| vLLM (NVIDIA) | `vllm-single/` | `vllm-distributed/` | ✅ |
| vLLM-Ascend | `vllm-ascend-single/` | `vllm-ascend-distributed/` | ✅ |
| SGLang | `sglang-single/` | `sglang-distributed/` | ✅ |
| MindIE | `mindie-single/` | `mindie-distributed/` | ✅ |

## K8s 资源说明

Accel 在 K8s 层面引入 3 个额外资源：

### 1. initContainer: `accel-init`

```yaml
initContainers:
  - name: accel-init
    image: wings-accel:latest          # ← 替换为实际镜像地址
    imagePullPolicy: IfNotPresent
    command: ["/bin/sh", "-c"]
    args:
      - |
        echo "Copying accel files to shared volume..."
        cp -r /accel/* /accel-volume/
        echo "Accel files copied successfully"
    volumeMounts:
      - name: accel-volume
        mountPath: /accel-volume
```

- 职责：将镜像内的 `/accel/` 全部文件拷贝到 `accel-volume` 共享卷
- 生命周期：Pod 启动前执行一次，完成后退出
- 失败影响：如 initContainer 失败，Pod 不会启动（K8s 默认行为）

### 2. Volume: `accel-volume`

```yaml
volumes:
  - name: accel-volume
    emptyDir: {}
```

- 挂载到 initContainer (`/accel-volume`) 和 engine 容器 (`/accel-volume`)
- 临时卷，Pod 销毁后自动清理

### 3. Engine 容器 args 变更

原始（无 Accel）:
```bash
while [ ! -f /shared-volume/start_command.sh ]; do sleep 2; done
cd /shared-volume && bash start_command.sh
```

启用 Accel 后:
```bash
while [ ! -f /shared-volume/start_command.sh ]; do sleep 2; done

# 条件安装
if [ "$ENABLE_ACCEL" = "true" ]; then
  cd /accel-volume && bash install.sh
fi

cd /shared-volume && bash start_command.sh
```

## Python Sidecar 层注入逻辑

除了 K8s 层面的 whl 包安装，wings-infer sidecar 还会在 Python 层面向 `start_command.sh` 注入 `WINGS_ENGINE_PATCH_OPTIONS` 环境变量，告诉 `wings_engine_patch` 包要激活哪些补丁功能。

### 工作机制

当 `ENABLE_ACCEL=true` 时，`wings_entry.py` 的 `build_launcher_plan()` 会在脚本头部自动注入：

```bash
#!/usr/bin/env bash
set -euo pipefail
export WINGS_ENGINE_PATCH_OPTIONS='{"vllm": ["test_patch"]}'    # ← 自动注入
exec python3 -m vllm.entrypoints.openai.api_server ...
```

### 引擎映射

| 引擎名 | PATCH_OPTIONS key | 示例 |
|--------|-------------------|------|
| `vllm` | `vllm` | `{"vllm": ["test_patch"]}` |
| `vllm_ascend` | `vllm` | `{"vllm": ["test_patch"]}` |
| `sglang` | `sglang` | `{"sglang": ["test_patch"]}` |
| `mindie` | `mindie` | `{"mindie": ["test_patch"]}` |

### 自定义 PATCH_OPTIONS

通过设置 `WINGS_ENGINE_PATCH_OPTIONS` 环境变量可覆盖自动生成的值：

```yaml
# wings-infer 容器环境变量
env:
  - name: ENABLE_ACCEL
    value: "true"
  - name: WINGS_ENGINE_PATCH_OPTIONS
    value: '{"vllm": ["custom_patch_1", "custom_patch_2"]}'  # 自定义补丁列表
```

设置此变量后，sidecar 将直接使用提供的值，不再按引擎名自动生成。

### 代码位置

- 注入逻辑: `backend/app/core/wings_entry.py` → `_build_accel_env_line()`
- 开关: `backend/app/config/settings.py` → `ENABLE_ACCEL`

## 部署示例

以 vLLM 单机为例：

```bash
# 1. 构建 accel 镜像
bash build-accel-image.sh

# 2. 编辑配置 (确认 ENABLE_ACCEL=true)
vi k8s/overlays/vllm-single/deployment.yaml

# 3. 部署
kubectl apply -k k8s/overlays/vllm-single/

# 4. 查看 Pod 状态 (应看到 init:0/1 → Running)
kubectl -n wings-infer get pods -w

# 5. 检查 accel 安装日志
kubectl -n wings-infer logs <pod-name> -c engine | grep -i accel
```

预期日志输出：
```
[engine] Accel is enabled, installing accel packages...
[engine] Accel packages installed successfully
[engine] Executing start_command.sh...
```

## 故障排查

### initContainer 失败

```bash
# 查看 initContainer 日志
kubectl -n wings-infer logs <pod-name> -c accel-init

# 常见原因：
# - 镜像拉取失败 → 检查 imagePullPolicy 和仓库配置
# - 权限问题   → 检查 install.sh 是否有执行权限 (已在 Dockerfile 中设置)
```

### Accel 安装失败

```bash
# 查看 engine 容器日志
kubectl -n wings-infer logs <pod-name> -c engine

# 常见原因：
# - whl 包与 Python 版本不兼容 → 检查 supported_features.json
# - pip 不可用 → 检查引擎基础镜像是否包含 pip
```

### 禁用 Accel 后 Pod 仍失败

即使 `ENABLE_ACCEL=false`，initContainer 仍会执行（仅拷贝文件，不安装）。  
如果 accel 镜像不可用，需要**移除** initContainer 和 accel-volume 配置，  
或确保镜像可访问。

### Patch 包安装成功但未激活

检查 `start_command.sh` 中是否包含 `WINGS_ENGINE_PATCH_OPTIONS`：

```bash
# 查看生成的启动脚本
kubectl -n wings-infer exec <pod-name> -c wings-infer -- cat /shared-volume/start_command.sh

# 应包含类似内容:
# export WINGS_ENGINE_PATCH_OPTIONS='{"vllm": ["test_patch"]}'
```

如果缺失，检查：
- wings-infer 容器的 `ENABLE_ACCEL` 环境变量是否为 `"true"`
- sidecar 日志中是否有 `Accel enabled: injecting WINGS_ENGINE_PATCH_OPTIONS` 日志

## 自定义 Accel 包

如需打包自定义加速插件：

1. 将 `.whl` 文件放入 `wings-accel/wings_engine_patch/`
2. 修改 `wings_engine_patch/install.sh` 中的安装逻辑
3. 更新 `supported_features.json` 中的兼容性声明
4. 重新构建镜像：`bash build-accel-image.sh v2.0.0`
