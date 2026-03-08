# ST（昇腾 910B2C）分布式验证方案

> 文档目的：在两台 910B2C 昇腾机器上验证分布式推理，优先 **vllm-ascend**，其次 **mindie**。  
> 参照 nv.md 的结构和思路，针对昇腾 NPU 环境做适配。  
> 状态：**阶段三准备就绪**（2026-03-04）  
> 代码改造 ✅ + K8s YAML 文件 ✅，待执行：镜像构建/传输 → 部署 StatefulSet → 推理验证  
> NV 场景（vLLM DP + SGLang）已全部验证通过，当前执行 ST 昇腾 vllm-ascend 分布式验证。

---

## 机器资源

| 项目 | .110 (server) | .170 (agent) |
|------|---------------|--------------|
| IP | 7.6.52.110 | 7.6.52.170 |
| 账户/密码 | root / Xfusion@123 | root /  Fusion@123|
| hostname | `910b-47` | `root` |
| NPU | 16x 910B2C（每张 65536 MB HBM） | 16x 910B2C（每张 65536 MB HBM） |
| 工作目录 | /data3/zhanghui | /data/zhanghui（需创建） |
| 模型路径 | /mnt/cephfs/models/DeepSeek-R1-Distill-Qwen-1.5B | 需确认 cephfs 挂载情况 |
| vllm-ascend 镜像 | `quay.io/ascend/vllm-ascend:v0.14.0rc1` ✅ | `quay.io/ascend/vllm-ascend:latest`（无 v0.14.0rc1，需传输） |
| mindie 镜像 | `swr.cn-south-1.myhuaweicloud.com/ascendhub/mindie:2.2.RC1-800I-A2-py311-openeuler24.03-lts` ✅ | 同镜像 ✅（已存在） |
| wings-infer 镜像 | `wings-infer:zhanghui-ascend-st` ✅（46h 前构建） | 需传输 |
| k3s 容器 | 尚未启动（需新建 `k3s-verify-server-ascend-zhanghui`） | 尚未启动（需新建 `k3s-verify-agent-ascend-zhanghui`） |

> **注意**：.110 上已有大量他人 Docker 容器（30+），NPU 当前均空闲（HBM 3.4GB/65536MB 为系统占用）。使用 NPU 0 各一张。

---

## 容器命名约定

- 所有本次验证新起 Docker 容器统一追加 `-zhanghui` 后缀
- server: `k3s-verify-server-ascend-zhanghui`
- agent: `k3s-verify-agent-ascend-zhanghui`

---

## NPU 与模型约束

- 两台机器均仅使用 **1 张 910B2C NPU**（物理设备 davinci0）
- 模型统一为 deepseek1.5b（示例路径：`/mnt/models/DeepSeek-R1-Distill-Qwen-1.5B`）
- 优先复用机器上已有的 vllm-ascend / mindie 镜像，不重复拉取

---

## 整体方案

参照 nv.md 的**方案 A**：把 wings/wings 裸机分布式逻辑"搬"到 K8s，用 StatefulSet Pod 序号替代 IP 地址判断角色。

```
每个 Pod
 └─ wings-infer 读 NODE_RANK
     ├─ rank=0 → 生成 head 脚本 → (vllm-ascend) ray start --head + vllm serve
     │                          → (mindie)      写 config.json(rank=0) + mindieservice_daemon
     └─ rank=N → 生成 worker 脚本 → (vllm-ascend) ray start --address=head --block
                                  → (mindie)      写 config.json(rank=N) + mindieservice_daemon
```

Pod 间通信靠 Headless Service 提供固定 DNS，head 地址写死为 `infer-0.infer-hl`

---

## 与 NV 方案的关键差异

| 对比项 | NV (nv.md) | ST (本文档) |
|---|---|---|
| 加速硬件 | L20 GPU | 910B2C NPU |
| 状态检查 | `nvidia-smi` | `npu-smi info` |
| 设备文件 | `/dev/nvidia*` | `/dev/davinci0`, `/dev/davinci_manager`, `/dev/hisi_hdc`, `/dev/devmm_svm` |
| 驱动透传 | 拷贝 libnvidia-ml.so 挂载 | 直接 privileged + hostPath 挂载 davinci 设备；Ascend CANN 内嵌于引擎镜像 |
| 集通信库 | NCCL (`NCCL_SOCKET_IFNAME`) | HCCL (`HCCL_WHITELIST_DISABLE=1`, `HCCL_IF_IP`) |
| 分布式引擎 1 | vLLM Ray / vLLM DP / SGLang | vllm-ascend（Ray 模式，DP 暂不验证） |
| 分布式引擎 2 | — | mindie（配置驱动，无 Ray，via worldSize+rank） |
| 引擎镜像 | `vllm/vllm-openai` / `lmsysorg/sglang` | `quay.io/ascend/vllm-ascend:v0.14.0rc1` / mindie 镜像 |
| CANN 环境初始化 | 不需要 | `source /usr/local/Ascend/ascend-toolkit/set_env.sh` + atb |

