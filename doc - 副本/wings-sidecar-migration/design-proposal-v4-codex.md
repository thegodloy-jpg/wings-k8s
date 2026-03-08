# Wings Sidecar 迁移方案 v4-codex（Launcher 模式，审查修订版）

## 1. 方案定位

本版明确采用 `Launcher` 模式：

- `main.py` 不再承担业务 API 服务职责。
- `main.py` 作为主函数入口，职责类似 `wings_start.sh`：
  - 解析输入参数（语义对齐 `wings_start.sh`）
  - 生成并写入共享卷启动命令
  - 启动/监管 proxy 进程
  - 启动/监管健康检查进程
  - 处理信号与清理

当前阶段仅做方案设计，不改代码。

---

## 0. 审查结论（Codex）

1. 探针语义风险（高）：复用 `wings/wings/proxy/health.py` 时，冷启动阶段 `/health` 可能返回 `201`，而 K8s `httpGet` 将 `2xx/3xx` 都视为成功，readiness 可能“提前就绪”。
2. 返回体字段不一致（中）：文档原描述 `engine_ready/proxy_healthy/status`，与复用 `health_service.py` 的实际字段（如 `p/backend_ok/backend_code/ever_ready`）不一致。
3. 启动责任描述不完整（中）：文档仅写“写共享卷命令”，未明确谁消费 `start_command.sh` 并拉起 `17000` 引擎进程。

---

## 2. 目标与边界

## 2.1 目标

1. 端口策略固定：
- 引擎后端：`17000`
- 对外业务：`18000`
- 健康探针：`19000`

2. 复用策略固定：
- 优先直接复用 `F:\zhanghui\wings-k8s\wings\wings\proxy`。

3. 参数策略固定：
- 输入参数语义与 `wings_start.sh` 对齐。

## 2.2 MVP 边界

- 单机、非分布式。
- 仅 vLLM。
- 不处理 sglang/mindie/wings 其他引擎路径。

---

## 3. 进程模型（v4）

```text
container entrypoint
  -> python -m app.main    # launcher
       -> 写 /shared-volume/start_command.sh
       -> 由既有执行器消费 start_command.sh 并拉起引擎（17000）
       -> 启动 proxy 进程（18000）
       -> 启动 health 进程（19000，内部轮询 127.0.0.1:17000/health）
       -> 前台守护 + 信号清理
```

说明：
- 业务 API 由 proxy 进程提供（18000）。
- K8s 探针只打健康进程（19000）。
- `127.0.0.1:17000/health` 的等待/探测逻辑下沉到 health 进程，launcher 不再阻塞等待。
- launcher 只做编排，不承载业务路由。

---

## 4. 参数对齐规范（对齐 wings_start.sh）

## 4.1 参数集合

与 `wings_start.sh` 对齐以下参数族：

- 基础参数：
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

- 推理参数：
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

- 特性开关：
  - `--enable-speculative-decode`
  - `--speculative-decode-model-path`
  - `--enable-rag-acc`
  - `--enable-auto-tool-choice`

- 分布式参数：
  - `--distributed`（MVP 解析兼容，执行时拒绝）

## 4.2 默认值与校验（对齐脚本语义）

- 默认值对齐：
  - `MODEL_PATH=/weights`
  - `SAVE_PATH=/opt/wings/outputs`
  - `PORT` 缺省按 `18000` 参与端口派生

- 校验对齐：
  - `model_name` 必填，缺失即失败。
  - 未知参数即失败（对齐 `wings_start.sh` 的 usage 逻辑）。

## 4.3 输入优先级

1. CLI 参数（容器启动参数）
2. 环境变量
3. 默认值

---

## 5. 端口派生规则（对齐 wings_start.sh）

保留同名开关：`ENABLE_REASON_PROXY`（默认 `true`）。

规则：

1. `ENABLE_REASON_PROXY=true`
- `BACKEND_PORT=17000`
- `PROXY_PORT = PORT or 18000`
- 对外：`PROXY_PORT`（默认 18000）

2. `ENABLE_REASON_PROXY=false`
- `BACKEND_PORT = PORT or 18000`
- 不启动 proxy（MVP 可直接判定为不支持或降级）

3. 健康端口
- `HEALTH_PORT=19000`（固定）

---

## 6. 模块职责拆分（v4）

## 6.1 Launcher（`app/main.py`）

负责：
- 参数解析
- 端口派生
- 调用控制平面生成命令
- 写共享卷
- 启动 proxy 子进程
- 启动 health 子进程
- 进程回收/信号处理

## 6.2 控制平面（`app/core/*`, `app/engines/*`）

负责：
- `wings_entry`：决策 + 配置合并
- `vllm_adapter`：拼命令（仅命令字符串）
- `engine_manager`：等待引擎就绪、状态管理

