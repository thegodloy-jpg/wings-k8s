# vllm-ascend k3s-verify 验证报告

**日期**: 2026-03-02  
**环境**: `k3s-verify-zhanghui` (host: 7.6.52.110)  
**模型**: DeepSeek-R1-Distill-Qwen-1.5B  
**引擎镜像**: `vllm-ascend:v0.14.0rc1`  
**状态**: ✅ 全部测试通过

---

## 1. 环境说明

| 项目 | 值 |
|------|----|
| 宿主机 | 7.6.52.110 |
| k3s-verify 容器 | `k3s-verify-zhanghui` |
| k3s 容器 Docker Bridge IP | **172.17.0.12** |
| Namespace | `wings-verify` |
| Deployment | `wings-infer-vllm-ascend` |
| Pod | `wings-infer-vllm-ascend-5b585d5648-8df2l` |
| Pod IP | 10.42.0.24 |
| NodePort (proxy 18000) | **31810** |
| NodePort (wings-health 19000) | **31910** |
| 使用 NPU | **NPU 0** (Ascend 910B) |

> **访问说明**: Pod IP（10.42.0.24）从宿主机不可直接访问（k3s CNI 隔离）。  
> 需通过 **k3s 容器 Bridge IP（172.17.0.12）+ NodePort** 访问，或从 pod 内部 curl 。

---

## 2. 关键修复记录

### 2.1 ZSH_VERSION `set -u` 崩溃

**问题**: CANN 的 `nnal/atb/set_env.sh` 在脚本开头引用了 `ZSH_VERSION` 变量但未给默认值，与 `set -euo pipefail` 的 `-u` 标志不兼容，导致 `start_command.sh` 执行时立即崩溃（`unbound variable`）。

**修复**: 在 `vllm_adapter.py` 的 `build_start_script` 函数中，`vllm_ascend` 分支生成的 `start_command.sh` 脚本 source CANN env 前后包裹 `set +u` / `set -u`：

```bash
# set +u: nnal/atb/set_env.sh references ZSH_VERSION without default
set +u
[ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ] \
  && source /usr/local/Ascend/ascend-toolkit/set_env.sh \
  || echo 'WARN: ascend-toolkit/set_env.sh not found'
[ -f /usr/local/Ascend/nnal/atb/set_env.sh ] \
  && source /usr/local/Ascend/nnal/atb/set_env.sh \
  || echo 'WARN: nnal/atb/set_env.sh not found'
set -u
exec python3 -m vllm.entrypoints.openai.api_server ...
```

### 2.2 驱动库挂载策略

**问题**: 初始方案挂载整个 `/usr/local/Ascend` 目录，覆盖了镜像内置的 CANN 8.5.0，导致库版本冲突。

**修复**: Deployment yaml 仅挂载 driver 子目录：

```yaml
- name: ascend-driver
  hostPath:
    path: /usr/local/Ascend/driver
    type: Directory
# 挂载到 engine 容器的 /usr/local/Ascend/driver（只读）
```

镜像内置 CANN 路径 `ASCEND_TOOLKIT_HOME=/usr/local/Ascend/cann-8.5.0` 不受影响。

### 2.3 NPU 设备选择（无法切换，使用默认 NPU 0）

**背景**: 生产环境 NPU 0-3 中，用户期望使用 NPU 2 进行验证。

**尝试过的方案**（均已确认无效）:

| 方案 | 结果 | 根因 |
|------|------|------|
| `ASCEND_VISIBLE_DEVICES=2` env var | ❌ 无效 | 需要 Ascend Docker Runtime hook 在 container 启动时重映射设备，k3s 嵌套 Docker 环境中该 hook 未注入 |
| `ASCEND_DEVICE_ID=2` env var | ❌ 无效 | 该变量为 CANN C-level 变量，`torch_npu` 的 `set_device(local_rank)` 不读取此值 |
| Python launcher `sitecustomize.py` | ❌ 崩溃 | vllm 多进程启动时 `__main__` 模块初始化冲突 |

**根因**: vllm TP=1 时 hardcode `local_rank=0`，调用 `torch.npu.set_device(0)`，始终使用物理 NPU 0。  
若需指定 NPU，需在宿主机安装 **Ascend Device Plugin for Kubernetes**，通过 resource quota 方式分配。

**结论**: 验证环境使用 NPU 0，用户已确认"指定显卡为npu0，就不会报错，能够验通"。

---

## 3. 最终部署配置概要

### `k8s/deployment-vllm-ascend.verify.yaml` 关键配置

```yaml
# vllm-ascend-engine 容器
env: []           # 不设置 ASCEND_* 变量（k3s 嵌套环境中无效）
volumeMounts:
  - name: shared-volume    # 存放 start_command.sh
    mountPath: /shared
  - name: model-volume     # 模型权重 (hostPath: /mnt/cephfs/models)
    mountPath: /models
  - name: ascend-driver    # 仅 driver (hostPath: /usr/local/Ascend/driver)
    mountPath: /usr/local/Ascend/driver
    readOnly: true

# Service NodePort
ports:
  - name: proxy
    nodePort: 31810
    targetPort: 18000
  - name: health
    nodePort: 31910
    targetPort: 19000
```

### `start_command.sh` (shared-volume 实际内容)