---

## 步骤

## 阶段一：基础环境准备

### 1. 配置两台机器无密码互访

> **执行时间**: 2026-03-04  
> **状态**: ✅ 已完成

```bash
# === 在 .110 上 ===
# .110 已有 RSA key（/root/.ssh/id_rsa），直接推送到 .170
sshpass -p 'Fusion@123' ssh-copy-id -o StrictHostKeyChecking=no root@7.6.52.170
# 验证
ssh -o BatchMode=yes root@7.6.52.170 hostname   # → root ✅

# === 在 .170 上（通过 .110 跳板执行）===
# .170 无 key，先生成再推送到 .110
ssh-keygen -t rsa -N "" -f ~/.ssh/id_rsa
sshpass -p 'Xfusion@123' ssh-copy-id -o StrictHostKeyChecking=no root@7.6.52.110
# 验证
ssh -o BatchMode=yes root@7.6.52.110 hostname   # → 910b-47 ✅
```

**验证结果**:
| 方向 | 命令 | 结果 |
|------|------|------|
| .110 → .170 | `ssh root@7.6.52.170 hostname` | `root` ✅ |
| .170 → .110 | `ssh root@7.6.52.110 hostname` | `910b-47` ✅ |

### 2. 确认两台机器的 Docker 环境和 NPU 驱动

> **执行时间**: 2026-03-04  
> **状态**: ✅ 已完成

| 项目 | .110 | .170 |
|------|------|------|
| hostname | `910b-47` | `root` |
| Docker 版本 | 26.1.1 | - |
| npu-smi 版本 | 25.2.0 | 25.5.0 |
| NPU 数量 | 16x 910B2C（HBM 65536MB each）| 16x 910B2C（HBM 65536MB each）|
| NPU HBM 占用 | 3.4GB/65536MB（全部空闲）| 3.4GB/65536MB（全部空闲）|
| `/dev/davinci0` | ✅ 存在 | ✅ 存在 |
| `/dev/davinci_manager` | ✅ 存在 | ✅ 存在 |
| `/dev/hisi_hdc` | ✅ 存在 | ✅ 存在 |
| `/dev/devmm_svm` | ✅ 存在 | ✅ 存在 |
| NPU 选择 | **NPU 0**（空闲）| **NPU 0**（空闲）|

**模型路径**:
- `.110`: `/mnt/cephfs/models/DeepSeek-R1-Distill-Qwen-1.5B`（3.4GB）✅
- `.170`: `/mnt/cephfs/models/DeepSeek-R1-Distill-Qwen-1.5B`（挂载同一 cephfs）✅

**镜像**:
| 镜像 | .110 | .170 |
|------|------|------|
| `quay.io/ascend/vllm-ascend:v0.14.0rc1` | ✅ 已有（17GB）| ❌ 无此版本，需传输 |
| `swr.cn-south-1.myhuaweicloud.com/ascendhub/mindie:2.2.RC1-...` | ✅ 已有（23.1GB）| ✅ 已有 |
| `wings-infer:zhanghui-ascend-st` | ✅ 已有（164MB，46h前构建）| ❌ 需传输 |
| `rancher/k3s:v1.30.6-k3s1` | ✅ 210MB | ✅ 已从 .110 传输 |

### 3. 构建基于 Docker 的 k3s 双节点集群

> **执行时间**: 2026-03-04  
> **状态**: ✅ 已完成

**前置准备**：
- `.170` 无 cephfs 挂载 → 使用 `.110` 的 cephfs 凭证挂载，写入 fstab 持久化
- `.170` 无 `rancher/k3s:v1.30.6-k3s1` 镜像 → 通过 `docker save | ssh docker load` 传输

