# vLLM Engine K8s Error Logs (No Accel)

Timestamp: 20260228-135852
Remote: 7.6.52.148
Namespace: wings-verify
Pod: wings-infer-b56d6b6d4-lpnlz
Mode: wings-infer only (without wings-accel)

## vllm-engine logs

```text
Waiting for start command...
Start command found, executing...
Waiting for engine to start on port 17000...
DEBUG 02-27 21:57:14 [plugins/__init__.py:35] No plugins for group vllm.platform_plugins found.
DEBUG 02-27 21:57:14 [platforms/__init__.py:36] Checking if TPU platform is available.
DEBUG 02-27 21:57:14 [platforms/__init__.py:55] TPU platform is not available because: No module named 'libtpu'
DEBUG 02-27 21:57:14 [platforms/__init__.py:61] Checking if CUDA platform is available.
DEBUG 02-27 21:57:14 [platforms/__init__.py:88] Exception happens when checking CUDA platform: NVML Shared Library Not Found
DEBUG 02-27 21:57:14 [platforms/__init__.py:105] CUDA platform is not available because: NVML Shared Library Not Found
DEBUG 02-27 21:57:14 [platforms/__init__.py:112] Checking if ROCm platform is available.
DEBUG 02-27 21:57:14 [platforms/__init__.py:126] ROCm platform is not available because: No module named 'amdsmi'
DEBUG 02-27 21:57:14 [platforms/__init__.py:133] Checking if XPU platform is available.
DEBUG 02-27 21:57:14 [platforms/__init__.py:153] XPU platform is not available because: No module named 'intel_extension_for_pytorch'
DEBUG 02-27 21:57:14 [platforms/__init__.py:160] Checking if CPU platform is available.
DEBUG 02-27 21:57:14 [platforms/__init__.py:230] No platform detected, vLLM is running on UnspecifiedPlatform
INFO 02-27 21:57:16 [triton_utils/importing.py:44] Triton is installed but 0 active driver(s) found (expected 1). Disabling Triton to prevent runtime errors.
INFO 02-27 21:57:16 [triton_utils/importing.py:68] Triton not installed or not compatible; certain GPU-related functions will not be available.
W0227 21:57:16.547000 8 torch/utils/cpp_extension.py:117] No CUDA runtime is found, using CUDA_HOME='/usr/local/cuda'
DEBUG 02-27 21:57:17 [entrypoints/utils.py:181] Setting VLLM_WORKER_MULTIPROC_METHOD to 'spawn'
DEBUG 02-27 21:57:17 [plugins/__init__.py:43] Available plugins for group vllm.general_plugins:
DEBUG 02-27 21:57:17 [plugins/__init__.py:45] - lora_filesystem_resolver -> vllm.plugins.lora_resolvers.filesystem_resolver:register_filesystem_resolver
DEBUG 02-27 21:57:17 [plugins/__init__.py:48] All plugins in this group will be loaded. Set `VLLM_PLUGINS` to control which plugins to load.
Traceback (most recent call last):
  File "<frozen runpy>", line 198, in _run_module_as_main
  File "<frozen runpy>", line 88, in _run_code
  File "/usr/local/lib/python3.12/dist-packages/vllm/entrypoints/openai/api_server.py", line 987, in <module>
    parser = make_arg_parser(parser)
             ^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.12/dist-packages/vllm/entrypoints/openai/cli_args.py", line 300, in make_arg_parser
    parser = AsyncEngineArgs.add_cli_args(parser)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.12/dist-packages/vllm/engine/arg_utils.py", line 2040, in add_cli_args
    parser = EngineArgs.add_cli_args(parser)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.12/dist-packages/vllm/engine/arg_utils.py", line 1153, in add_cli_args
    vllm_kwargs = get_kwargs(VllmConfig)
                  ^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.12/dist-packages/vllm/engine/arg_utils.py", line 346, in get_kwargs
    return copy.deepcopy(_compute_kwargs(cls))
                         ^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.12/dist-packages/vllm/engine/arg_utils.py", line 258, in _compute_kwargs
    default = default.default_factory()
              ^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.12/dist-packages/pydantic/_internal/_dataclasses.py", line 121, in __init__
    s.__pydantic_validator__.validate_python(ArgsKwargs(args, kwargs), self_instance=s)
  File "/usr/local/lib/python3.12/dist-packages/vllm/config/device.py", line 58, in __post_init__
    raise RuntimeError(
RuntimeError: Failed to infer device type, please set the environment variable `VLLM_LOGGING_LEVEL=DEBUG` to turn on verbose logging to help debug the issue.
```