```bash
#!/usr/bin/env bash
set -euo pipefail
set +u
[ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ] \
  && source /usr/local/Ascend/ascend-toolkit/set_env.sh || echo 'WARN: ...'
[ -f /usr/local/Ascend/nnal/atb/set_env.sh ] \
  && source /usr/local/Ascend/nnal/atb/set_env.sh || echo 'WARN: ...'
set -u
exec python3 -m vllm.entrypoints.openai.api_server \
  --host 0.0.0.0 --port 17000 \
  --served-model-name DeepSeek-R1-Distill-Qwen-1.5B \
  --model /models/DeepSeek-R1-Distill-Qwen-1.5B \
  --trust-remote-code --dtype auto --kv-cache-dtype auto \
  --gpu-memory-utilization 0.9 --max-num-batched-tokens 4096 \
  --block-size 16 --max-num-seqs 32 --seed 0 --max-model-len 5120 \
  --tensor-parallel-size 1
```

---

## 4. 验证测试结果

验证时间: 2026-03-02  
验证环境: `k3s-verify-zhanghui` (7.6.52.110)

| 编号 | 测试项 | 方式 | 结果 | 说明 |
|------|--------|------|------|------|
| T1 | vllm 引擎内部 health | pod logs (127.0.0.1 health GET 200) | ✅ **PASS** | 引擎 /health 200 OK |
| T2a | Proxy health (NodePort) | `curl http://172.17.0.12:31810/health` | ✅ **PASS** | `{"s":1,"p":"ready","backend_ok":true,"backend_code":200}` |
| T2b | Wings health (NodePort) | `curl http://172.17.0.12:31910/health` | ✅ **PASS** | 同上 |
| T3 | Models list | `curl http://172.17.0.12:31810/v1/models` | ✅ **PASS** | `DeepSeek-R1-Distill-Qwen-1.5B, max_model_len:5120` |
| T4 | Chat 推理（引擎直连） | `kubectl exec ... curl http://127.0.0.1:17000/v1/chat/completions` | ✅ **PASS** | 返回完整推理链，`1+1=2` ✓ |
| T5 | Chat 推理（经过代理） | `kubectl exec ... curl http://127.0.0.1:18000/v1/chat/completions` | ✅ **PASS** | 同上，proxy 18000→engine 17000 转发正常 |
| T6 | NPU 使用确认（推理后） | `npu-smi info` | ✅ **PASS** | NPU 0: `VLLMEngineCor PID 260937, 55924MB` HBM |

### T4/T5 推理响应摘要

**请求**: `{"model":"DeepSeek-R1-Distill-Qwen-1.5B","messages":[{"role":"user","content":"1+1=?"}],"max_tokens":80}`

**响应** (摘要):
```
<think>
To solve the equation 1 + 1, I start by recognizing that the numbers involved are both 1.
Next, I perform the addition operation by combining these two quantities.
The result of adding 1 and 1 is 2.
</think>

**Solution:**
To solve the equation 1 + 1, follow these simple steps:
1. Identify the numbers involved...
```

`finish_reason: length`（达到 `max_tokens:80` 限制）, `usage: {prompt_tokens:9, completion_tokens:80, total_tokens:89}`

---

## 5. NPU 使用情况

```
npu-smi info 验证结果:
+===========================+===============+=================================+=================+
| NPU     Chip              | Process id    | Process name                    | Memory(MB)      |
+---------------------------+---------------+---------------------------------+-----------------+
| 0       0                 | 260937        | VLLMEngineCor                   | 55924           |
+---------------------------+---------------+---------------------------------+-----------------+
| 1-3     (空闲，约3414MB)   | -             | -                               | -               |
+---------------------------+---------------+---------------------------------+-----------------+
| 4-15    (生产工作负载)      | [禁止触碰]     | [生产服务]                       | -               |
+---------------------------+---------------+---------------------------------+-----------------+
```

- 引擎独占 NPU 0，HBM 55924MB（推理后比启动时 55799MB 略增，KV cache 已激活）
- NPU 1-3 空闲，可供其他验证任务使用（若需切换 NPU 需安装 Ascend Device Plugin）

---

## 6. 已知限制

| 限制 | 说明 | 影响 |
|------|------|------|
| `NodePort` 外部 curl 传 JSON body 问题 | 从宿主机通过 NodePort 调用 `/v1/chat/completions` 时，SSH + PowerShell 的转义导致请求 body 解析失败（`messages` 丢失） | 外部工具直接调用受限；从容器内部或 port-forward 方式正常 |
| NPU 设备不可切换 | k3s 嵌套 Docker 中 Ascend Device Plugin 未安装，只能用 NPU 0 | 验证环境限制，生产 k8s 集群不受影响 |
| 全量 `/usr/local/Ascend` 不可挂载 | 会覆盖镜像内置 CANN，只能挂载 driver 子目录 | 部署时需注意 volume 配置 |

---

## 7. 结论

`infer-control-sidecar` + `vllm-ascend:v0.14.0rc1` 在 `k3s-verify-zhanghui` 嵌套环境中验证通过：

- ✅ Pod 稳定运行（2/2 Running，无 crash loop）
- ✅ ZSH_VERSION / CANN set_env.sh 兼容性问题已修复
- ✅ 引擎健康检查、模型列表、Chat 推理全部正常
- ✅ Wings-infer 代理转发 18000→17000 正常
- ✅ DeepSeek-R1-Distill-Qwen-1.5B 推理链（`<think>...</think>`）正常输出
- ✅ NPU 0 专用，HBM 利用率正常（55924MB / ~65536MB）

**验证结论**: **PASS** — 可推进至正式 k8s 集群部署。
