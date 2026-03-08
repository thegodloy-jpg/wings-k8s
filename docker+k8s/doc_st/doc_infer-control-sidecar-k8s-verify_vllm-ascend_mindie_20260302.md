# infer-control-sidecar-main 昇腾单机验证实施细节（vllm_ascend -> mindie）

**文档日期**: 2026-03-02  
**来源需求**: `docker+k8s/doc_st/requires.txt`  
**参考流程**: `docker+k8s/doc_infer-control-sidecar-k8s-verify_sglang_20260228.md`

---

## 1. 任务目标

在昇腾单机场景（模型 `DeepSeek-R1-Distill-Qwen-1.5B`）完成 `infer-control-sidecar-main` 的双引擎验证，顺序固定：

1. 先验证 `vllm_ascend`
2. 再验证 `mindie`

交付物要求：

- 两轮验证均形成可复现实操记录（命令、日志关键行、接口返回）
- 明确两引擎差异点（环境变量、配置写入方式）
- 记录失败回滚和复验路径

---

## 2. 基线结论（实施前必须确认）

### 2.1 代码基线选择

本项目当前存在两套后端：

- `backend`（当前主目录）
- `backend-20260228`（多引擎增强版）

本次任务必须使用 `backend-20260228` 产出的 sidecar 镜像，原因：

- `backend/app/core/start_args_compat.py` 仅允许 `vllm`
- `backend-20260228/app/core/start_args_compat.py` 支持 `vllm / vllm_ascend / sglang / mindie`
- `backend-20260228` 包含 `mindie_adapter.py`

### 2.2 引擎差异（本任务核心）

- `vllm_ascend`：主要是命令启动 + 昇腾环境变量
- `mindie`：除环境变量外，还需要在启动时写入/替换 Mindie `config.json`

说明：`mindie_adapter.py` 会把完整 `config.json` 内嵌到 `start_command.sh`，由引擎容器启动时写入：
`/usr/local/Ascend/mindie/latest/mindie-service/conf/config.json`

---

## 3. 变量约定（先统一）

以下变量用于命令模板，按现场替换：

```bash
# 本地路径
ROOT="/workspace/wings-k8s"
PRJ="$ROOT/infer-control-sidecar-main/infer-control-sidecar-main"

# 远端环境
REMOTE="root@7.6.52.110"
NS="wings-verify"
K3S_CONTAINER="k3s-verify"   # 若 kubectl 直接可用，此变量可忽略

# 镜像
SIDECAR_IMG="wings-infer:zhanghui-20260302"
VLLM_ASCEND_IMG="<待确认-vllm-ascend镜像>"
MINDIE_IMG="<待确认-mindie镜像>"

# 模型
MODEL_NAME="DeepSeek-R1-Distill-Qwen-1.5B"
MODEL_HOST_PATH="/mnt/models/DeepSeek-R1-Distill-Qwen-1.5B"
MODEL_IN_POD="/models/DeepSeek-R1-Distill-Qwen-1.5B"
```

如果远端是容器化 k3s，文档内 `kubectl ...` 统一替换为：

```bash
docker exec ${K3S_CONTAINER} kubectl ...
```

---

## 4. 前置检查

### 4.1 远端环境可用

```bash
ssh $REMOTE "hostname && docker ps"
ssh $REMOTE "ls -ld ${MODEL_HOST_PATH}"
ssh $REMOTE "npu-smi info || true"
```

### 4.2 Namespace

```bash
ssh $REMOTE "kubectl get ns ${NS} >/dev/null 2>&1 || kubectl create ns ${NS}"
```

### 4.3 验证用脚本来源

昇腾环境脚本从以下目录取：

- `$ROOT/wings/wings/config/set_vllm_ascend_env.sh`
- `$ROOT/wings/wings/config/set_mindie_single_env.sh`

后续通过 ConfigMap 挂载到引擎容器。

---

## 5. 构建与导入镜像

### 5.1 构建 sidecar（必须使用 20260228 Dockerfile）

