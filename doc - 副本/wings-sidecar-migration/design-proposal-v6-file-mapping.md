# Wings Sidecar v6 文件映射清单（最大复用实施版）

关联文档：`design-proposal-v6-max-reuse.md`  
目标：按“文件级最大复用”实施单机 vLLM 解耦（Launcher 模式）

---

## 1. 路径约定

源项目（控制层与代理复用源）：
- `F:\zhanghui\wings-k8s\wings\wings`

目标项目（实施承载）：
- `F:\zhanghui\wings-k8s\infer-control-sidecar-main\infer-control-sidecar-main\backend\app`

k8s 复用源与目标（同项目内）：
- `F:\zhanghui\wings-k8s\infer-control-sidecar-main\infer-control-sidecar-main\k8s`

---

## 2. 文件映射（源 -> 目标 -> 改动点）

说明：
- 复用方式：`直接复制` / `保留原有` / `薄适配重写`
- 改动点仅允许最小范围（import 路径、配置注入、入口编排）

| ID | 源文件 | 目标文件 | 复用方式 | 改动点（最小） |
|---|---|---|---|---|
| C-01 | `wings/wings/wings_start.sh`（语义） | `backend/app/main.py` | 薄适配重写 | 仅实现 launcher 编排：参数解析、写 `/shared-volume/start_command.sh`、拉起 proxy/health、信号清理 |
| C-02 | `wings/wings/wings.py`（参数语义） | `backend/app/core/wings_entry.py` | 薄适配重写 | 保持参数决策语义，去除非 vLLM/非单机场景 |
| C-03 | `wings/wings/core/config_loader.py` | `backend/app/core/config_loader.py` | 直接复制 | import 路径改为 `app.*` |
| C-04 | `wings/wings/core/hardware_detect.py` | `backend/app/core/hardware_detect.py` | 直接复制 | import 路径改为 `app.*` |
| C-05 | `wings/wings/core/engine_manager.py` | `backend/app/core/engine_manager.py` | 直接复制 | import 路径改为 `app.*`，保留单机场景必要逻辑 |
| C-06 | `wings/wings/engines/vllm_adapter.py` | `backend/app/engines/vllm_adapter.py` | 直接复制 | import 路径改为 `app.*`，固定 backend 端口 17000 |
| C-07 | `wings/wings/config/engine_parameter_mapping.json` | `backend/app/config/engine_parameter_mapping.json` | 直接复制 | 无逻辑改动 |
| C-08 | `wings/wings/config/vllm_default.json` | `backend/app/config/vllm_default.json` | 直接复制 | 无逻辑改动 |
| C-09 | `wings/wings/utils/device_utils.py` | `backend/app/utils/device_utils.py` | 直接复制 | import 路径改为 `app.*`（如有） |
| C-10 | `wings/wings/utils/file_utils.py` | `backend/app/utils/file_utils.py` | 直接复制 | 若冲突，保留 atomic write 能力 |
| C-11 | `wings/wings/utils/process_utils.py` | `backend/app/utils/process_utils.py` | 直接复制 | import 路径改为 `app.*`（如有） |
| C-12 | `wings/wings/utils/env_utils.py`（可选） | `backend/app/utils/env_utils.py` | 直接复制 | 供参数/env 兼容使用 |
| C-13 | （新增） | `backend/app/core/start_args_compat.py` | 薄适配重写 | 对齐 `wings_start.sh` 参数集合、默认值与校验（未知参数失败） |
| C-14 | （新增） | `backend/app/core/port_plan.py` | 薄适配重写 | 固化 `ENABLE_REASON_PROXY` 端口派生规则（17000/18000/19000） |
| C-15 | `infer-control ... /app/config/settings.py` | `backend/app/config/settings.py` | 保留原有+薄适配 | 增补 launcher 运行所需 env 字段，不改原部署习惯 |

---

## 3. Proxy / Health 整包复用映射