```bash
# === .170 挂载 cephfs ===
mkdir -p /mnt/cephfs
mount -t ceph 7.6.16.201,7.6.16.202,7.6.16.203:/ /mnt/cephfs \
  -o name=admin,secret=AQAiHaJmYPRVCRAAY27uQT8BeH+RPtJfW0eOvQ==,noatime
# 写入 fstab 持久化
echo "7.6.16.201,...:/ /mnt/cephfs ceph name=admin,secret=...,noatime,_netdev 0 2" >> /etc/fstab

# === 传输 k3s 镜像到 .170 ===
# 在 .110 执行
docker save rancher/k3s:v1.30.6-k3s1 | sshpass -p 'Fusion@123' ssh root@7.6.52.170 'docker load'

# === 在 .110 启动 k3s server ===
docker run -d --name k3s-verify-server-ascend-zhanghui \
  --privileged --restart=unless-stopped \
  --net=host \
  -v /mnt/cephfs:/mnt/cephfs \
  rancher/k3s:v1.30.6-k3s1 server \
  --node-external-ip=7.6.52.110 \
  --bind-address=7.6.52.110 \
  --advertise-address=7.6.52.110

# 获取 token
TOKEN=$(docker exec k3s-verify-server-ascend-zhanghui cat /var/lib/rancher/k3s/server/node-token)

# === 在 .170 启动 k3s agent ===
docker run -d --name k3s-verify-agent-ascend-zhanghui \
  --privileged --restart=unless-stopped \
  --net=host \
  -v /mnt/cephfs:/mnt/cephfs \
  rancher/k3s:v1.30.6-k3s1 agent \
  --server https://7.6.52.110:6443 \
  --token $TOKEN \
  --node-external-ip=7.6.52.170
```

**验证结果**:
```
NAME      STATUS   ROLES                  AGE   VERSION        INTERNAL-IP   EXTERNAL-IP
910b-47   Ready    control-plane,master   95s   v1.30.6+k3s1   7.6.52.110    7.6.52.110
root      Ready    <none>                 23s   v1.30.6+k3s1   7.6.52.170    7.6.52.170
```

两个节点均 **Ready** ✅

### 4. 准备 Ascend NPU 设备透传（替代 device plugin）

> **执行时间**: 2026-03-04  
> **状态**: ✅ 已验证

昇腾场景与 NV 不同：**不需要拷贝驱动库**（CANN toolkit 已内嵌于引擎镜像），k3s 容器以 `--privileged` 启动后 `/dev/davinci*` 自动透传。

**验证结果** (两个容器内均可见 16 张 davinci 设备):
```
# docker exec k3s-verify-server-ascend-zhanghui ls /dev/ | grep davinci
davinci0  davinci1  davinci2  ... davinci15  davinci_manager  ✅

# docker exec k3s-verify-agent-ascend-zhanghui ls /dev/ | grep davinci
davinci0  davinci1  davinci2  ... davinci15  davinci_manager  ✅
```

> 与 NV 方案的差异：NV 需要进入容器拷贝 libnvidia-ml.so 并建软链；Ascend 引擎镜像（vllm-ascend:v0.14.0rc1）内置了 CANN 8.5.0，无需从宿主机注入驱动库，只需 `privileged: true` 透传设备即可。

---

## 阶段二：代码改造（基线：`backend-dist-nv-20260303`）

> **执行时间**: 2026-03-04  
> **状态**: ✅ 已完成  
> **改造目录**: `infer-control-sidecar-main-st-dist/backend-dist-nv-20260303/`  

### 改造原则

- 以 **NV 分布式代码**（`backend-dist-nv-20260303`）为基线，不从头重写
- `start_args_compat.py` / `wings_entry.py` / `main.py` 已有完整分布式支持（直接复用）
- 重点改造：补全 **vllm-ascend HCCL env** + 新建 **mindie 分布式 adapter** + 补全 **config_loader mindie 分支**

### 5. 代码改造实际执行记录

> **执行时间**: 2026-03-04  
> **状态**: ✅ 已完成

| 文件 | 改动类型 | 改动内容 |
|---|---|---|
| `app/core/config_loader.py` | 修改 | `_merge_mindie_params`：分布式时 `worldSize=n_nodes×device_count`、`multiNodesInferEnabled=True`、`npuDeviceIds` 按节点数展开 |
| `app/core/config_loader.py` | 新增 | `_handle_mindie_distributed`：从 `distributed_config.json` 读 `master_port`，注入 `mindie_master_addr/port` |
| `app/core/config_loader.py` | 修改 | `_handle_distributed`：新增 `elif engine == 'mindie'` 分支 |
| `app/engines/vllm_adapter.py` | 修改 | `build_start_script` 分布式 ray 部分：`vllm_ascend` 使用 `HCCL_WHITELIST_DISABLE=1`/`HCCL_IF_IP`/`HCCL_SOCKET_IFNAME`（替代 NCCL 变量）+ CANN env setup |
| `app/engines/mindie_adapter.py` | **新建** | 完整 mindie adapter：单机 + 多节点（`MASTER_ADDR`/`RANK`/`WORLD_SIZE`/`HCCL` env 注入 + config.json merge-update bash 脚本）|
| `app/config/mindie_default.json` | **新建** | MindIE 默认参数配置（从 ST 原始代码复制）|
| `app/config/distributed_config.json` | **新建** | 分布式端口配置，新增 `mindie_distributed.master_port: 27070` |
| `k8s/statefulset-mindie-ascend-dist.yaml` | **新建** | 2 节点 MindIE StatefulSet（`hostNetwork: true` + privileged + `/dev/davinci0` hostPath）|
| `k8s/service-mindie-ascend-dist.yaml` | **新建** | Namespace + Headless Service + ClusterIP Service |