在项目根目录执行：

```bash
cd "$PRJ"
docker build -f Dockerfile.sidecar-20260228 -t ${SIDECAR_IMG} .
```

### 5.2 导入到 k3s containerd（如需）

```bash
docker save ${SIDECAR_IMG} | docker exec -i ${K3S_CONTAINER} ctr -n k8s.io images import -

docker save ${VLLM_ASCEND_IMG} | docker exec -i ${K3S_CONTAINER} ctr -n k8s.io images import -
docker save ${MINDIE_IMG}      | docker exec -i ${K3S_CONTAINER} ctr -n k8s.io images import -
```

---

## 6. 统一准备：Ascend 环境脚本 ConfigMap

```bash
scp "$ROOT/wings/wings/config/set_vllm_ascend_env.sh" "$REMOTE:/tmp/set_vllm_ascend_env.sh"
scp "$ROOT/wings/wings/config/set_mindie_single_env.sh" "$REMOTE:/tmp/set_mindie_single_env.sh"

ssh $REMOTE "kubectl -n ${NS} delete configmap ascend-env-scripts --ignore-not-found"
ssh $REMOTE "kubectl -n ${NS} create configmap ascend-env-scripts \
  --from-file=set_vllm_ascend_env.sh=/tmp/set_vllm_ascend_env.sh \
  --from-file=set_mindie_single_env.sh=/tmp/set_mindie_single_env.sh \
  --dry-run=client -o yaml | kubectl apply -f -"
```

挂载方式（两套 Deployment 都要有）：

```yaml
volumes:
  - name: ascend-env-scripts
    configMap:
      name: ascend-env-scripts
      defaultMode: 0755
```

```yaml
volumeMounts:
  - name: ascend-env-scripts
    mountPath: /opt/wings-env
```

---

## 7. 阶段A：vllm_ascend 验证（先执行）

### 7.1 部署文件要点（`deployment-vllm-ascend.verify.yaml`）

`wings-infer` 容器（控制面）：

```yaml
env:
  - name: ENGINE
    value: "vllm_ascend"
  - name: ENGINE_PORT
    value: "17000"
  - name: PORT
    value: "18000"
  - name: HEALTH_PORT
    value: "19000"
  - name: MODEL_NAME
    value: "DeepSeek-R1-Distill-Qwen-1.5B"
  - name: MODEL_PATH
    value: "/models/DeepSeek-R1-Distill-Qwen-1.5B"
  - name: WINGS_SKIP_PID_CHECK
    value: "true"
```

引擎容器（`vllm-ascend-engine`）关键启动逻辑：

```yaml
command: ["/bin/bash", "-c"]
args:
  - |
    source /opt/wings-env/set_vllm_ascend_env.sh

    # 按现场 NPU 拓扑设置（示例：使用 0 号卡）
    export ASCEND_RT_VISIBLE_DEVICES=${ASCEND_RT_VISIBLE_DEVICES:-0}

    echo "Waiting for start command from wings-infer..."
    while [ ! -f /shared-volume/start_command.sh ]; do sleep 1; done

    echo "Start command:"
    cat /shared-volume/start_command.sh

    cd /shared-volume
    bash start_command.sh &
    ENGINE_PID=$!

    while ! nc -z 127.0.0.1 17000 2>/dev/null; do
      sleep 2
      kill -0 $ENGINE_PID 2>/dev/null || exit 1
    done

    wait $ENGINE_PID
```

Service 建议：`NodePort 31810 -> 18000`，`31910 -> 19000`。

### 7.2 执行

```bash
ssh $REMOTE "kubectl -n ${NS} apply -f /tmp/deployment-vllm-ascend.verify.yaml"
ssh $REMOTE "kubectl -n ${NS} apply -f /tmp/service-vllm-ascend.verify.yaml"
ssh $REMOTE "kubectl -n ${NS} get pods -w"
```

### 7.3 验证点

