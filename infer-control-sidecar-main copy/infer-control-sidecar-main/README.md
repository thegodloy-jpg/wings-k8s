# Wings-Infer K8s Demo

Wings-Infer 是一个基于Kubernetes的统一推理服务管理平台，通过sidecar模式自动管理vLLM和SGLang推理引擎的启动和请求转发。

## 项目结构

```
wings-Infer-demo/
├── k8s/                           # Kubernetes部署配置
│   ├── deployment.yaml            # vLLM引擎部署配置
│   ├── deployment-sglang.yaml     # SGLang引擎部署配置
│   └── service.yaml               # 服务暴露配置
├── backend/                       # Wings-Infer应用代码
│   ├── requirements.txt           # Python依赖
│   ├── app/
│   │   ├── main.py               # 应用入口
│   │   ├── api/
│   │   │   └── routes.py         # API路由
│   │   ├── services/
│   │   │   ├── command_builder.py    # 引擎命令构建
│   │   │   ├── engine_manager.py     # 引擎管理器
│   │   │   └── proxy_service.py      # 请求转发服务
│   │   ├── config/
│   │   │   └── settings.py      # 配置管理
│   │   └── utils/
│   │       ├── file_utils.py    # 文件操作工具
│   │       └── http_client.py   # HTTP客户端
└── Dockerfile                    # Wings-Infer镜像构建配置
```

## 核心特性

### 1. Sidecar自动启动
- **控制容器** (wings-infer): 负责API服务、命令拼接、引擎管理
- **引擎容器** (vllm/sglang): 通过共享卷读取启动命令并执行
- 无需手动curl启动，通过共享卷实现自动化

### 2. 统一API接口
- `/health` - 健康检查
- `/v1/completions` - 文本补全
- `/v1/chat/completions` - 聊天补全
- `/generate` - 文本生成
- `/engine/status` - 引擎状态查询

### 3. 灵活的引擎切换
- 支持vLLM和SGLang两种推理引擎
- 通过环境变量轻松切换
- 统一的命令拼接逻辑

## 部署指南

### 前置要求

1. Kubernetes集群 (v1.20+)
2. kubectl命令行工具
3. Docker (用于构建镜像)
4. 存储卷支持 (用于模型持久化)

### 1. 构建Wings-Infer镜像

```bash
# 在项目根目录执行
docker build -t wings-infer:latest .
```

### 2. 准备模型数据

将模型文件放到可访问的存储位置（如NFS、对象存储等），并创建PVC。

如果使用本地测试，可以创建临时PVC：

```bash
kubectl apply -f k8s/deployment.yaml
# 注意：需要根据实际情况调整 storageClassName
```

### 3. 部署到Kubernetes

#### 使用vLLM引擎:

```bash
# 部署应用
kubectl apply -f k8s/deployment.yaml

# 部署服务
kubectl apply -f k8s/service.yaml
```

#### 使用SGLang引擎:

```bash
# 部署应用
kubectl apply -f k8s/deployment-sglang.yaml

# 部署服务
kubectl apply -f k8s/service.yaml
```

### 4. 查看部署状态

```bash
# 查看Pod状态
kubectl get pods -l app=wings-infer

# 查看日志
kubectl logs -f deployment/wings-infer -c wings-infer
kubectl logs -f deployment/wings-infer -c vllm-engine

# 查看服务
kubectl get svc wings-infer-service
```

### 5. 访问服务

获取服务外部IP：

```bash
kubectl get svc wings-infer-service
```

然后访问 `http://<EXTERNAL-IP>:9000`

## API使用示例

### 健康检查

```bash
curl http://<EXTERNAL-IP>:9000/health
```

### 文本补全

```bash
curl -X POST http://<EXTERNAL-IP>:9000/v1/completions \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Once upon a time",
    "max_tokens": 50,
    "temperature": 0.7
  }'
```

### 聊天补全

```bash
curl -X POST http://<EXTERNAL-IP>:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "user", "content": "Hello, how are you?"}
    ],
    "max_tokens": 100,
    "temperature": 0.7
  }'
```

### 文本生成

```bash
curl -X POST http://<EXTERNAL-IP>:9000/generate \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "The future of AI is",
    "max_tokens": 100,
    "temperature": 0.8
  }'
```

## 配置说明

### 环境变量

在 `k8s/deployment.yaml` 中可以配置以下环境变量：

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `ENGINE_TYPE` | `vllm` | 引擎类型：vllm 或 sglang |
| `ENGINE_PORT` | `8000` | 引擎服务端口 |
| `WINGS_PORT` | `9000` | Wings-Infer服务端口 |
| `MODEL_NAME` | `meta-llama/Llama-2-7b-chat-hf` | 模型名称 |
| `MODEL_PATH` | `/models` | 模型路径 |
| `TP_SIZE` | `1` | Tensor并行大小 |
| `MAX_MODEL_LEN` | `4096` | 最大序列长度 |

### 资源配置

根据实际需求调整资源配置：

```yaml
resources:
  requests:
    cpu: "2"          # 根据GPU数量调整
    memory: "8Gi"     # 根据模型大小调整
  limits:
    cpu: "8"
    memory: "32Gi"
```

## 工作原理

### 启动流程

1. **Wings-Infer容器启动**
   - 读取配置和环境变量
   - 构建引擎启动命令
   - 将命令写入共享卷 (`/shared-volume/start_command.sh`)
   - 写入就绪状态到共享卷

2. **引擎容器启动**
   - 等待共享卷中出现启动命令文件
   - 读取并执行启动命令
   - 启动推理服务
   - 写入运行状态到共享卷 (`engine_status.txt`)

3. **Wings-Infer监控**
   - 定期检查引擎状态
   - 引擎就绪后开始接受请求
   - 转发请求到引擎服务

### 共享卷机制

- **emptyDir** 用于临时文件传输
- 支持内存模式 (`medium: Memory`) 提高性能
- 两个容器通过共享目录通信

### 请求转发

1. 客户端请求 → Wings-Infer API
2. Wings-Infer转发 → 引擎服务
3. 引擎响应 → Wings-Infer → 客户端

## 扩展性设计

### 添加新引擎

1. 在 `backend/app/services/command_builder.py` 中添加新引擎的命令构建方法
2. 在 `k8s/` 中创建新的部署配置文件
3. 更新配置支持新的引擎类型

### 添加新API

1. 在 `backend/app/api/routes.py` 中添加新路由
2. 在 `backend/app/services/proxy_service.py` 中添加转发逻辑
3. 更新API文档

### 多模型支持

可以通过创建多个Deployment来支持多个模型：

```bash
kubectl apply -f k8s/deployment-model1.yaml
kubectl apply -f k8s/deployment-model2.yaml
```

## 故障排查

### Pod无法启动

```bash
# 查看Pod事件
kubectl describe pod <pod-name>

# 查看容器日志
kubectl logs <pod-name> -c wings-infer
kubectl logs <pod-name> -c vllm-engine
```

### 引擎启动失败

1. 检查模型路径是否正确
2. 检查GPU资源是否充足
3. 查看引擎容器日志
4. 检查共享卷权限

### 请求失败

1. 检查引擎状态：`curl http://<IP>:9000/health`
2. 查看Wings-Infer日志
3. 确认引擎服务端口配置正确

## 性能优化

1. **使用内存共享卷**：已配置 `medium: Memory`
2. **调整资源配置**：根据实际负载调整CPU和内存
3. **启用GPU加速**：确保节点有GPU资源并正确配置
4. **模型量化**：使用量化模型减少内存占用

## 许可证

MIT License

## 贡献

欢迎提交Issue和Pull Request！