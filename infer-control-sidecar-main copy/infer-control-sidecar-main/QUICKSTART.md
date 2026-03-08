# Wings-Infer 快速开始

## 🚀 5分钟快速部署

### 方式一：使用 Docker Compose (本地测试)

```bash
# 1. 克隆或进入项目目录
cd wings-Infer-demo

# 2. 启动服务
docker-compose up -d

# 3. 查看日志
docker-compose logs -f wings-infer

# 4. 测试API
bash test_api.sh http://localhost:9000

# 5. 停止服务
docker-compose down
```

### 方式二：使用 Kubernetes (生产环境)

```bash
# 1. 构建镜像
./deploy.sh build

# 2. 部署到K8s
./deploy.sh deploy

# 3. 查看状态
./deploy.sh status

# 4. 端口转发 (如果LoadBalancer不可用)
./deploy.sh forward

# 5. 测试API
./deploy.sh test

# 6. 查看日志
./deploy.sh logs wings-infer

# 7. 删除部署
./deploy.sh delete
```

## 📋 前置条件

### Docker Compose 方式
- Docker
- Docker Compose
- (可选) NVIDIA Docker Runtime (用于GPU加速)

### Kubernetes 方式
- Kubernetes集群
- kubectl
- Docker
- 模型存储 (NFS/对象存储/PV)

## 🔧 配置说明

### 修改引擎类型

编辑 `k8s/deployment.yaml` 或 `docker-compose.yml`：

```yaml
# vLLM
ENGINE_TYPE=vllm

# SGLang
ENGINE_TYPE=sglang
```

### 修改模型配置

```yaml
MODEL_NAME=your-model-name
MODEL_PATH=/models
TP_SIZE=1  # Tensor并行大小
MAX_MODEL_LEN=4096
```

### 准备模型

1. 下载模型文件
2. 放到 `./models` 目录 (Docker Compose)
3. 或配置K8s PVC (Kubernetes)

## 🎯 核心功能演示

### 1. 自动启动流程

```
Wings-Infer容器启动
    ↓
构建引擎启动命令
    ↓
写入共享卷
    ↓
引擎容器读取命令
    ↓
自动启动推理服务
    ↓
状态同步
    ↓
准备就绪 ✓
```

### 2. API请求流程

```
客户端请求
    ↓
Wings-Infer API层
    ↓
请求转发服务
    ↓
引擎推理
    ↓
响应返回
    ↓
客户端
```

## 📝 API使用示例

### 健康检查

```bash
curl http://localhost:9000/health
```

响应：
```json
{
  "status": "healthy",
  "engine_ready": true,
  "proxy_healthy": true
}
```

### 文本补全

```bash
curl -X POST http://localhost:9000/v1/completions \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Once upon a time",
    "max_tokens": 50
  }'
```

### 聊天补全

```bash
curl -X POST http://localhost:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "user", "content": "你好"}
    ]
  }'
```

## 🔍 监控和调试

### 查看日志

```bash
# Docker Compose
docker-compose logs -f wings-infer
docker-compose logs -f vllm-engine

# Kubernetes
kubectl logs -f deployment/wings-infer -c wings-infer
kubectl logs -f deployment/wings-infer -c vllm-engine
```

### 检查共享卷

```bash
# 进入容器查看
kubectl exec -it <pod-name> -c wings-infer -- ls -la /shared-volume

# 应该看到:
# start_command.sh    # 启动命令
# wings_status.txt    # wings状态
# engine_status.txt   # 引擎状态
```

### 检查资源使用

```bash
# Kubernetes
kubectl top pods -l app=wings-infer

# Docker
docker stats
```

## ⚠️ 常见问题

### 1. 引擎容器无法启动

**原因**: 模型路径错误或模型文件不存在

**解决**:
```bash
# 检查模型路径
kubectl exec -it <pod-name> -c vllm-engine -- ls -la /models

# 检查PVC
kubectl get pvc model-pvc
```

### 2. 健康检查失败

**原因**: 引擎服务未就绪

**解决**:
```bash
# 查看引擎日志
kubectl logs <pod-name> -c vllm-engine

# 检查引擎状态
curl http://localhost:9000/engine/status
```

### 3. 请求超时

**原因**: 模型加载时间过长或资源不足

**解决**:
- 增加资源限制
- 增加健康检查超时时间
- 使用更小的模型

## 🎓 架构理解

### Sidecar模式优势

1. **自动化**: 无需手动启动引擎
2. **解耦**: 控制层和引擎层分离
3. **灵活性**: 易于切换引擎类型
4. **可扩展**: 支持多引擎、多模型

### 共享卷通信

```
+----------------+      +----------------+
|  Wings-Infer   |      |   vLLM Engine  |
|                |      |                |
|  写入命令 ---> |      | <--- 读取命令 |
|                |      |                |
|  读取状态 <--- |      | ---> 写入状态 |
|                |      |                |
+----------------+      +----------------+
        |                       |
        +-----------+-----------+
                    |
              /shared-volume/
```

## 📚 更多资源

- 完整文档: [README.md](README.md)
- API文档: http://localhost:9000/docs
- vLLM文档: https://docs.vllm.ai/
- SGLang文档: https://lmsys.org/blog/2023-12-21-sglang/

## 💡 下一步

1. 根据实际需求调整资源配置
2. 配置模型持久化存储
3. 设置监控和告警
4. 配置负载均衡和自动扩缩容
5. 集成到现有系统

## 🤝 支持

如有问题，请查看：
- README.md 故障排查章节
- 日志文件
- GitHub Issues