| ID | 源文件 | 目标文件 | 复用方式 | 改动点（最小） |
|---|---|---|---|---|
| P-01 | `wings/wings/proxy/__init__.py` | `backend/app/proxy/__init__.py` | 直接复制 | 无 |
| P-02 | `wings/wings/proxy/gateway.py` | `backend/app/proxy/gateway.py` | 直接复制 | import `wings.proxy` -> `app.proxy` |
| P-03 | `wings/wings/proxy/health.py` | `backend/app/proxy/health.py` | 直接复制 | import `wings.proxy` -> `app.proxy` |
| P-04 | `wings/wings/proxy/health_service.py` | `backend/app/proxy/health_service.py` | 直接复制 | import `wings.proxy` -> `app.proxy` |
| P-05 | `wings/wings/proxy/http_client.py` | `backend/app/proxy/http_client.py` | 直接复制 | import 路径适配 |
| P-06 | `wings/wings/proxy/queueing.py` | `backend/app/proxy/queueing.py` | 直接复制 | import 路径适配 |
| P-07 | `wings/wings/proxy/settings.py` | `backend/app/proxy/settings.py` | 直接复制+薄适配 | 避免 import 时 `parse_args()` 消费 launcher argv（改 env 优先或 `parse_known_args`） |
| P-08 | `wings/wings/proxy/speaker_logging.py` | `backend/app/proxy/speaker_logging.py` | 直接复制 | import 路径适配 |
| P-09 | `wings/wings/proxy/tags.py` | `backend/app/proxy/tags.py` | 直接复制 | import 路径适配 |
| P-10 | `wings/wings/proxy/simple_proxy.py`（可选） | `backend/app/proxy/simple_proxy.py` | 直接复制 | 非主路径，可保留 |

强制注入环境：
- `BACKEND_URL=http://127.0.0.1:17000`
- `PORT=18000`
- `HEALTH_SERVICE_PORT=19000`

---

## 4. 目标项目现有文件处理策略

| ID | 现有文件（目标项目） | 处理方式 | 说明 |
|---|---|---|---|
| T-01 | `backend/app/main.py` | 替换为 launcher | 不再作为 FastAPI 业务服务入口 |
| T-02 | `backend/app/api/routes.py` | 保留但不作为主路径 | v4 下业务入口由 proxy 提供 |
| T-03 | `backend/app/services/command_builder.py` | 废弃/移除引用 | 由 `engines/vllm_adapter.py` 承接 |
| T-04 | `backend/app/services/engine_manager.py` | 保留或薄改 | 作为 launcher 协调层，不直接承载业务 API |
| T-05 | `backend/app/services/proxy_service.py` | 可保留 | 非主链路，避免影响旧逻辑 |
| T-06 | `backend/app/utils/http_client.py` | 保留 | 仅在需要时使用 |
| T-07 | `backend/app/utils/file_utils.py` | 与 wings 版择一 | 以满足共享卷命令写入可靠性为准 |

---

## 5. k8s 文件复用映射（原样优先）

| ID | 文件 | 复用方式 | 说明 |
|---|---|---|---|
| K-01 | `k8s/deployment.yaml` | 原样复用优先 | 仅调整镜像/tag/必要 env，不改结构风格 |
| K-02 | `k8s/service.yaml` | 原样复用优先 | 业务入口保持 18000 |
| K-03 | `k8s/deployment-sglang.yaml` | 保留不启用 | v6 MVP 仅 vLLM |

---

## 6. 最小改动白名单（防止“伪复用”）

允许改动：
1. import 路径适配（`wings.proxy` -> `app.proxy` 等）
2. 配置注入适配（env 字段补齐）
3. launcher 入口与子进程编排代码
4. 与单机 vLLM 无关代码的开关关闭（非删除优先）

禁止改动：
1. 重写 `proxy/gateway.py` 核心转发逻辑
2. 重写 `proxy/health.py` 状态机核心逻辑
3. 以“等价新实现”替代已有可复用文件

---

## 7. 实施顺序（建议）

1. 先完成 Proxy/Health 整包复制与 import 适配  
2. 再完成控制层 core/engines/config/utils 文件复制  
3. 最后替换 `app/main.py` 为 launcher 并接通进程编排  
4. 使用现有 k8s 清单进行首轮上机验证

---

## 8. 验收检查清单（执行时打勾）

- [ ] `start_command.sh` 由 launcher 写入 `/shared-volume`
- [ ] proxy 在 `18000` 提供业务入口
- [ ] health 在 `19000` 提供探针入口
- [ ] health 内部持续探测 `127.0.0.1:17000/health`
- [ ] `SIGTERM` 可回收 launcher/proxy/health 子进程
- [ ] 文件级复用占主导（新增代码仅为适配层）