**未改动文件**（NV 版已完整实现，直接复用）：
- `app/core/start_args_compat.py` — `--nnodes`/`--node-rank`/`--head-node-addr`/`--distributed-executor-backend` 已有 ✅
- `app/core/wings_entry.py` — rank>0 不注入 host/port 已有 ✅
- `app/main.py` — rank>0 跳过 proxy 已有 ✅

### 6. config_loader.py 关键改动详情

#### `_merge_mindie_params` — 分布式 worldSize 计算

```python
# 改动前（只支持单机）
_adjust_tensor_parallelism(params, ctx["device_count"], 'worldSize')
params['npuDeviceIds'] = [[i for i in range(ctx["device_count"])]]

# 改动后（区分单机 / 分布式）
if ctx.get('distributed'):
    node_ips = get_node_ips()   # 读 NODE_IPS 环境变量
    n_nodes = len([ip.strip() for ip in node_ips.split(',')]) if node_ips else 1
    params['worldSize'] = int(ctx["device_count"]) * n_nodes       # 例：1×2=2
    params['multiNodesInferEnabled'] = True
    params['npuDeviceIds'] = [[i for i in range(ctx["device_count"])] for _ in range(n_nodes)]
    # 验证环境（2节点各1NPU）=> npuDeviceIds = [[0], [0]], worldSize=2
else:
    _adjust_tensor_parallelism(params, ctx["device_count"], 'worldSize')
    params['npuDeviceIds'] = [[i for i in range(ctx["device_count"])]]
```

#### `_handle_distributed` — 新增 mindie 分支

```python
def _handle_distributed(engine, cmd_params, model_info):
    ...
    elif engine == 'mindie':
        _handle_mindie_distributed(distributed_config, cmd_params)

# 新函数
def _handle_mindie_distributed(distributed_config, cmd_params):
    mindie_cfg = distributed_config.get('mindie_distributed', {})
    master_port = mindie_cfg.get('master_port', 27070)
    cmd_params.update({
        'mindie_master_addr': get_master_ip(),   # 读 MASTER_ADDR / NODE_IPS[0]
        'mindie_master_port': master_port,
    })
```

### 7. mindie_adapter.py 关键逻辑（新建）

mindie 多节点 start_command.sh 生成逻辑（`build_start_script` 核心）：

```bash
# === rank-0 和 rank-1 生成内容相同的脚本结构 ===
# (差异体现在 RANK 环境变量和 config.json 中的 ipAddress 字段)

# 1. 源 CANN/MindIE 环境
set +u
[ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ] && source /usr/local/Ascend/ascend-toolkit/set_env.sh
[ -f /usr/local/Ascend/mindie/set_env.sh ] && source /usr/local/Ascend/mindie/set_env.sh
set -u

# 2. HCCL 分布式环境变量（nnodes>1 时注入）
export MASTER_ADDR=7.6.52.110         # head_node_addr
export MASTER_PORT=27070
export RANK=0                          # node_rank (infer-0=0, infer-1=1)
export WORLD_SIZE=2                    # nnodes
export HCCL_WHITELIST_DISABLE=1        # 容器内必须禁用白名单
export HCCL_IF_IP=$(hostname -i)       # HCCL 绑定 IP
export HCCL_SOCKET_IFNAME=eth0
export GLOO_SOCKET_IFNAME=eth0

# 3. merge-update config.json（保留原始字段，仅覆盖 worldSize/npuDeviceIds/ipAddress 等）
cat > /tmp/_mindie_overrides.json << 'OVERRIDES_EOF'
{
  "server":  { "ipAddress": "0.0.0.0", "port": 18000, ... },
  "backend": { "npuDeviceIds": [[0], [0]], "multiNodesInferEnabled": true },
  "model_config": { "worldSize": 2, "modelWeightPath": "/models/...", ... },
  ...
}
OVERRIDES_EOF

# Python inline merge script (读原始 config.json → merge → 写回)
# (详见 mindie_adapter.py build_start_script)

# 4. 启动 daemon
cd /usr/local/Ascend/mindie/latest/mindie-service
exec ./bin/mindieservice_daemon
```

**rank-0 vs rank-1 差异**：
- rank-0：`ipAddress=0.0.0.0`（对外暴露 HTTP API），proxy 容器启动
- rank-1：`ipAddress=127.0.0.1`（不暴露 HTTP），`RANK=1`，wings 跳过 proxy

