# Wings Sidecar 迁移方案 v3.1（main.py 主入口 + 参数逻辑对齐 wings_start.sh）

## 1. 目标

本版在 v3 基础上新增一条硬约束：
- `main.py` 仍然是主入口，但输入参数语义必须和 `wings_start.sh` 对齐。

同时保持以下约束：
- 引擎端口：`17000`
- 对外业务端口：`18000`
- 健康检查端口：`19000`
- 优先直接复用 `F:\zhanghui\wings-k8s\wings\wings\proxy`

MVP 范围：单机、非分布式、仅 vLLM。

---

## 2. 主入口模型

- 主入口进程：`app.main:app`
- `main.py` 在 `lifespan` 内完成：
  1. 解析并标准化输入参数（与 `wings_start.sh` 对齐）
  2. 执行控制平面流程（决策 + 配置 + 拼命令）
  3. 写 `/shared-volume/start_command.sh`
  4. 轮询 `127.0.0.1:17000/health`

业务网关与健康面采用双服务：
- `app.proxy.gateway:app` 监听 `18000`（业务入口）
- `app.health_server:app` 监听 `19000`（K8s 探针）

---

## 3. 输入参数对齐设计（核心）

## 3.1 参数集合对齐

兼容 `wings_start.sh` 现有参数集合（保持同名语义）：

- 基础参数
  - `--host`
  - `--port`
  - `--model-name`
  - `--model-path`
  - `--engine`
  - `--input-length`
  - `--output-length`
  - `--config-file`
  - `--gpu-usage-mode`
  - `--device-count`
  - `--model-type`
  - `--save-path`

- vLLM 常用参数
  - `--trust-remote-code`
  - `--dtype`
  - `--kv-cache-dtype`
  - `--quantization`
  - `--quantization-param-path`
  - `--gpu-memory-utilization`
  - `--enable-chunked-prefill`
  - `--block-size`
  - `--max-num-seqs`
  - `--seed`
  - `--enable-expert-parallel`
  - `--max-num-batched-tokens`
  - `--enable-prefix-caching`

- 特性开关
  - `--enable-speculative-decode`
  - `--speculative-decode-model-path`
  - `--enable-rag-acc`
  - `--enable-auto-tool-choice`

- 分布式参数（MVP 只做解析兼容，不执行）
  - `--distributed`

## 3.2 输入通道与优先级

为兼容脚本式输入，定义两条输入通道：

1. `WINGS_START_ARGS`（推荐）
- 形态：原始参数串，例如：
  - `--model-name xxx --model-path /weights/xxx --dtype bfloat16`
- 处理：`shlex.split` 后按与 `wings_start.sh` 同构的 `argparse` 解析。

2. 结构化环境变量（兜底）
- 例如：`MODEL_NAME`、`MODEL_PATH`、`DTYPE`、`MAX_NUM_SEQS` 等。

优先级规则：
- `WINGS_START_ARGS` > 结构化环境变量 > 默认值

## 3.3 默认值与必填校验对齐

默认值对齐 `wings_start.sh`：
- `MODEL_PATH=/weights`
- `SAVE_PATH=/opt/wings/outputs`
- `PORT` 默认 `18000`（参与端口派生）

必填校验对齐：
- `model_name` 必填，缺失直接启动失败。

未知参数处理对齐：
- 遇到未知参数直接失败（与脚本 `usage` 行为一致）。

---

## 4. 端口派生逻辑（完全对齐 wings_start.sh）

引入同名开关：`ENABLE_REASON_PROXY`（默认 `true`）。

派生规则：

1. `ENABLE_REASON_PROXY=false`
- `BACKEND_PORT = PORT or 18000`
- 不启动业务 proxy
- 业务直接由后端服务暴露

2. `ENABLE_REASON_PROXY=true`（默认）
- `PROXY_PORT = PORT or 18000`
- `BACKEND_PORT = 17000`（固定）
- 对外业务走 proxy:18000 -> backend:17000

3. 健康端口（本方案新增硬约束）
- `HEALTH_PORT = 19000`（固定）
- K8s 探针只打 `19000/health`

