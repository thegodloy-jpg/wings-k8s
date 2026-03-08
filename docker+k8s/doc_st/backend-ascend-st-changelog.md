# backend-ascend-st 变更记录

**创建日期**: 2026-03-02  
**基于版本**: `backend-20260228`  
**目标**: 华为昇腾 910B2C (CANN 8.5.0) 场景下验证 vllm-ascend 和 MindIE 引擎  
**验证服务器**: `7.6.52.110`（12x Ascend 910B2C，NPU 0-3 空闲）

---

## 一、新版本位置

| 类型 | 路径 |
|------|------|
| 新版 backend 代码 | `infer-control-sidecar-main/backend-ascend-st/` |
| Dockerfile | `infer-control-sidecar-main/Dockerfile.sidecar-ascend-st` |
| docker-compose | `infer-control-sidecar-main/docker-compose-ascend-st.yml` |

---

## 二、变更内容

### 变更 1：修复 `vllm_ascend` 的 `build_start_script`

**文件**: `backend-ascend-st/app/engines/vllm_adapter.py`

**问题根因**: `build_start_script` 在原版 `backend-20260228` 中直接调用 `build_start_command`（仅生成 python3 命令），
未包含 Ascend 必需的 CANN/ATB 环境初始化。结果写入 `start_command.sh` 的脚本缺少 `source set_env.sh`，
导致 `vllm-ascend` 容器因找不到 Ascend runtime 库而启动失败。

**修改前**:
```python
def build_start_script(params: Dict[str, Any]) -> str:
    return "exec " + build_start_command(params) + "\n"
```

**修改后**:
```python
def build_start_script(params: Dict[str, Any]) -> str:
    engine = params.get("engine", "vllm")
    cmd = _build_vllm_cmd_parts(params)

    if engine == "vllm_ascend":
        config_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config"
        )
        env_script = os.path.join(config_dir, "set_vllm_ascend_env.sh")
        if os.path.exists(env_script):
            env_block = f"source {env_script}\n"
        else:
            # inline fallback
            env_block = (
                "source /usr/local/Ascend/ascend-toolkit/set_env.sh\n"
                "source /usr/local/Ascend/nnal/atb/set_env.sh\n"
            )
        return env_block + f"exec {cmd}\n"

    return f"exec {cmd}\n"
```

**生成的 `start_command.sh` 示例（vllm_ascend）**:
```bash
#!/usr/bin/env bash
set -euo pipefail
source /app/app/config/set_vllm_ascend_env.sh
exec python3 -m vllm.entrypoints.openai.api_server \
  --model /models/DeepSeek-R1-Distill-Qwen-1.5B \
  --served-model-name DeepSeek-R1-Distill-Qwen-1.5B \
  --host 0.0.0.0 --port 17000 \
  --tensor-parallel-size 1 \
  --max-model-len 5120 \
  --gpu-memory-utilization 0.9 \
  --trust-remote-code
```

---

### 变更 2：修复 `_build_base_env_commands` 路径引用

**文件**: `backend-ascend-st/app/engines/vllm_adapter.py`

**问题根因**: 原代码引用 `{root}/wings/config/set_vllm_ascend_env.sh`，
该路径在 sidecar 容器内不存在（wings 项目并未被打包进去）。

**修改**: 改为优先查找 `app/config/set_vllm_ascend_env.sh`（打包在镜像内），
找不到时 fallback 为 inline 的系统路径。

---

### 变更 3：新增 `app/config/set_vllm_ascend_env.sh`

**文件**: `backend-ascend-st/app/config/set_vllm_ascend_env.sh`

```bash
#!/usr/bin/env bash
source /usr/local/Ascend/ascend-toolkit/set_env.sh
source /usr/local/Ascend/nnal/atb/set_env.sh
```

路径依据 `quay.io/ascend/vllm-ascend:v0.14.0rc1` 镜像验证，两个 set_env.sh 均已确认存在。

---

## 三、部署说明（vllm-ascend 验证）

### 前提条件

服务器 `7.6.52.110` 上已有：
- `quay.io/ascend/vllm-ascend:v0.14.0rc1`（18GB）
- `swr.cn-south-1.myhuaweicloud.com/ascendhub/mindie:2.2.RC1-800I-A2-py311-openeuler24.03-lts`（23GB）
- 模型：`/mnt/cephfs/models/DeepSeek-R1-Distill-Qwen-1.5B`
- NPU 0-3 空闲（NPU 4-11 已被占用，**勿触碰**）

### 步骤 1：构建 wings-infer 镜像