### 改造后文件清单

#### vllm-ascend Ray（rank-0 head）
```bash
# start_command.sh 内容
set +u
source /usr/local/Ascend/ascend-toolkit/set_env.sh
source /usr/local/Ascend/nnal/atb/set_env.sh
set -u

export HCCL_WHITELIST_DISABLE=1
export HCCL_IF_IP=$(hostname -i)

ray start --head --port=6379 --num-gpus=1 --dashboard-host=0.0.0.0

# 等待 worker 加入
for i in $(seq 1 60); do
  COUNT=$(python3 -c "import ray; ray.init(address='auto',ignore_reinit_error=True); \
    print(len([n for n in ray.nodes() if n['alive']])); ray.shutdown()" 2>/dev/null || echo 0)
  [ "$COUNT" -ge "2" ] && break
  sleep 5
done

exec python3 -m vllm.entrypoints.openai.api_server \
  --model /models/DeepSeek-R1-Distill-Qwen-1.5B \
  --served-model-name DeepSeek-R1-Distill-Qwen-1.5B \
  --host 0.0.0.0 --port 17000 \
  --tensor-parallel-size 2 \
  --distributed-executor-backend ray \
  --trust-remote-code
```

#### vllm-ascend Ray（rank-1 worker）
```bash
# start_command.sh 内容
set +u
source /usr/local/Ascend/ascend-toolkit/set_env.sh
source /usr/local/Ascend/nnal/atb/set_env.sh
set -u

export HCCL_WHITELIST_DISABLE=1
export HCCL_IF_IP=$(hostname -i)

# 等待 head 可达
for i in $(seq 1 60); do
  python3 -c "import socket; s=socket.socket(); \
    s.settimeout(2); s.connect(('infer-0.infer-hl',6379)); s.close()" 2>/dev/null && break
  sleep 5
done

exec ray start --address=infer-0.infer-hl:6379 --num-gpus=1 --block
```

> **注意**：`--num-gpus=1` 在 vllm-ascend Ray 模式中表示 NPU 数量，依赖 ray 的 NPU 资源上报；如 ray 无法感知 NPU，需在 ray start 时添加 `--resources '{"NPU": 1}'` 替代。

### 7. mindie 分布式脚本生成逻辑（其次实现）

mindie 跨节点分布式不使用 Ray，而是通过 **config.json 中的 `worldSize`/`npuDeviceIds`** 字段以及 PyTorch 分布式环境变量（`MASTER_ADDR`、`MASTER_PORT`、`RANK`、`WORLD_SIZE`）实现多节点通信（HCCL）。

#### mindie（rank-0，node=7.6.52.110）
```bash
# start_command.sh 内容（由 wings-infer 生成）
set +u
source /usr/local/Ascend/ascend-toolkit/set_env.sh
source /usr/local/Ascend/nnal/atb/set_env.sh
source /usr/local/Ascend/mindie/set_env.sh
set -u

export HCCL_WHITELIST_DISABLE=1
export MASTER_ADDR=infer-0.infer-hl
export MASTER_PORT=29500
export RANK=0
export WORLD_SIZE=2

# merge-update config.json：worldSize=2, npuDeviceIds=[[0]], ipAddress=0.0.0.0, port=17000
# （inline Python merge 脚本，与单机 mindie 方案一致，增加 worldSize 和 rank 字段）
...

cd /usr/local/Ascend/mindie/latest/mindie-service
exec ./bin/mindieservice_daemon
```

#### mindie（rank-1，node=7.6.52.170）
```bash
# 与 rank-0 类似，区别：
#   RANK=1, ipAddress=0.0.0.0（不对外提供 API），port=17000
#   不启动 proxy（wings main.py 在 rank>0 时跳过）
export RANK=1
export MASTER_ADDR=infer-0.infer-hl
...
exec ./bin/mindieservice_daemon
```

---

## 阶段三：镜像构建与推送

### 8. 构建 wings-infer 镜像并导入 k3s containerd

> **前提**: 阶段二代码改造已完成，改造目录为 `infer-control-sidecar-main-st-dist/backend-dist-nv-20260303/`

