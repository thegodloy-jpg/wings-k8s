# Wings Sidecar 迁移方案 v5-review-fix（解耦契约补强版）

更新时间戳：`20260226-181820`

## 1. 修订目标

本版用于修复 v5 审查问题，目标是让方案具备工程可落地性：

- 明确控制层与执行层的“单写者”职责，避免共享卷竞态。
- 固化共享卷协议版本与必填字段，保证可演进。
- 补齐执行层工程形态、部署假设与网络前提。
- 明确产物优先级与 readiness 探针可执行方案。

当前仍为方案设计阶段，不改代码。

---

## 2. 范围与边界（MVP）

- 单机、非分布式。
- 仅 vLLM。
- 端口固定：`17000`（引擎后端）、`18000`（对外业务）、`19000`（健康探针）。
- 继续优先复用 `wings/wings/proxy`（业务代理 + health 逻辑）。

---

## 3. 部署与工程形态（新增明确）

### 3.1 同 Pod、同网络命名空间假设

- 控制层（wings-infer）、引擎执行层、proxy/health 运行在同一 Pod。
- 共享同一网络命名空间，因此 `127.0.0.1:17000` 可达。
- 共享卷路径固定为 `/shared-volume/engine`。

### 3.2 引擎执行层（engine executor）

定义为独立进程/容器（同 Pod sidecar），职责为：

- 监听共享卷命令产物。
- 拉起 vLLM 并监听 `17000`。
- 统一回写运行状态与错误信息。
- 负责引擎进程生命周期（启动、退出、重启策略）。

执行层必须与控制层版本匹配共享卷协议版本。

---

## 4. 控制层职责（收敛）

控制层只做：

- 参数解析与配置合并（对齐 `wings_start.sh` 语义）。
- 拼装命令并写共享卷。
- 启动/监管 proxy（18000）与 health（19000）。
- 生命周期与信号处理。

控制层不做：

- 直接拉起引擎进程。
- 虚拟环境激活（`source`/`conda activate`）。

---

## 5. 共享卷协议（补强）

### 5.1 目录约定

共享卷路径固定：

- `/shared-volume/engine/start_command.json`
- `/shared-volume/engine/start_command.sh`（兼容）
- `/shared-volume/engine/runtime.env`（可选）
- `/shared-volume/engine/status.json`
- `/shared-volume/engine/engine.pid`
- `/shared-volume/engine/last_error.log`
- `/shared-volume/engine/desired_state.json`（可选）

### 5.2 单写者规则（强制）

- 控制层：只写 `start_command.json`、`start_command.sh`、`runtime.env`、`desired_state.json`。
- 执行层：只写 `status.json`、`engine.pid`、`last_error.log`。

任何一方不得覆盖对方写入的文件。

### 5.3 版本与必填字段

`start_command.json` 必须包含：

- `schema_version`（示例：`1`）
- `request_id`
- `created_at`
- `engine`（固定 `vllm`）
- `backend_port`（固定 `17000`）
- `argv`（数组）
- `env`
- `work_dir`
- `health_url`
- `startup_timeout_sec`
- `log_path`

`status.json` 必须包含：

- `schema_version`
- `phase`
- `updated_at`
- `pid`
- `reason`
- `exit_code`

### 5.4 状态机（执行层单写）

合法迁移顺序：

`accepted` → `starting` → `running` → `ready` → `stopped`

失败路径：

`accepted/starting/running` → `failed`

控制层不写 `status.json`，只读。

---

## 6. 产物优先级（新增明确）

优先级规则（执行层必须遵循）：

1. 若存在 `start_command.json`，以其为唯一权威命令来源。
2. `runtime.env` 只用于补充环境变量，不覆盖 `start_command.json` 中显式设置。
3. `start_command.sh` 仅在 `start_command.json` 缺失时作为兼容兜底。

禁止同时混用 `json` 与 `sh` 的命令语义。

---

## 7. 原子写入规则（控制层）

控制层写共享卷时必须：

1. 先写临时文件（例如 `start_command.json.tmp`）。
2. `fsync` 后原子 `rename` 为正式文件名。
3. 最后写 `start_command.json` 成功标志（文件完成即视为可消费）。

执行层只消费完整文件，不读取 `.tmp`。

---

## 8. 健康与探针策略（可执行）

### 8.1 探针入口

- `19000` 为唯一探针入口。
- `health_service + health.py` 内部周期探测 `127.0.0.1:17000/health`。

### 8.2 readiness（推荐 exec 方案）

原因：复用 health 的冷启动 `201` 会被 `httpGet` 误判成功。

示例（Python 依赖，控制层容器已内置）：

```bash
python - <<'PY'
import json, sys, urllib.request
try:
    data = json.load(urllib.request.urlopen("http://127.0.0.1:19000/health", timeout=1))
    ok = (data.get("p") == "ready" and data.get("backend_ok") is True)
    sys.exit(0 if ok else 1)
except Exception:
    sys.exit(1)
PY
```

liveness 仍可使用 `httpGet /health`。

---

## 9. 终止与回收（新增建议）

控制层可通过 `desired_state.json` 请求停止：

- `action`: `stop`
- `request_id`
- `reason`

执行层收到后应：

- 优雅终止引擎进程
- 更新 `status.json` 为 `stopped`

---

## 10. 示例产物（禁用 venv 激活）

`start_command.sh`（兼容）：

```bash
#!/usr/bin/env bash
set -euo pipefail
exec vllm serve /weights --host 0.0.0.0 --port 17000 --served-model-name ${MODEL_NAME}
```

`start_command.json`（推荐）：

```json
{
  "schema_version": 1,
  "request_id": "req-20260226-001",
  "created_at": "2026-02-26T18:18:20+08:00",
  "engine": "vllm",
  "backend_port": 17000,
  "argv": ["vllm", "serve", "/weights", "--host", "0.0.0.0", "--port", "17000"],
  "env": {"MODEL_NAME": "qwen2.5-7b-instruct"},
  "work_dir": "/workspace",
  "health_url": "http://127.0.0.1:17000/health",
  "startup_timeout_sec": 600,
  "log_path": "/shared-volume/engine/engine.log"
}
```

---

## 11. 最小可验证流程（MVP）

1. 控制层写入 `start_command.json`，执行层开始消费。
2. 执行层创建 `status.json` 并进入 `accepted/starting`。
3. 引擎就绪后 `status.json` 变为 `ready`，`19000/health` 返回 ready 语义。
4. 业务流量从 `18000` 转发到 `17000` 正常。
5. 终止时 `desired_state.json` 或 SIGTERM 触发回收，`status.json` 更新为 `stopped`。

---

## 12. 结论

v5-review-fix 将解耦方案补足为“工程可执行”版本：

- 共享卷协议可版本化演进。
- 单写者规则消除竞态。
- readiness 可执行且可解释。
- 执行层工程形态明确，方案落地风险显著降低。