```bash
# 上传代码
scp -r infer-control-sidecar-main/ root@7.6.52.110:/tmp/wings-build/

# 在服务器上构建（k3s-verify 规范：tag 必须含 zhanghui 字段）
ssh root@7.6.52.110 "
  cd /tmp/wings-build/infer-control-sidecar-main
  docker build -f Dockerfile.sidecar-ascend-st -t wings-infer:zhanghui-ascend-st .
  echo 'Build done'
"

# 导入 k3s containerd（k3s-verify 专用）
ssh root@7.6.52.110 "
  docker save wings-infer:zhanghui-ascend-st | docker exec -i k3s-verify ctr images import -
  docker exec k3s-verify ctr images ls | grep zhanghui
"
```

### 步骤 2：启动 docker-compose

```bash
ssh root@7.6.52.110 "
  cd /tmp/wings-build/infer-control-sidecar-main
  docker-compose -f docker-compose-ascend-st.yml up -d
  sleep 5
  docker-compose -f docker-compose-ascend-st.yml ps
"
```

### 步骤 3：监控日志

```bash
# wings-infer 日志（确认 start_command.sh 生成）
ssh root@7.6.52.110 "docker logs wings-infer-ascend-st -f --tail=30"

# 引擎日志（确认模型加载）
ssh root@7.6.52.110 "docker logs vllm-ascend-engine-st -f --tail=50"
```

### 步骤 4：验证测试

```bash
# 健康检查
curl http://7.6.52.110:19000/health

# 模型列表
curl http://7.6.52.110:18000/v1/models

# 推理测试
curl -X POST http://7.6.52.110:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "DeepSeek-R1-Distill-Qwen-1.5B",
    "messages": [{"role": "user", "content": "hello"}],
    "max_tokens": 30
  }'
```

### 清理

```bash
ssh root@7.6.52.110 "
  cd /tmp/wings-build/infer-control-sidecar-main
  docker-compose -f docker-compose-ascend-st.yml down
"
```

---

### 变更 4（2026-03-02）：k3s-verify 镜像 tag 规范 — 必须含 zhanghui 字段

**背景**：与 SGLang 验证（`wings-infer:zhanghui-20260228`）保持一致，k3s-verify 验证时
所有自建镜像的 tag 必须包含 `zhanghui` 字段，便于区分验证镜像与生产镜像。

**涉及文件及修改**：

| 文件 | 修改前 | 修改后 |
|------|--------|--------|
| `docker-compose-ascend-st.yml` | `wings-infer:ascend-st` | `wings-infer:zhanghui-ascend-st` |
| `Dockerfile.sidecar-ascend-st` | 注释无构建命令 | 补充构建命令，tag 为 `wings-infer:zhanghui-ascend-st` |
| `k8s/deployment-vllm-ascend.verify.yaml` | *(新建)* | `wings-infer:zhanghui-ascend-st` |
| `k8s/service-vllm-ascend.verify.yaml` | *(新建)* | NodePort 31810/31910（与 sglang 31800/31900 错开）|

**k3s-verify 部署命令**：
```bash
# 确认 k3s-verify 运行
ssh root@7.6.52.110 "docker start k3s-verify 2>/dev/null; docker ps | grep k3s-verify"

# 确认 namespace
ssh root@7.6.52.110 "docker exec k3s-verify kubectl get namespace wings-verify || \
  docker exec k3s-verify kubectl create namespace wings-verify"

# 上传并应用 YAML
scp k8s/deployment-vllm-ascend.verify.yaml \
    k8s/service-vllm-ascend.verify.yaml \
    root@7.6.52.110:/tmp/
ssh root@7.6.52.110 "
  docker cp /tmp/deployment-vllm-ascend.verify.yaml k3s-verify:/tmp/
  docker cp /tmp/service-vllm-ascend.verify.yaml k3s-verify:/tmp/
  docker exec k3s-verify kubectl apply -f /tmp/deployment-vllm-ascend.verify.yaml
  docker exec k3s-verify kubectl apply -f /tmp/service-vllm-ascend.verify.yaml
"

# 监控 Pod 启动（等待 2/2 Running）
ssh root@7.6.52.110 "docker exec k3s-verify kubectl -n wings-verify get pods -w"
```

---

## 四、环境信息

| 项目 | 值 |
|------|-----|
| 服务器 | `7.6.52.110`（hostname: 910b-47） |
| NPU 型号 | Ascend 910B2C × 12，HBM 64GB each |
| 验证用 NPU | NPU 0（`ASCEND_VISIBLE_DEVICES=0`） |
| 占用中 NPU | NPU 4-11（**严禁触碰**） |
| CANN 版本 | 8.5.0（镜像内） |
| vllm-ascend | `quay.io/ascend/vllm-ascend:v0.14.0rc1` |
| MindIE | `swr.cn-south-1.myhuaweicloud.com/ascendhub/mindie:2.2.RC1-800I-A2-py311-openeuler24.03-lts` |
| 模型路径 | `/mnt/cephfs/models/DeepSeek-R1-Distill-Qwen-1.5B` |
| 端口规划 | engine=17000, proxy=18000, health=19000 |