```bash
# === 在 .110 物理机上执行 ===

# 1. 准备代码（宿主机路径，需映射到 Docker Build 上下文）
# 本地 Windows 工作区路径：
#   F:\zhanghui\wings-k8s\infer-control-sidecar-main\infer-control-sidecar-main-st-dist\
# 先 rsync / scp 传到 .110：
#   scp -r <local_path>\infer-control-sidecar-main-st-dist root@7.6.52.110:/data3/zhanghui/

# 2. 构建镜像
cd /data3/zhanghui/infer-control-sidecar-main-st-dist
docker build -t wings-infer:zhanghui-ascend-st-dist .

# 3. 导入到 .110 k3s containerd
docker save wings-infer:zhanghui-ascend-st-dist -o /tmp/wings-infer-ascend-st-dist.tar
docker cp /tmp/wings-infer-ascend-st-dist.tar k3s-verify-server-ascend-zhanghui:/tmp/
docker exec k3s-verify-server-ascend-zhanghui ctr -n k8s.io images import /tmp/wings-infer-ascend-st-dist.tar

# 4. 同步到 .170 agent 节点
scp /tmp/wings-infer-ascend-st-dist.tar root@7.6.52.170:/tmp/
ssh root@7.6.52.170 "docker cp /tmp/wings-infer-ascend-st-dist.tar \
  k3s-verify-agent-ascend-zhanghui:/tmp/ && \
  docker exec k3s-verify-agent-ascend-zhanghui ctr -n k8s.io images import \
  /tmp/wings-infer-ascend-st-dist.tar"

# 5. 验证两节点均可见
docker exec k3s-verify-server-ascend-zhanghui ctr -n k8s.io images ls | grep wings-infer
ssh root@7.6.52.170 "docker exec k3s-verify-agent-ascend-zhanghui ctr -n k8s.io images ls | grep wings-infer"
```

### 9. 确认引擎镜像并导入

```bash
# ── 阶段 A：vllm-ascend 引擎镜像 ──
# 优先复用机器上已有镜像，例如 quay.io/ascend/vllm-ascend:v0.14.0rc1
docker save quay.io/ascend/vllm-ascend:v0.14.0rc1 | \
  docker exec -i k3s-verify-server-ascend-zhanghui ctr -n k8s.io images import -

ssh root@7.6.52.170 "docker save quay.io/ascend/vllm-ascend:v0.14.0rc1 | \
  docker exec -i k3s-verify-agent-ascend-zhanghui ctr -n k8s.io images import -"

# ── 阶段 B：mindie 引擎镜像（待确认当前机器上的具体 tag） ──
# 示例：mindieservice:2.2.RC1 或类似
MINDIE_IMAGE=<mindie镜像名:tag>    # 需在机器上 docker images 确认

docker save $MINDIE_IMAGE | \
  docker exec -i k3s-verify-server-ascend-zhanghui ctr -n k8s.io images import -

ssh root@7.6.52.170 "docker save $MINDIE_IMAGE | \
  docker exec -i k3s-verify-agent-ascend-zhanghui ctr -n k8s.io images import -"

# 确认模型文件在两台机器均可访问
ls /mnt/models/DeepSeek-R1-Distill-Qwen-1.5B
```

---

## 阶段四：K8s 资源部署（全部通过 docker exec k3s-verify-server-ascend-zhanghui 运行）

> **YAML 文件位置**: `infer-control-sidecar-main-st-dist/k8s/`  
> - `service-vllm-ascend-dist.yaml` — Namespace + Headless Service(ray:6379/vllm:17000/proxy:18000/health:19000) + ClusterIP ✅（2026-03-04 新建）
> - `statefulset-vllm-ascend-dist.yaml` — 2 节点 vllm-ascend Ray StatefulSet ✅（2026-03-04 新建）
> - `service-mindie-ascend-dist.yaml` — Namespace + Headless + ClusterIP（mindie 专用）
> - `statefulset-mindie-ascend-dist.yaml` — 2 节点 MindIE StatefulSet  

### 10. 创建 Namespace 和基础 K8s 资源

```bash
docker exec -i k3s-verify-server-ascend-zhanghui kubectl create namespace inference

# Headless Service（Pod 互相发现）
cat <<EOF | docker exec -i k3s-verify-server-ascend-zhanghui kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: infer-hl
  namespace: inference
spec:
  clusterIP: None
  selector:
    app: infer-dist
  ports:
    - name: ray-gcs
      port: 6379
    - name: hccl-master
      port: 29500
    - name: mindie-dist
      port: 29600
EOF

# ClusterIP Service（对外 API，只打 rank-0）
cat <<EOF | docker exec -i k3s-verify-server-ascend-zhanghui kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: infer-api
  namespace: inference
spec:
  selector:
    app: infer-dist
    statefulset.kubernetes.io/pod-name: infer-0
  ports:
    - name: proxy
      port: 18000
      targetPort: 18000
EOF
```

### 11. 部署 StatefulSet（阶段 A：vllm-ascend Ray）

> **状态**: ✅ YAML 文件已准备（`service-vllm-ascend-dist.yaml` + `statefulset-vllm-ascend-dist.yaml`）

