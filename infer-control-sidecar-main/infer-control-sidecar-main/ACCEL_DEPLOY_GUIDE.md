# Wings-Accel 部署指南

本指南说明如何在 Linux 机器上构建、导入和部署 wings-accel 加速功能。

## 前提条件

- Linux 机器 IP: 90.90.161.168
- 已安装 Docker
- 已安装 containerd 和 ctr
- 已安装 kubectl
- 已有 wings-infer 项目代码

## 部署步骤

### 1. 同步代码到 Linux 机器

在 Windows 机器上，使用以下命令同步代码到 Linux 机器：

```bash
# 使用 scp 同步代码（需要密码或密钥）
scp -r wings-accel k8s/deployment.yaml build-accel-image.sh import-accel-image.sh deploy-with-accel.sh debug-accel.sh g00050869@90.90.161.168:/home/g00050869/wings-Infer-demo/
```

或者使用 rsync：

```bash
rsync -avz --progress wings-accel/ k8s/deployment.yaml build-accel-image.sh import-accel-image.sh deploy-with-accel.sh debug-accel.sh g00050869@90.90.161.168:/home/g00050869/wings-Infer-demo/
```

### 2. 登录到 Linux 机器

```bash
ssh g00050869@90.90.161.168
cd /home/g00050869/wings-Infer-demo
```

### 3. 构建 accel 镜像

```bash
# 给脚本添加执行权限
chmod +x build-accel-image.sh

# 构建镜像
bash build-accel-image.sh
```

### 4. 导入镜像到 containerd

```bash
# 给脚本添加执行权限
chmod +x import-accel-image.sh

# 导入镜像
bash import-accel-image.sh
```

### 5. 部署 wings-infer（启用加速）

```bash
# 给脚本添加执行权限
chmod +x deploy-with-accel.sh

# 部署
bash deploy-with-accel.sh
```

### 6. 调试加速功能

```bash
# 给脚本添加执行权限
chmod +x debug-accel.sh

# 调试
bash debug-accel.sh
```

## 功能说明

### 加速开关

在 [k8s/deployment.yaml](k8s/deployment.yaml) 中，有两个地方设置了 `ENABLE_ACCEL` 环境变量：

1. **wings-infer 容器** (第 75-76 行):
   ```yaml
   - name: ENABLE_ACCEL
     value: "true"  # 设置为 true 启用加速，false 禁用
   ```

2. **vllm-engine 容器** (第 142-143 行):
   ```yaml
   - name: ENABLE_ACCEL
     value: "true"  # 设置为 true 启用加速，false 禁用
   ```

要禁用加速功能，将这两个地方的 `value` 改为 `"false"`。

### 工作原理

1. **InitContainer**: `accel-init` 容器在启动时将 `wings-accel` 镜像中的所有文件拷贝到共享卷 `accel-volume`

2. **Engine 容器**: `vllm-engine` 容器在启动时检查 `ENABLE_ACCEL` 环境变量：
   - 如果为 `true`，则执行 `/accel-volume/install.sh` 安装加速包
   - 如果为 `false`，则跳过安装

3. **Side-car 模式**: 使用 initContainer + 共享卷的方式实现 side-car 模式，将 accel 文件注入到 engine 容器中

### Accel 文件结构

```
wings-accel/
├── Dockerfile                          # 镜像构建文件
├── install.sh                          # 主安装脚本
├── supported_features.json             # 支持的特性配置
├── lmcache/                            # lmcache 目录
└── wings_engine_patch/                 # engine 补丁目录
    ├── install.sh                      # 补丁安装脚本
    ├── wings_engine_patch-1.0.0-py3-none-any.whl  # 补丁包
    └── wrapt-2.0.1-py3-none-any.whl    # 依赖包
```

## 常见问题

### 1. 镜像构建失败

检查 Dockerfile 是否存在，以及是否有足够的权限：

```bash
ls -la wings-accel/Dockerfile
```

### 2. 镜像导入失败

检查 containerd 是否正常运行：

```bash
systemctl status containerd
ctr -n k8s.io images ls
```

### 3. Pod 启动失败

查看 pod 状态和日志：

```bash
kubectl get pods -l app=wings-infer
kubectl describe pod <pod-name>
kubectl logs <pod-name> -c vllm-engine
```

### 4. 加速包未安装

检查 accel-volume 内容和安装日志：

```bash
kubectl exec <pod-name> -c vllm-engine -- ls -la /accel-volume
kubectl logs <pod-name> -c vllm-engine | grep -i accel
```

## 手动调试命令

### 查看 pod 状态

```bash
kubectl get pods -l app=wings-infer
kubectl describe pod <pod-name>
```

### 查看日志

```bash
# wings-infer 容器日志
kubectl logs <pod-name> -c wings-infer -f

# vllm-engine 容器日志
kubectl logs <pod-name> -c vllm-engine -f

# initContainer 日志
kubectl logs <pod-name> -c accel-init
```

### 进入容器调试

```bash
# 进入 vllm-engine 容器
kubectl exec -it <pod-name> -c vllm-engine -- /bin/bash

# 在容器内检查
ls -la /accel-volume
cat /accel-volume/install.sh
pip list | grep -E "wings_engine_patch|wrapt"
```

### 重新部署

```bash
# 删除现有部署
kubectl delete deployment wings-infer

# 重新部署
kubectl apply -f k8s/deployment.yaml
```

## 下一步

部署完成后，可以：

1. 测试推理功能是否正常
2. 检查加速包是否生效
3. 监控性能指标
4. 根据需要调整配置

## 联系方式

如有问题，请联系开发团队。
