# Wings Sidecar 迁移方案 v6（最大复用版）

更新时间：`2026-02-26`
基线文档：`design-proposal-v4.md`
实施清单：`design-proposal-v6-file-mapping.md`

---

## 1. 背景与目标

当前 `wings` 与 `infer-control-sidecar-main` 两个项目都可独立运行。  
本次迁移目标不是重写，而是针对**单机 vLLM 解耦场景**，在保证可落地验证的前提下做到：

1. 控制层尽最大可能复用 `wings` 现有实现（文件级复用优先）。
2. k8s 部分直接复用 `infer-control-sidecar-main` 现有配置（首轮验证不改配置习惯）。
3. 保持 v4 的 Launcher 架构：`main.py` 只做编排，不承载业务 API。

---

## 2. 硬约束（v6）

1. 场景约束：
- 单机、非分布式
- 仅 vLLM

2. 端口约束：
- 引擎后端固定 `17000`
- 业务入口固定 `18000`
- 探针入口固定 `19000`

3. 角色约束：
- Launcher 只做参数解析、命令下发、子进程监管、信号清理
- 业务 API 由 proxy 提供
- 健康检查由 health_service 提供

4. 复用约束：
- **优先直接复用文件，不做等价重写**
- 仅允许“最小适配改造”（import 路径、环境变量注入、启动参数冲突规避）

---

## 3. 复用策略（文件级）

## 3.1 控制层（优先复用 wings）

以下内容以 `F:\zhanghui\wings-k8s\wings\wings` 为主来源：

- 参数与语义来源：
  - `wings_start.sh`
  - `wings.py`
- 控制层核心：
  - `core/config_loader.py`
  - `core/hardware_detect.py`
- 引擎适配：
  - `engines/vllm_adapter.py`
- 配置映射：
  - `config/engine_parameter_mapping.json`
  - `config/vllm_default.json`
- 公共工具：
  - `utils/device_utils.py`
  - `utils/file_utils.py`
  - `utils/process_utils.py`

改造原则：
- 保持原逻辑和参数语义，不做行为重写。
- 仅做 sidecar 目录结构与调用入口所需的薄适配。

## 3.2 Proxy/Health（整包复用 wings/proxy）

直接复用目录：`wings/wings/proxy/*`

最小改造范围：
- import 路径从 `wings.proxy` 适配到 sidecar 内部包路径
- `settings.py` 规避 `argparse.parse_args()` 与 launcher 参数冲突（改为 env 优先或隔离解析）
- 注入 `BACKEND_URL=http://127.0.0.1:17000`

禁止事项：
- 不重写 `gateway.py` 核心转发逻辑
- 不重写 `health.py` 状态机核心逻辑

## 3.3 k8s（直接复用 infer-control-sidecar-main）

直接复用路径：`F:\zhanghui\wings-k8s\infer-control-sidecar-main\infer-control-sidecar-main\k8s`

首轮验证策略：
- 保持 deployment/service 配置风格与已有变量命名
- 尽量不改清单结构，避免引入“配置差异导致的验证偏差”

---

## 4. v6 目标进程模型（保持 v4）

```text
container entrypoint
  -> python -m app.main   # launcher
       -> 解析参数（对齐 wings_start.sh）
       -> 生成并写 /shared-volume/start_command.sh
       -> 启动 proxy（18000，复用 wings/proxy）
       -> 启动 health（19000，复用 wings/proxy/health_service.py）
       -> 前台守护 + 信号清理
```

说明：
- launcher 不直接承载业务路由；
- 对 `127.0.0.1:17000/health` 的等待与探测下沉到 health 进程。

---

## 5. 目录落地建议（sidecar 工程）

建议以 `infer-control-sidecar-main/backend/app` 为落地目录，按来源分层：

- `app/main.py`：新增 launcher 入口（少量新代码）
- `app/proxy/*`：从 `wings/wings/proxy/*` 直接复制并最小适配
- `app/core/*`：优先复用 `wings/wings/core/*`，仅补 sidecar 接口层
- `app/engines/vllm_adapter.py`：优先复用 `wings` 版本
- `app/config/*`：优先复用 `wings/config` 的 vllm 映射和默认配置
- `app/utils/*`：优先复用 `wings/utils` 对应文件

---

## 6. 参数与端口规则（不变）

1. 输入优先级：CLI > ENV > 默认值  
2. `model_name` 必填；未知参数按 v4 约束处理  
3. `ENABLE_REASON_PROXY=true` 时：
- `BACKEND_PORT=17000`
- `PROXY_PORT=PORT or 18000`
- `HEALTH_PORT=19000`

---

## 7. 验证策略（按你现有配置习惯）

## 7.1 首轮验证目标

- 用 `infer-control-sidecar-main` 现有 k8s 配置习惯部署
- 验证“解耦 + 最大复用”链路成立，而不是验证重写代码

## 7.2 最小验证步骤

1. 启动后确认共享卷出现 `/shared-volume/start_command.sh`  
2. 确认 `18000` 可用（proxy 业务入口）  
3. 确认 `19000/health` 可用（health 进程）  
4. 确认 health 内部持续探测 `127.0.0.1:17000/health`  
5. 发送 `SIGTERM`，确认 launcher/proxy/health 可被统一回收

---

## 8. 风险与控制

1. `proxy/settings.py` 参数冲突风险  
控制：禁止 import 时消费 launcher argv，统一 env 驱动。

2. “看似复用，实则重写”风险  
控制：新增文档级“来源清单”，每个文件标注来源与改动点。

3. k8s 偏差风险  
控制：首轮验证不改 `infer-control-sidecar-main` 的既有配置风格。

---

## 9. v6 验收标准

1. 文件级复用验收：
- 控制层、proxy、health 的关键模块以 `wings` 原文件为主；
- 新写代码仅限 launcher 编排与必要适配层。

2. 部署一致性验收：
- k8s 使用 `infer-control-sidecar-main` 现有清单风格完成部署验证。

3. 行为验收：
- 17000/18000/19000 端口行为符合 v4 描述；
- launcher 不承载业务 API；
- 信号回收行为正确。

---

## 10. 结论

v6 相对 v4 的核心升级是将“复用策略”从原则提升为硬约束：

- 从“目录级复用”升级为“文件级最大复用”；
- 控制层以 `wings` 为主，k8s 以 `infer-control-sidecar-main` 为主；
- 先复用、后适配，确保后续上机验证与现有配置习惯一致。
