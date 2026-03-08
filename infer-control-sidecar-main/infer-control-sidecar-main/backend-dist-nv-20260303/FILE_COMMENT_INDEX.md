# backend-dist-nv-20260303 — NV 分布式版本文件索引

**创建日期**：2026-03-03  
**基线版本**：`backend-ascend-st-2603030944`  
**目标场景**：NV GPU（L20）双节点分布式推理，K8s StatefulSet 方案（方案 A）  
**支持引擎**：vLLM Ray、vLLM DP、SGLang

---

## 目录结构

```
backend-dist-nv-20260303/
├── Dockerfile                  # 分布式专用镜像，COPY 指向本目录 app/，启用 ray 依赖
├── requirements.txt            # 新增 ray[default]>=2.9.0
├── FILE_COMMENT_INDEX.md       # 本文件（变更说明索引）
└── app/                        # 应用代码（从 backend-ascend-st-2603030944 分叉）
    ├── main.py                 # [改] rank>0 时跳过 proxy 启动
    ├── core/
    │   ├── start_args_compat.py  # [改] 增加 --nnodes/--node-rank/--head-node-addr/--distributed-executor-backend，移除 distributed 报错封锁
    │   ├── wings_entry.py        # [改] 注入分布式参数到 merged dict
    │   ├── engine_manager.py     # [不变]
    │   ├── config_loader.py      # [不变]
    │   ├── hardware_detect.py    # [不变]
    │   └── port_plan.py          # [不变]
    ├── engines/
    │   ├── vllm_adapter.py       # [改] build_start_script() 按 rank + backend 生成 head/worker 脚本
    │   ├── sglang_adapter.py     # [改] build_start_script() 追加 --nnodes/--node-rank/--dist-init-addr
    │   └── mindie_adapter.py     # [不变，Ascend Only]
    ├── config/
    │   └── settings.py           # [不变]
    └── proxy/                    # [不变]
```

---

## 关键变更说明

### 1. `app/core/start_args_compat.py`

```python
# 新增参数
parser.add_argument("--nnodes",       type=int,   default=1)
parser.add_argument("--node-rank",    type=int,   default=0)
parser.add_argument("--head-node-addr", type=str, default="")
parser.add_argument("--distributed-executor-backend", type=str,
                    choices=["ray","dp_deployment",""], default="")

# 删除（或注释掉）：
# if args.distributed:
#     raise ValueError("distributed mode is not supported in MVP")
```

### 2. `app/main.py`

```python
node_rank = int(os.getenv("NODE_RANK", "0"))
if node_rank == 0:
    # 正常启动 proxy + health
    ...
else:
    # worker 节点：只启动 engine，不启动 proxy
    run_engine_only(launch_args)
```

### 3. `app/engines/vllm_adapter.py` — vLLM Ray

**rank-0 head 脚本**（写入 `/shared-volume/start_command.sh`）：
```bash
export VLLM_HOST_IP=$(hostname -i)
export NCCL_SOCKET_IFNAME=eth0
export GLOO_SOCKET_IFNAME=eth0
ray start --head --port=6379 --num-gpus=1 --dashboard-host=0.0.0.0
# 等待 worker 加入 (nnodes-1 个节点)
for i in $(seq 1 60); do
  COUNT=$(python3 -c "import ray; ray.init(address='auto', ignore_reinit_error=True); \
    print(len([n for n in ray.nodes() if n['alive']])); ray.shutdown()" 2>/dev/null || echo 0)
  [ "$COUNT" -ge "2" ] && break
  sleep 5
done
exec python3 -m vllm.entrypoints.openai.api_server \
  --model /models/<MODEL_NAME> \
  --host 0.0.0.0 --port 17000 \
  --tensor-parallel-size 2 \
  --distributed-executor-backend ray
```

**rank-N worker 脚本**:
```bash
export NCCL_SOCKET_IFNAME=eth0
export GLOO_SOCKET_IFNAME=eth0
# 等待 head GCS 可达
for i in $(seq 1 60); do
  python3 -c "import socket; s=socket.socket(); s.settimeout(2); \
    s.connect(('infer-0.infer-hl',6379)); s.close()" 2>/dev/null && break
  sleep 5
done
exec ray start --address=infer-0.infer-hl:6379 --num-gpus=1 --block
```

### 4. `app/engines/vllm_adapter.py` — vLLM DP

**rank-0**:
```bash
exec python3 -m vllm.entrypoints.openai.api_server \
  --model /models/<MODEL_NAME> \
  --host 0.0.0.0 --port 17000 \
  --data-parallel-address infer-0.infer-hl \
  --data-parallel-rpc-port 13355 \
  --data-parallel-size 2 \
  --data-parallel-size-local 1
```

**rank-N**:
```bash
exec python3 -m vllm.entrypoints.openai.api_server \
  --model /models/<MODEL_NAME> \
  --data-parallel-address infer-0.infer-hl \
  --data-parallel-rpc-port 13355 \
  --data-parallel-size 2 \
  --data-parallel-size-local 1 \
  --headless \
  --data-parallel-start-rank 1
```

### 5. `app/engines/sglang_adapter.py`

```bash
# rank-0
exec python3 -m sglang.launch_server \
  --model-path /models/<MODEL_NAME> \
  --host 0.0.0.0 --port 17000 \
  --nnodes 2 --node-rank 0 \
  --dist-init-addr infer-0.infer-hl:28030

# rank-N
exec python3 -m sglang.launch_server \
  --model-path /models/<MODEL_NAME> \
  --host 0.0.0.0 \
  --nnodes 2 --node-rank 1 \
  --dist-init-addr infer-0.infer-hl:28030
```

---

## 镜像构建命令

```bash
# 在 infer-control-sidecar-main/ 目录执行
docker build \
  -f backend-dist-nv-20260303/Dockerfile \
  -t wings-infer:dist-nv-zhanghui-20260303 \
  .

# 同步到第二台机器
docker save wings-infer:dist-nv-zhanghui-20260303 \
  | ssh root@7.6.16.150 docker load
```

---

## 环境变量对照表（K8s Pod）

| 变量名 | rank-0 取值 | rank-N 取值 |
|--------|------------|------------|
| `NODE_RANK` | `0` | `1` |
| `NNODES` | `2` | `2` |
| `HEAD_NODE_ADDR` | `infer-0.infer-hl.wings-verify-dist.svc.cluster.local` | （同） |
| `DISTRIBUTED` | `true` | `true` |
| `DISTRIBUTED_EXECUTOR_BACKEND` | `ray` / `dp_deployment` / `` | （同） |
| `ENGINE` | `vllm` / `sglang` | （同） |
| `MODEL_NAME` | 实际模型名 | （同） |
| `ENGINE_PORT` | `17000` | `17000` |
| `PORT` | `18000` | — （worker 不暴露代理） |