---

## 5. 新增参数兼容模块

新增：`app/core/start_args_compat.py`

职责：
1. 构建与 `wings_start.sh` 同构的 parser。
2. 解析 `WINGS_START_ARGS` + env。
3. 输出标准化参数对象 `LaunchArgs`。
4. 执行端口派生，输出 `PortPlan`。

建议接口：

```python
@dataclass
class LaunchArgs:
    model_name: str
    model_path: str
    host: str | None
    port: int | None
    engine: str | None
    distributed: bool
    # ...其余对齐参数

@dataclass
class PortPlan:
    enable_proxy: bool
    backend_port: int  # 固定 17000 when proxy enabled
    proxy_port: int    # 默认 18000
    health_port: int   # 固定 19000


def resolve_launch_args() -> tuple[LaunchArgs, PortPlan]:
    ...
```

---

## 6. main.py 启动流程（更新后）

`main.py` 的 `lifespan` 调整为：

1. `launch_args, port_plan = resolve_launch_args()`
2. 将 `launch_args` 注入控制平面：
   - `wings_entry.resolve_engine_params(launch_args, port_plan)`
3. 生成 vLLM 命令时显式使用：
   - `--port 17000`
4. 写共享卷并等待 `127.0.0.1:17000/health`
5. 启动 / 检查 `proxy`（18000）与 `health`（19000）

---

## 7. 目录与改造清单

## 7.1 新增
- `app/core/start_args_compat.py`（参数对齐核心）
- `app/core/wings_entry.py`
- `app/core/config_loader.py`
- `app/core/hardware_detect.py`
- `app/engines/vllm_adapter.py`
- `app/proxy/*`（直接复用 wings/wings/proxy）
- `app/health_server.py`
- `app/config/vllm_default.json`
- `app/config/engine_parameter_mapping.json`

## 7.2 修改
- `app/main.py`（接入参数兼容层）
- `app/services/engine_manager.py`（改为消费标准化参数）
- `app/config/settings.py`（补充 `WINGS_START_ARGS/ENABLE_REASON_PROXY/PROXY_PORT/HEALTH_PORT/BACKEND_URL`）

## 7.3 删除
- `app/services/command_builder.py`

---

## 8. k8s 对齐要求

`deployment.yaml`：
- `wings-infer` 容器开放端口：`18000`, `19000`
- 探针：`/health` on `19000`
- 环境变量建议：
  - `WINGS_START_ARGS=--model-name ... --model-path ...`
  - `ENABLE_REASON_PROXY=true`
  - `PORT=18000`
  - `ENGINE_PORT=17000`
  - `HEALTH_PORT=19000`

`service.yaml`：
- 对外端口：`18000 -> targetPort 18000`

---

## 9. 验收用例（必须覆盖）

1. 默认 proxy 场景
- 输入：`ENABLE_REASON_PROXY=true`，未设置 `PORT`
- 期望：`proxy=18000`，`backend=17000`

2. 禁用 proxy 场景
- 输入：`ENABLE_REASON_PROXY=false`，未设置 `PORT`
- 期望：`backend=18000`

3. 业务端口覆盖场景
- 输入：`ENABLE_REASON_PROXY=true`，`PORT=18080`
- 期望：`proxy=18080`，`backend=17000`

4. 必填参数校验
- 输入：缺失 `--model-name`
- 期望：启动失败

5. 未知参数校验
- 输入：`--unknown-flag`
- 期望：启动失败

6. 引擎健康
- `curl http://127.0.0.1:17000/health` 返回 200

7. 健康面
- `curl http://127.0.0.1:19000/health` 返回 healthy

8. 业务面
- `curl http://<service>:18000/v1/chat/completions` 返回 200

---

## 10. 结论

本次方案已将“主入口仍为 `main.py`”与“参数逻辑对齐 `wings_start.sh`”合并：
- 对齐参数集合
- 对齐默认值与校验
- 对齐端口派生逻辑
- 保持 v3 的端口目标（17000/18000/19000）
- 同时维持 proxy 直接复用优先

这版可以直接作为后续代码改造的执行基线。