先将 YAML 文件 scp 到 .110，再通过 k3s server 容器部署：

```bash
# === 1. 将 YAML 文件传输到 .110 ===
# 在 Windows 本地执行（或从 .110 直接访问 rsync）：
scp infer-control-sidecar-main-st-dist/k8s/service-vllm-ascend-dist.yaml \
  root@7.6.52.110:/data3/zhanghui/
scp infer-control-sidecar-main-st-dist/k8s/statefulset-vllm-ascend-dist.yaml \
  root@7.6.52.110:/data3/zhanghui/

# === 2. 创建 Namespace 和 Service ===
docker exec -i k3s-verify-server-ascend-zhanghui sh -c \
  "kubectl apply -f /host/data3/zhanghui/service-vllm-ascend-dist.yaml"
# 或者通过 docker exec 直接 cat | kubectl apply：
# cat service-vllm-ascend-dist.yaml | docker exec -i k3s-verify-server-ascend-zhanghui kubectl apply -f -

# === 3. 部署 StatefulSet ===
docker exec -i k3s-verify-server-ascend-zhanghui sh -c \
  "kubectl apply -f /host/data3/zhanghui/statefulset-vllm-ascend-dist.yaml"

# 也可以通过 stdin 传入（无需宿主机路径映射）：
# cat k8s/service-vllm-ascend-dist.yaml | docker exec -i k3s-verify-server-ascend-zhanghui kubectl apply -f -
# cat k8s/statefulset-vllm-ascend-dist.yaml | docker exec -i k3s-verify-server-ascend-zhanghui kubectl apply -f -

# === 4. 确认两个 Pod 调度到不同节点 ===
docker exec -i k3s-verify-server-ascend-zhanghui \
  kubectl get pods -n wings-verify-st-dist -o wide
# 预期：
# infer-vllm-0   0/2   Init   0   ...  7.6.52.110
# infer-vllm-1   0/2   Init   0   ...  7.6.52.170
```

**关键设计说明**（与原文档 inline YAML 的差异）：

| 项目 | 原文档 inline | 当前 YAML 文件 |
|------|--------------|---------------|
| namespace | `inference` | `wings-verify-st-dist`（与 mindie 统一） |
| StatefulSet name | `infer` | `infer-vllm`（避免与 mindie 冲突） |
| serviceName | `infer-hl` | `infer-hl-vllm` |
| wings-infer image | `wings-infer:dist-ascend-st-zhanghui` | `wings-infer:zhanghui-ascend-st-dist` |
| HEAD_NODE_ADDR | `infer-0.infer-hl.inference.svc.cluster.local` | `7.6.52.110`（hostNetwork 下用真实 IP）|
| 驱动库挂载 | 有 `devmm_svm` | 无（vllm-ascend 镜像内置 CANN）|

> ⚠️ **Ray NPU 资源感知问题（待验证 #7）**：`ray start --num-gpus=1` 在昇腾环境下 ray 可能无法自动上报 NPU 资源。如果 vllm serve 报错 `ResourcesUnavailable: Insufficient GPUs`，需参考「问题排查」修改 ray 启动命令。

### 12. 阶段 B：切换 mindie 时的差异点（概要）

切换到 mindie 时，主要改动：
1. `ENGINE` 环境变量改为 `mindie`
2. `image` 改为 mindie 引擎镜像
3. 移除 `DISTRIBUTED_EXECUTOR_BACKEND`，改为 `MINDIE_WORLD_SIZE=2` 等 mindie 分布式参数
4. engine 容器启动逻辑不变（等待 start_command.sh 并执行）
5. start_command.sh 内容由 wings-infer 的 mindie_adapter 生成，包含 HCCL 通信参数和 config.json merge 逻辑

---

## 阶段五：验证

### 13. 观察 Pod 启动状态
```bash
docker exec -i k3s-verify-server-ascend-zhanghui kubectl get pods -n inference -w

# 预期：
# infer-0   2/2   Running   0   ...   node=7.6.52.110
# infer-1   2/2   Running   0   ...   node=7.6.52.170

# 查看 wings-infer 生成的脚本内容
docker exec -i k3s-verify-server-ascend-zhanghui kubectl exec -n inference infer-0 \
  -c wings-infer -- cat /shared-volume/start_command.sh

# 查看 engine 日志
docker exec -i k3s-verify-server-ascend-zhanghui kubectl logs -n inference infer-0 -c engine -f
docker exec -i k3s-verify-server-ascend-zhanghui kubectl logs -n inference infer-1 -c engine -f
```