## 6.3 Proxy（`app/proxy/*`）

策略：
- 直接复用 `wings/wings/proxy` 代码
- 最小改造 import 与配置注入（`BACKEND_URL` 指向 `17000`）

## 6.4 Health（复用 `wings/wings/proxy/health_service.py`）

职责：
- 直接复用 `wings/wings/proxy/health_service.py` + `wings/wings/proxy/health.py`
- 暴露 `/health`（19000）
- 在 health 后台循环中持续探测 `127.0.0.1:17000/health` 并推进状态机
- 输出字段沿用复用实现（MVP 不重写字段语义），核心包括：
  - `p`（阶段，如 `ready/starting/start_failed/degraded`）
  - `backend_ok`、`backend_code`
  - `ever_ready`、`interrupted`、`pid_alive`
  - `code`（当前 health 响应码）

---

## 7. 启动时序

1. `main.py` 读取 CLI/env，完成参数标准化。
2. 计算端口计划（17000/18000/19000）。
3. 生成 vLLM 启动命令并写入 `/shared-volume/start_command.sh`。
4. 由既有执行器消费 `start_command.sh`，拉起引擎进程并监听 `17000`。
5. 启动 proxy 进程（18000）。
6. 启动 health 进程（19000，复用 `wings/wings/proxy/health_service.py`）。
7. 由 health 进程内部轮询 `http://127.0.0.1:17000/health`，对外返回阶段性健康状态。
8. 进入前台守护循环，接收 `SIGTERM/SIGINT` 并清理子进程。

---

## 8. 与现状差异（设计层）

当前：
- `main.py` 是 FastAPI 服务（含路由与 lifespan）。

v4 目标：
- `main.py` 是 launcher，不暴露业务 API。
- 业务 API 下沉到 `proxy` 进程（18000）。
- 探针 API 下沉到复用的 `proxy/health_service`（19000）。

---

## 9. K8s 清单目标形态

Deployment（wings-infer 容器）：
- 暴露端口：`18000`、`19000`
- env:
  - `ENGINE_PORT=17000`
  - `PORT=18000`
  - `HEALTH_PORT=19000`
  - `ENABLE_REASON_PROXY=true`

Probe：
- readiness（MVP 推荐）：
  - 使用 `exec` 探针，判定 `GET http://127.0.0.1:19000/health` 返回体中 `p=="ready"` 且 `backend_ok==true`
  - 原因：直接 `httpGet /health` 会把冷启动 `201` 也判为成功
- liveness: `GET /health` on `19000`

Service：
- `port: 18000`
- `targetPort: 18000`

---

## 10. 风险与对策

风险 1：`proxy/settings.py` import 即 parse args，可能与 launcher 参数冲突。  
对策：在复用时加一层适配（优先读取 env，避免 import 时消费 launcher argv）。

风险 2：launcher + proxy + health 三进程管理复杂。  
对策：明确父子进程树与退出策略；统一信号处理与日志前缀。

风险 3：`ENABLE_REASON_PROXY=false` 的行为在 sidecar 中未定义。  
对策：MVP 先固定 `ENABLE_REASON_PROXY=true`，关闭代理场景后续再支持。

风险 4：复用 health 状态码映射（含 `201`）与 K8s readiness `httpGet` 判定规则不一致。  
对策：MVP 将 readiness 改为 `exec` 探针按响应体字段判定；后续如需纯 `httpGet`，再做最小状态码适配。

---

## 11. 验收标准（方案层）

1. 参数兼容验收：
- 给定与 `wings_start.sh` 一致的参数串，launcher 解析结果一致。

2. 端口验收：
- 引擎健康：`127.0.0.1:17000/health`
- 业务入口：`<service>:18000`
- 探针入口：`127.0.0.1:19000/health`
- launcher 启动流程中无独立“阻塞等待 17000”步骤，由 19000 health 进程内部完成探测。
 - readiness 必须以“引擎真实就绪”为准（非仅 `2xx`）。

3. 复用验收：
- 业务转发逻辑来自 `wings/wings/proxy`，不重写核心路径。

4. 生命周期验收：
- `SIGTERM` 能结束 launcher/proxy/health 子进程并清理现场。

---

## 12. 结论

v4 将架构从“FastAPI 主服务”切换为“Launcher 主函数”：

- 符合你提出的 `main.py` 角色定位（对齐 `wings_start.sh`）。
- 保留 proxy 直接复用优先。
- 将 `17000/health` 等待逻辑明确收口到复用的 health 进程（19000）。
- 保持 `17000/18000/19000` 端口约束清晰可执行。
- 适合作为下一阶段代码改造基线。
