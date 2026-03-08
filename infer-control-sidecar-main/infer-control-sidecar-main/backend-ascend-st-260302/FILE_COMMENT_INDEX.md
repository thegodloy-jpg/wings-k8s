# Backend File Comment Index

This index complements in-file comments. JSON files are documented here because JSON does not support native comments.

> **目录说明（backend-20260228）**
> 本目录是 `backend/` 的清理迭代版本，在原有基础上：
> - **删除** `app/api/`（遗留 FastAPI 路由，实际代理由 `app/proxy/gateway.py` 承担）
> - **删除** `app/services/`（遗留服务层，已被 `app/core/` + `app/engines/` 取代）
> - **新增** `app/engines/sglang_adapter.py`（SGLang 引擎适配器，command-build only）
> - **新增** `app/engines/mindie_adapter.py`（Mindie 昇腾引擎适配器，含 config.json 生成）
> - **新增** `app/config/sglang_default.json`（SGLang 引擎级别 fallback 参数）
> - **新增** `app/config/mindie_default.json`（Mindie 引擎级别 fallback 参数）
> - **更新** `app/core/wings_entry.py`（支持 vllm/vllm_ascend/sglang/mindie 全引擎）
> - **更新** `app/core/engine_manager.py`（优先 `build_start_script`，fallback `build_start_command`）
> - **更新** `app/core/start_args_compat.py`（解除 vllm-only 限制）
> - **更新** `app/core/config_loader.py`（添加 sglang/mindie fallback 加载逻辑）

| File | Notes |
|---|---|
| app/\_\_init\_\_.py | Top-level package marker for backend application modules. |
| app/config/\_\_init\_\_.py | Configuration package marker. |
| app/config/engine_parameter_mapping.json | Parameter-name mapping table from default config keys to engine-specific keys (vllm / sglang / mindie). |
| app/config/settings.py | Environment-backed settings model shared by launcher, proxy, and health services. |
| app/config/vllm_default.json | Default vLLM runtime parameter values merged by config loader. Includes model-architecture-level configs for vllm/vllm_ascend/sglang/mindie engine keys. |
| app/config/sglang_default.json | Engine-level fallback defaults for SGLang (used when vllm_default.json has no model-specific sglang section). |
| app/config/mindie_default.json | Engine-level fallback defaults for Mindie Ascend (used when vllm_default.json has no model-specific mindie section). |
| app/core/\_\_init\_\_.py | Core orchestration package marker. |
| app/core/config_loader.py | Loads and merges configuration layers (defaults, mappings, CLI/env overrides). Supports vllm/vllm_ascend/sglang/mindie. Added `_load_engine_fallback_defaults`. |
| app/core/engine_manager.py | Resolves engine adapter module and builds start_command.sh script body. Prefers `build_start_script`; falls back to `build_start_command` + exec wrap. Alias: vllm_ascend → vllm_adapter. |
| app/core/hardware_detect.py | Detects hardware runtime characteristics to inform config selection. |
| app/core/port_plan.py | Derives deterministic backend/proxy/health port plan for launcher mode. |
| app/core/start_args_compat.py | Parses launcher CLI args. Supports engines: vllm, vllm_ascend, sglang, mindie. |
| app/core/wings_entry.py | Builds launcher plan by combining parsed args, hardware context, and merged config. Uses `engine_manager.start_engine_service` for all-engine dispatch. |
| app/engines/\_\_init\_\_.py | Engine adapter package marker. |
| app/engines/vllm_adapter.py | vLLM adapter: assembles command args + env fragments. Supports vllm and vllm_ascend. Implements `build_start_command` and `build_start_script`. |
| app/engines/sglang_adapter.py | SGLang adapter: assembles `python3 -m sglang.launch_server` command. Implements `build_start_command` and `build_start_script`. Migrated from wings/engines/sglang_adapter.py. |
| app/engines/mindie_adapter.py | Mindie (华为昇腾) adapter: generates full config.json + daemon start script. Implements `build_start_command` and `build_start_script`. Migrated from wings/engines/mindie_adapter.py. |
| app/main.py | Launcher entrypoint that orchestrates argument parsing, command artifact writing, and child-process supervision. |
| app/proxy/\_\_init\_\_.py | Proxy package export surface reused from wings implementation. |
| app/proxy/gateway.py | Primary business proxy app forwarding OpenAI-compatible requests to backend engine. |
| app/proxy/health.py | Health state machine that probes backend/proxy signals and computes readiness phase. |
| app/proxy/health_service.py | HTTP health service exposing aggregated status output for probe endpoints. |
| app/proxy/http_client.py | Proxy-specific async HTTP client helper for backend calls. |
| app/proxy/queueing.py | Queueing and concurrency primitives for controlling proxy request admission. |
| app/proxy/settings.py | Proxy runtime settings loader with safe argument parsing behavior. |
| app/proxy/simple_proxy.py | Simple passthrough proxy helper for low-overhead forwarding fallback. |
| app/proxy/speaker_logging.py | Per-request structured logging helper for proxy layer. |
| app/proxy/tags.py | OpenAI-compatible tag constants and type definitions used by proxy. |
| app/utils/\_\_init\_\_.py | Utilities package marker. |
| app/utils/device_utils.py | Device capability helpers (GPU count, memory, type detection). |
| app/utils/env_utils.py | Environment variable accessors with typed defaults. |
| app/utils/file_utils.py | File I/O helpers: safe_write_file, write_command_to_volume, JSON config loading. |
| app/utils/http_client.py | Shared async httpx client factory. |
| app/utils/model_utils.py | Model metadata identification (architecture, type, quantization) from model paths. |
| app/utils/noise_filter.py | Filters noisy/repetitive log patterns from engine stdout. |
| app/utils/process_utils.py | Subprocess management: PID logging, startup wait, log stream forwarding. |
| app/utils/wings_file_utils.py | Wings-specific file path and permission utilities. |
| requirements.txt | Python package dependencies for the backend application. |

| app/proxy/simple_proxy.py | Simplified proxy implementation retained for fallback and testing scenarios. |
| app/proxy/speaker_logging.py | Structured logging helpers for proxy request lifecycle, diagnostics, and tracing context. |
| app/proxy/tags.py | Tag constants and helper utilities shared across proxy components. |
| app/services/__init__.py | Legacy services package marker. |
| app/services/command_builder.py | Legacy command builder retained for backward compatibility with service-based startup flow. |
| app/services/engine_manager.py | Legacy async engine manager that writes shared-volume command artifacts and checks readiness. |
| app/services/proxy_service.py | Legacy proxy management helpers for startup checks and service lifecycle integration. |
| app/utils/__init__.py | Utility package marker. |
| app/utils/device_utils.py | Device-level helper methods for hardware capability checks and resource introspection. |
| app/utils/env_utils.py | Environment helper functions used by adapter and launcher config resolution. |
| app/utils/file_utils.py | Safe file IO helpers for shared-volume artifacts and general config file operations. |
| app/utils/http_client.py | Generic HTTP client helpers used by legacy service modules. |
| app/utils/model_utils.py | Model metadata parsing and architecture identification helpers. |
| app/utils/noise_filter.py | Log/output noise filtering helpers to improve runtime signal clarity. |
| app/utils/process_utils.py | Process helper methods for startup waiting, PID logging, and stream handling. |
| app/utils/wings_file_utils.py | Compatibility copy of wings file utility helpers retained during migration. |
| requirements.txt | Pinned Python dependencies for launcher/proxy/health runtime. |