### 14. 验证推理服务可用
```bash
# 等待引擎就绪（健康检查）
docker exec -i k3s-verify-server-ascend-zhanghui sh -c \
  "curl http://<infer-api ClusterIP>:18000/health"

# 发送推理请求
docker exec -i k3s-verify-server-ascend-zhanghui sh -c "curl -X POST \
  http://<infer-api ClusterIP>:18000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    \"model\": \"DeepSeek-R1-Distill-Qwen-1.5B\",
    \"messages\": [{\"role\": \"user\", \"content\": \"1+1=?\"}],
    \"max_tokens\": 50
  }'"
```

### 15. 各引擎切换验证
```bash
# 切换引擎后滚动重启
docker exec -i k3s-verify-server-ascend-zhanghui kubectl rollout restart statefulset/infer -n inference
docker exec -i k3s-verify-server-ascend-zhanghui kubectl rollout status statefulset/infer -n inference
```

---

## 待确认事项（开发前须核实）

| # | 事项 | 状态 | 说明 |
|---|------|------|------|
| 1 | mindie 镜像 tag | ✅ 已确认 | .110 和 .170 均有 `2.2.RC1-800I-A2-py311-openeuler24.03-lts` |
| 2 | NPU 设备编号 | ✅ 已确认 | 两台机器各选 **NPU 0**（`ASCEND_VISIBLE_DEVICES=0`），HBM 全空闲 |
| 3 | 模型路径 | ✅ 已确认 | 两台均挂载 cephfs，路径 `/mnt/cephfs/models/DeepSeek-R1-Distill-Qwen-1.5B`（3.4GB）|
| 4 | k3s 容器内 davinci 设备可见性 | ✅ 已验证 | `--privileged --net=host` 下两节点均可见 davinci0~davinci15 |
| 5 | 代码改造（config_loader/mindie_adapter/vllm_adapter） | ✅ 已完成 | 详见阶段二 步骤 5-7 |
| 6 | mindie 多节点分布式的通信方式 | ❓ 待验证 | 需验证 `2.2.RC1` 版本跨节点 worldSize=2 是否可用（单机已有历史验证记录） |
| 7 | vllm-ascend Ray 对 NPU 资源上报 | ❓ 待验证 | 需确认 ray start 在昇腾环境是否正确上报 NPU 资源，或改用 `--resources '{"NPU":1}'` |
| 8 | vllm-ascend:v0.14.0rc1 传输到 .170 | ⏳ 待执行 | 17GB，需 docker save+scp+docker load |
| 9 | 构建 wings-infer:zhanghui-ascend-st-dist 镜像 | ⏳ 待执行 | 基于本次改造代码 docker build，然后导入 k3s containerd |
| 10 | 镜像传输到两节点 k3s containerd | ⏳ 待执行 | wings-infer + vllm-ascend + mindie 三张镜像均需导入 |
| 11 | vllm-ascend K8s YAML 文件 | ✅ 已完成 | `service-vllm-ascend-dist.yaml` + `statefulset-vllm-ascend-dist.yaml` 已创建（2026-03-04）|

---

## 问题排查

### Ray 无法感知 NPU（资源不足错误）

**现象**: vllm serve 报 `ResourcesUnavailable: Insufficient GPUs` 或 ray node 的 GPU 资源为 0  
**原因**: `ray start --num-gpus=1` 在 vllm-ascend 镜像中依赖 Ascend Docker Runtime 的 GPU 资源上报，k3s privileged 模式可能不触发  
**解法 A**: 用 `--resources` 替代 `--num-gpus`

修改 [vllm_adapter.py](../infer-control-sidecar-main/infer-control-sidecar-main-st-dist/backend-dist-nv-20260303/app/engines/vllm_adapter.py) 中 `build_start_script`：
```python
# 原来（约 328、344 行）：
"ray start --head --port=6379 --num-gpus=1 --dashboard-host=0.0.0.0"
"ray start --address=... --num-gpus=1 --block"

# 昇腾改为：
"ray start --head --port=6379 --resources='{\"NPU\": 1}' --dashboard-host=0.0.0.0"
"ray start --address=... --resources='{\"NPU\": 1}' --block"
```

**解法 B**: 升级 vllm-ascend 镜像到支持 NPU 资源自动上报的版本

### HCCL 通信初始化失败

**现象**: ray worker 报 `HCCL error` 或超时  
**检查点**：
```bash
# 确认两节点 HCCL 端口连通
ssH root@7.6.52.170 nc -zv 7.6.52.110 6379
ssh root@7.6.52.110 nc -zv 7.6.52.170 6379

# 确认 Pod 内 HCCL 变量
docker exec -i k3s-verify-server-ascend-zhanghui \
  kubectl exec -n wings-verify-st-dist infer-vllm-0 -c engine -- env | grep HCCL
```