1. `wings-infer` 日志里引擎识别为 `vllm_ascend`
2. `/shared-volume/start_command.sh` 含 `python3 -m vllm.entrypoints.openai.api_server`
3. 引擎容器端口 `17000` 监听成功
4. 健康接口：`/health` 返回 `backend_ok=true`
5. 推理接口：`/v1/chat/completions` 返回 200 且有 `choices`

命令模板：

```bash
POD=$(ssh $REMOTE "kubectl -n ${NS} get pod -l app=wings-infer-vllm-ascend -o jsonpath='{.items[0].metadata.name}'")

ssh $REMOTE "kubectl -n ${NS} logs ${POD} -c wings-infer --tail=120"
ssh $REMOTE "kubectl -n ${NS} logs ${POD} -c vllm-ascend-engine --tail=120"

ssh $REMOTE "kubectl -n ${NS} exec ${POD} -c wings-infer -- curl -s http://127.0.0.1:19000/health"
ssh $REMOTE "kubectl -n ${NS} exec ${POD} -c wings-infer -- curl -s http://127.0.0.1:18000/v1/models"
```

---

## 8. 阶段B：mindie 验证（第二步执行）

先清理阶段A资源，再部署 mindie：

```bash
ssh $REMOTE "kubectl -n ${NS} delete deployment wings-infer-vllm-ascend --ignore-not-found"
ssh $REMOTE "kubectl -n ${NS} delete service wings-infer-vllm-ascend-service --ignore-not-found"
```

### 8.1 部署文件要点（`deployment-mindie.verify.yaml`）

`wings-infer` 容器（控制面）：

```yaml
env:
  - name: ENGINE
    value: "mindie"
  - name: ENGINE_PORT
    value: "17000"
  - name: PORT
    value: "18000"
  - name: HEALTH_PORT
    value: "19000"
  - name: MODEL_NAME
    value: "DeepSeek-R1-Distill-Qwen-1.5B"
  - name: MODEL_PATH
    value: "/models/DeepSeek-R1-Distill-Qwen-1.5B"
  - name: WINGS_SKIP_PID_CHECK
    value: "true"
```

引擎容器（`mindie-engine`）关键启动逻辑：

```yaml
command: ["/bin/bash", "-c"]
args:
  - |
    source /opt/wings-env/set_mindie_single_env.sh

    # 按现场 NPU 拓扑设置（示例）
    export ASCEND_RT_VISIBLE_DEVICES=${ASCEND_RT_VISIBLE_DEVICES:-0}

    echo "Waiting for start command from wings-infer..."
    while [ ! -f /shared-volume/start_command.sh ]; do sleep 1; done

    echo "Start command:"
    sed -n '1,200p' /shared-volume/start_command.sh

    cd /shared-volume
    bash start_command.sh &
    ENGINE_PID=$!

    while ! nc -z 127.0.0.1 17000 2>/dev/null; do
      sleep 2
      kill -0 $ENGINE_PID 2>/dev/null || exit 1
    done

    wait $ENGINE_PID
```

Service 建议：`NodePort 31820 -> 18000`，`31920 -> 19000`。

### 8.2 执行

```bash
ssh $REMOTE "kubectl -n ${NS} apply -f /tmp/deployment-mindie.verify.yaml"
ssh $REMOTE "kubectl -n ${NS} apply -f /tmp/service-mindie.verify.yaml"
ssh $REMOTE "kubectl -n ${NS} get pods -w"
```

### 8.3 验证点（必须覆盖“配置替换”）

1. `start_command.sh` 中包含 `cat > '/usr/local/Ascend/mindie/latest/mindie-service/conf/config.json'`
2. mindie 容器内实际 `config.json` 被写入且参数正确
3. `mindieservice_daemon` 进程存活并监听 `17000`
4. `/health` 与 `/v1/chat/completions` 成功

命令模板：

