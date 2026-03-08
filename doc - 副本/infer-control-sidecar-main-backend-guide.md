# infer-control-sidecar-main backend 目录说明

更新时间：`2026-02-26`

## 1. 文档范围

本文档用于说明以下目录的运行架构与模块职责：

- `F:/zhanghui/wings-k8s/infer-control-sidecar-main/infer-control-sidecar-main/backend`

内容基于当前 sidecar 迁移中的 launcher 模式实现。

## 2. 目录结构（概览）

- `backend/app/main.py`
  - launcher 主入口。
  - 负责参数解析、写共享卷启动命令、监管 proxy/health 子进程。
- `backend/app/core/*`
  - 控制层逻辑：参数兼容、端口规划、硬件/配置合并、启动计划组装。
- `backend/app/engines/vllm_adapter.py`
  - vLLM 启动命令构建器。
  - 在 launcher 模式下已禁用“直接拉起引擎进程”接口。
- `backend/app/proxy/*`
  - 复用的代理与健康检查实现。
- `backend/app/services/*`
  - 保留的历史兼容服务模块。
- `backend/app/utils/*`
  - 通用工具能力（文件、环境变量、进程、模型、HTTP、设备等）。
- `backend/app/config/*`
  - 参数映射与默认引擎配置。
- `backend/requirements.txt`
  - Python 运行依赖。

## 3. 运行调用链（主路径）

主路径入口：

- `python -m app.main`

当前调用顺序：

1. `app.main.run(argv)`  
   - 解析启动参数：`app.core.start_args_compat.parse_launch_args`
2. 派生端口：`app.core.port_plan.derive_port_plan`
3. 生成 launcher 计划：`app.core.wings_entry.build_launcher_plan`
4. `build_launcher_plan` 内部：
   - 硬件探测：`app.core.hardware_detect.detect_hardware`
   - 配置合并：`app.core.config_loader.load_and_merge_configs`
   - 生成 vLLM 命令：`app.engines.vllm_adapter.build_start_command`
5. 写共享卷启动产物：
   - `app.main._write_start_command(...)`
   - 默认目标：`/shared-volume/start_command.sh`
6. 拉起子进程：
   - Proxy 应用（`app.proxy.gateway:app`）
   - Health 应用（`app.proxy.health_service:app`）
7. 进入监管循环：
   - 若 proxy/health 异常退出，则自动重启
8. 接收 `SIGINT/SIGTERM`：
   - 回收子进程并优雅退出 launcher

## 4. config_loader 的调用方式

`config_loader.py` 不是由 `main.py` 直接调用。  
它在 `wings_entry.py` 的“启动计划构建阶段”被调用：

- `app.main.run(...)`
  -> `app.core.wings_entry.build_launcher_plan(...)`
  -> `app.core.config_loader.load_and_merge_configs(...)`

`load_and_merge_configs(...)` 的职责：

1. 规范化已解析的 CLI 参数。
2. 执行引擎/模型相关自动决策。
3. 按需加载用户配置文件。
4. 合并默认配置 + 用户配置 + CLI 覆盖项。
5. 返回最终参数，供命令构建使用。

## 5. 活跃模块与兼容模块

launcher 主链路中的活跃模块：

- `app/main.py`
- `app/core/start_args_compat.py`
- `app/core/port_plan.py`
- `app/core/wings_entry.py`
- `app/core/config_loader.py`
- `app/engines/vllm_adapter.py`（走 `build_start_command`）
- `app/proxy/gateway.py`
- `app/proxy/health.py`
- `app/proxy/health_service.py`

历史兼容模块（不在主链路）：

- `app/services/command_builder.py`
- `app/services/engine_manager.py`
- `app/services/proxy_service.py`
- `app/api/routes.py`
- `app/proxy/simple_proxy.py`

## 6. 关键默认配置

来自 `app/config/settings.py`：

- `SHARED_VOLUME_PATH=/shared-volume`
- `START_COMMAND_FILENAME=start_command.sh`
- `ENGINE_PORT=17000`
- `PORT=18000`
- `HEALTH_PORT=19000`
- `ENABLE_REASON_PROXY=true`（默认）
- `PROXY_APP=app.proxy.gateway:app`
- `HEALTH_APP=app.proxy.health_service:app`

## 7. launcher 模式下的引擎适配器契约

针对 `app/engines/vllm_adapter.py`：

- launcher 支持接口：`build_start_command(params) -> str`
- launcher 禁止接口：`start_engine(params)`（会抛出运行时异常）

该约束用于避免控制容器内直接起引擎进程，保持“命令产物解耦”。

## 8. 共享卷产物（当前实现）

当前 launcher 写入的核心产物：

- `/shared-volume/start_command.sh`

引擎 sidecar 负责监听并执行该产物。

## 9. backend 文件索引

更细粒度的逐文件说明见：

- `F:/zhanghui/wings-k8s/infer-control-sidecar-main/infer-control-sidecar-main/backend/FILE_COMMENT_INDEX.md`