## wings-infer logs (related)

```text
2026-02-28 05:57:09,397 [INFO] [launcher] Using static hardware context (detection disabled): {'device': 'nvidia', 'count': 1, 'details': [], 'units': 'GB'}
2026-02-28 05:57:09,397 [INFO] [launcher] Starting config loading and merging...
2026-02-28 05:57:09,397 [WARNING] [launcher] Cannot get VRAM details, skipping VRAM check
2026-02-28 05:57:09,397 [INFO] [launcher] Successfully loaded config file: /models/DeepSeek-R1-Distill-Qwen-1.5B/config.json
2026-02-28 05:57:09,398 [INFO] [launcher] Set global environment variable WINGS_ENGINE=vllm
2026-02-28 05:57:09,398 [INFO] [launcher] Determined default config file for hardware environment 'nvidia': /app/app/config/vllm_default.json
2026-02-28 05:57:09,398 [INFO] [launcher] Successfully loaded config file: /app/app/config/vllm_default.json
2026-02-28 05:57:09,398 [WARNING] [launcher] No model_deploy_config found for model_type= (engine=vllm), use minimal defaults
2026-02-28 05:57:09,398 [INFO] [launcher] Successfully loaded config file: /app/app/config/engine_parameter_mapping.json
2026-02-28 05:57:09,398 [INFO] [launcher] Config merging completed.
2026-02-28 05:57:09,398 [INFO] [launcher] start command written: /shared-volume/start_command.sh
2026-02-28 05:57:09,398 [INFO] [launcher] starting proxy: python -m uvicorn app.proxy.gateway:app --host 0.0.0.0 --port 18000 --log-level info
2026-02-28 05:57:09,398 [INFO] [launcher] starting health: python -m uvicorn app.proxy.health_service:app --host 0.0.0.0 --port 19000 --log-level info
2026-02-28 05:57:09,399 [INFO] [launcher] launcher running: backend=17000 proxy=18000 health=19000
INFO:     Started server process [8]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:19000 (Press CTRL+C to quit)
INFO:     10.42.0.1:51218 - "GET /health HTTP/1.1" 201 Created
INFO:     10.42.0.1:54068 - "GET /health HTTP/1.1" 201 Created
INFO:     10.42.0.1:39516 - "GET /health HTTP/1.1" 201 Created
INFO:     10.42.0.1:54166 - "GET /health HTTP/1.1" 201 Created
INFO:     10.42.0.1:33022 - "GET /health HTTP/1.1" 201 Created
INFO:     10.42.0.1:53830 - "GET /health HTTP/1.1" 201 Created
INFO:     10.42.0.1:53838 - "GET /health HTTP/1.1" 201 Created
INFO:     10.42.0.1:45408 - "GET /health HTTP/1.1" 201 Created
```

## probe outputs

```text
HEALTH_19000
{"s":0,"p":"starting","pid_alive":false,"backend_ok":false,"backend_code":0,"interrupted":false,"ever_ready":false,"cf":0,"lat_ms":2}
MODELS_18000
HTTP/1.1 502 Bad Gateway
date: Sat, 28 Feb 2026 05:58:51 GMT
server: uvicorn
content-length: 64
content-type: application/json

{"detail":"Backend unavailable: All connection attempts failed"}
CHAT_18000
HTTP/1.1 502 Bad Gateway
date: Sat, 28 Feb 2026 05:58:51 GMT
server: uvicorn
content-length: 66
content-type: application/json

{"detail":"backend connect error: All connection attempts failed"}
```
