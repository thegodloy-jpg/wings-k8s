# Backend File Comment Index

This index complements in-file comments. JSON files are documented here because JSON does not support native comments.

| File | Notes |
|---|---|
| app/__init__.py | Top-level package marker for backend application modules. |
| app/api/__init__.py | API subpackage marker for legacy route handlers. |
| app/api/routes.py | Legacy FastAPI route handlers kept for compatibility and migration safety. |
| app/config/__init__.py | Configuration package marker. |
| app/config/engine_parameter_mapping.json | Parameter-name mapping table from default config keys to engine-specific keys. |
| app/config/settings.py | Environment-backed settings model shared by launcher, proxy, and health services. |
| app/config/vllm_default.json | Default vLLM runtime parameter values merged by config loader. |
| app/core/__init__.py | Core orchestration package marker. |
| app/core/config_loader.py | Loads and merges configuration layers (defaults, mappings, CLI/env overrides). |
| app/core/engine_manager.py | Resolves engine adapter module and builds startup command strings. |
| app/core/hardware_detect.py | Detects hardware runtime characteristics to inform config selection. |
| app/core/port_plan.py | Derives deterministic backend/proxy/health port plan for launcher mode. |
| app/core/start_args_compat.py | Parses launcher CLI args with semantics aligned to wings_start.sh. |
| app/core/wings_entry.py | Builds launcher plan by combining parsed args, hardware context, and merged config. |
| app/engines/__init__.py | Engine adapter package marker. |
| app/engines/vllm_adapter.py | vLLM adapter responsible for assembling engine startup command arguments and env fragments. |
| app/main.py | Launcher entrypoint that orchestrates argument parsing, command artifact writing, and child-process supervision. |
| app/proxy/__init__.py | Proxy package export surface reused from wings implementation. |
| app/proxy/gateway.py | Primary business proxy app forwarding OpenAI-compatible requests to backend engine. |
| app/proxy/health.py | Health state machine that probes backend/proxy signals and computes readiness phase. |
| app/proxy/health_service.py | HTTP health service exposing aggregated status output for probe endpoints. |
| app/proxy/http_client.py | Proxy-specific async HTTP client helper for backend calls. |
| app/proxy/queueing.py | Queueing and concurrency primitives for controlling proxy request admission. |
| app/proxy/settings.py | Proxy runtime settings loader with safe argument parsing behavior. |
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