```bash
POD=$(ssh $REMOTE "kubectl -n ${NS} get pod -l app=wings-infer-mindie -o jsonpath='{.items[0].metadata.name}'")

ssh $REMOTE "kubectl -n ${NS} exec ${POD} -c mindie-engine -- \
  bash -lc \"sed -n '1,120p' /shared-volume/start_command.sh\""

ssh $REMOTE "kubectl -n ${NS} exec ${POD} -c mindie-engine -- \
  bash -lc \"cat /usr/local/Ascend/mindie/latest/mindie-service/conf/config.json\""

ssh $REMOTE "kubectl -n ${NS} exec ${POD} -c wings-infer -- curl -s http://127.0.0.1:19000/health"
ssh $REMOTE "kubectl -n ${NS} exec ${POD} -c wings-infer -- curl -s http://127.0.0.1:18000/v1/models"
```

---

## 9. 验收标准（两阶段都要满足）

- Pod `2/2 Running`，无 CrashLoopBackOff
- `wings-infer` 健康接口 `backend_ok=true`
- 模型列表可返回 `DeepSeek-R1-Distill-Qwen-1.5B`
- ChatCompletions 返回 200 且存在 `choices[0].message.content`
- `mindie` 阶段额外要求：`config.json` 替换成功（有现场证据）

---

## 10. 常见问题与排障

### 10.1 启动报错 `only vLLM engine is supported in MVP`

原因：误用 `backend` 镜像。  
处理：改用 `Dockerfile.sidecar-20260228` 重建 sidecar。

### 10.2 报错 `Adapter for engine 'mindie' not found`

原因：镜像里不是 `backend-20260228`。  
处理：重建并确认镜像内存在 `app/engines/mindie_adapter.py`。

### 10.3 vllm_ascend 启动但 NPU 环境未生效

原因：未 `source set_vllm_ascend_env.sh`。  
处理：确认引擎容器启动脚本第一行已 source，且 ConfigMap 挂载路径正确。

### 10.4 mindie 无法启动或参数不生效

原因：`config.json` 未生成/未覆盖。  
处理：检查 `/shared-volume/start_command.sh` 是否包含 heredoc 写配置步骤；再检查目标 `config.json` 内容。

### 10.5 模型加载失败

原因：`MODEL_PATH` 与宿主机挂载不一致。  
处理：核对 hostPath `/mnt/models` 和容器内 `/models/...` 的真实存在性与权限。

---

## 11. 回滚与清理

```bash
ssh $REMOTE "kubectl -n ${NS} delete deployment wings-infer-vllm-ascend wings-infer-mindie --ignore-not-found"
ssh $REMOTE "kubectl -n ${NS} delete service wings-infer-vllm-ascend-service wings-infer-mindie-service --ignore-not-found"
ssh $REMOTE "kubectl -n ${NS} delete configmap ascend-env-scripts --ignore-not-found"
```

如果要恢复到先前 SGLang 验证环境，重新 apply 对应 `deployment-sglang.verify.yaml` / `service-sglang.verify.yaml`。

---

## 12. 证据归档建议

建议每个阶段产出一个归档文件，至少包含：

- Deployment/Service 最终 YAML
- `kubectl get pod -o wide`
- 两容器关键日志（最后 200 行）
- `/health`、`/v1/models`、`/v1/chat/completions` 返回
- mindie 的 `config.json` 实际内容

建议文件名：

- `doc_infer-control-sidecar-k8s-verify_vllm-ascend_YYYYMMDD-HHMMSS.md`
- `doc_infer-control-sidecar-k8s-verify_mindie_YYYYMMDD-HHMMSS.md`

---

## 13. 待你确认（执行前）

1. `VLLM_ASCEND_IMG` 的准确镜像名和 tag
2. `MINDIE_IMG` 的准确镜像名和 tag
3. 现场是否为“容器化 k3s”（`k3s-verify`）还是“直接 kubectl 环境”
4. 模型目录是否固定为 `/mnt/models/DeepSeek-R1-Distill-Qwen-1.5B`

以上 4 项确认后，可按本文档直接执行。
