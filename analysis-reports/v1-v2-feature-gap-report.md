# v1 vs v2 功能差距报告（Feature Gap Report）

> **v1（目标）**: `infer-control-sidecar-unified/backend/app/`  
> **v2（源）**: `D:\project\wings-k8s-v2-260311\wings\`  
> **范围**: 仅列出 v2 存在但 v1 **尚未迁移** 的功能差距  
> **排除**: 用户已确认完成迁移的条目（wings_adapter, xllm_adapter, mmgm_utils, rag_acc, model_utils 新增, device_utils 新增, vllm_adapter 新增, start_args_compat 新增, gateway 新增端点, engines/\_\_init\_\_.py 等）

---

## 一、汇总统计

| 优先级 | 数量 | 说明 |
|--------|------|------|
| **P0** | 15   | 功能或正确性缺失，会导致特定场景无法工作 |
| **P1** | 8    | 重要辅助功能，缺失会影响可维护性或边缘场景 |
| **P2** | 5    | 低优先级：鲁棒性/小优化，或仅对非 sidecar 场景有意义 |

---

## 二、逐项差距清单

### P0 — 功能/正确性缺失

#### 1. `env_utils.py` — `get_soft_fp4_env()`
| 字段 | 内容 |
|------|------|
| v2 文件 | `utils/env_utils.py` |
| v1 目标文件 | `utils/env_utils.py` |
| 描述 | 读取 `ENABLE_SOFT_FP4` 环境变量（bool），用于 FP4 量化判断 |
| 影响 | `config_loader._set_soft_fp4()` 调用该函数；缺失则 Qwen3-32B-NVFP4 等模型无法自动启用 FP4 |

#### 2. `env_utils.py` — `get_speculative_decoding_env()`
| 字段 | 内容 |
|------|------|
| v2 文件 | `utils/env_utils.py` |
| v1 目标文件 | `utils/env_utils.py` |
| 描述 | 读取 `SD_ENABLE` 环境变量（bool），用于推测解码开关 |
| 影响 | `config_loader._set_spec_decoding_config()` 依赖此函数；缺失则推测解码配置无法生效 |

#### 3. `env_utils.py` — `get_sparse_env()`
| 字段 | 内容 |
|------|------|
| v2 文件 | `utils/env_utils.py` |
| v1 目标文件 | `utils/env_utils.py` |
| 描述 | 读取 `SPARSE_ENABLE` 环境变量（bool），用于稀疏 KV 缓存开关 |
| 影响 | `config_loader._set_sparse_config()` 依赖此函数；缺失则稀疏 KV 配置无法生效 |

#### 4. `config_loader.py` — `_set_spec_decoding_config(params)`
| 字段 | 内容 |
|------|------|
| v2 文件 | `core/config_loader.py` |
| v1 目标文件 | `core/config_loader.py` |
| 描述 | 根据 params 中的 speculative decoding 参数，设置 `SD_ENABLE` 环境变量 |
| 影响 | 推测解码模式无法通过 API 参数动态开启 |

#### 5. `config_loader.py` — `_set_sparse_config(params)`
| 字段 | 内容 |
|------|------|
| v2 文件 | `core/config_loader.py` |
| v1 目标文件 | `core/config_loader.py` |
| 描述 | 根据 params 中的 sparse 参数，设置 `SPARSE_ENABLE` 环境变量 |
| 影响 | 稀疏 KV 缓存模式无法通过 API 参数动态开启 |

#### 6. `config_loader.py` — `_set_soft_fp4(params, ctx, model_info)`
| 字段 | 内容 |
|------|------|
| v2 文件 | `core/config_loader.py` |
| v1 目标文件 | `core/config_loader.py` |
| 描述 | 检测 Qwen3-32B-NVFP4 模型（昇腾设备），自动配置 FP4 量化：设 quantization='fp4'、dtype='bfloat16'、max_num_seqs=64、enable_chunked_prefill=True |
| 影响 | Qwen3-32B-NVFP4 在昇腾设备上无法正确运行 |

#### 7. `config_loader.py` — `_set_function_call(params)`
| 字段 | 内容 |
|------|------|
| v2 文件 | `core/config_loader.py` |
| v1 目标文件 | `core/config_loader.py` |
| 描述 | 根据 `enable_auto_tool_choice` 参数启用/禁用函数调用（tool use）；设置 `--enable-auto-tool-choice` 和 `--tool-call-parser` |
| 影响 | 函数调用/自动工具选择功能无法通过参数开启 |

#### 8. `config_loader.py` — `_validate_embedding_rerank_params(params, ctx)`
| 字段 | 内容 |
|------|------|
| v2 文件 | `core/config_loader.py` |
| v1 目标文件 | `core/config_loader.py` |
| 描述 | 当模型为 embedding 或 rerank 类型时，强制禁用 `enable_chunked_prefill` 和 `enable_prefix_caching`（避免不兼容） |
| 影响 | embedding/rerank 模型可能因参数冲突而启动失败 |

#### 9. `config_loader.py` — `_merge_xllm_params(params, ctx, engine_cmd_parameter)`
| 字段 | 内容 |
|------|------|
| v2 文件 | `core/config_loader.py` |
| v1 目标文件 | `core/config_loader.py` |
| 描述 | xllm 引擎的参数合并函数，将默认配置参数映射为 xllm CLI 参数格式 |
| 影响 | xllm 引擎无法正确接收大多数配置参数（即使 xllm_adapter 已迁移） |

#### 10. `config_loader.py` — `_merge_cmd_params` 缺少参数映射
| 字段 | 内容 |
|------|------|
| v2 文件 | `core/config_loader.py` |
| v1 目标文件 | `core/config_loader.py` |
| 描述 | v2 的 `_merge_cmd_params` 包含 v1 缺失的 engine_cmd_parameter 键：`enable_speculative_decode`、`speculative_decode_model_path`、`enable_rag_acc`、`enable_auto_tool_choice`、`enable_sparse`、`lc_sparse_threshold`、`total_budget`、`local_kvstore_capacity` |
| 影响 | 推测解码、RAG 加速、函数调用、稀疏 KV、KV 预算等特性的参数无法传递给引擎 |

#### 11. `config_loader.py` — `_select_nvidia_engine` 缺少推测解码 / 稀疏检查
| 字段 | 内容 |
|------|------|
| v2 文件 | `core/config_loader.py` |
| v1 目标文件 | `core/config_loader.py` |
| 描述 | v2 的 `_select_nvidia_engine` 在引擎选择逻辑开头调用 `_set_spec_decoding_config` 和 `_set_sparse_config`；v1 缺失 |
| 影响 | NVIDIA 引擎选择不考虑推测解码/稀疏需求 |

#### 12. `config_loader.py` — `_select_ascend_engine` 缺少 xllm 和 env-aware 路由
| 字段 | 内容 |
|------|------|
| v2 文件 | `core/config_loader.py` |
| v1 目标文件 | `core/config_loader.py` |
| 描述 | v2 有完整的昇腾引擎选择链：mmum 模型→xllm Qwen3→env-aware vllm_ascend→默认 mindie。v1 缺少 `_check_qwen3_xllm_available()`、`_should_use_vllm_ascend_for_env()`、`_get_vllm_ascend_reason()`、`_is_vllm_ascend_only_model()`、`_should_use_xllm_for_qwen3()` 等辅助函数 |
| 影响 | 昇腾设备上 Qwen3 模型无法自动路由到 xllm；env-aware vllm_ascend 无法自动选择 |

#### 13. `config_loader.py` — `_validate_user_engine` 不支持 xllm / transformers
| 字段 | 内容 |
|------|------|
| v2 文件 | `core/config_loader.py` |
| v1 目标文件 | `core/config_loader.py` |
| 描述 | v2 的 `_validate_user_engine` 识别 'xllm' 和 'transformers' 引擎名称；v1 不识别 |
| 影响 | 用户显式指定 xllm 或 transformers 引擎时会被拒绝 |

#### 14. `config_loader.py` — `_set_soft_fp8` 扩展模型检测
| 字段 | 内容 |
|------|------|
| v2 文件 | `core/config_loader.py` |
| v1 目标文件 | `core/config_loader.py` |
| 描述 | v2 的 `_set_soft_fp8` 支持 Qwen3 系列模型（Qwen3-30B-A3B, Qwen3-235B-A22B, Qwen3-32B 等）+ DeepSeek 系列的 FP8 自动启用，而 v1 仅支持 DeepSeekV3 |
| 影响 | Qwen3 系列等新模型无法自动启用 FP8 量化 |

#### 15. `config_loader.py` — `_adjust_tensor_parallelism` 缺少 300I A2 卡检测
| 字段 | 内容 |
|------|------|
| v2 文件 | `core/config_loader.py` |
| v1 目标文件 | `core/config_loader.py` |
| 描述 | v2 在 `_adjust_tensor_parallelism` 中调用 `check_pcie_cards("d802", "4000")` 检测 300I A2 卡，并设置 `ASCEND_RT_VISIBLE_DEVICES` |
| 影响 | 300I A2 卡场景下张量并行可能配置错误 |

---

### P1 — 重要辅助功能

#### 16. `config_loader.py` — `_configure_mmum_sglang(ctx)`
| 字段 | 内容 |
|------|------|
| v2 文件 | `core/config_loader.py` |
| v1 目标文件 | `core/config_loader.py` |
| 描述 | MMUM（多模态）模型使用 SGLang 时，自动设置 `SGLANG_DISABLE_CUDNN_CHECK=1` 环境变量 |
| 影响 | MMUM + SGLang 组合可能因 cuDNN 检查而失败 |

#### 17. `config_loader.py` — `_handle_mindie_xllm_engine()` / `_handle_sglang_engine()`
| 字段 | 内容 |
|------|------|
| v2 文件 | `core/config_loader.py` |
| v1 目标文件 | `core/config_loader.py` |
| 描述 | 引擎验证函数：mindie/xllm 仅限昇腾设备、sglang 仅限 NVIDIA/单卡模式 |
| 影响 | 引擎/硬件组合验证不完整，用户可能在不支持的组合上尝试启动 |

#### 18. `config_loader.py` — `_handle_distributed` 缺少 xllm 校验
| 字段 | 内容 |
|------|------|
| v2 文件 | `core/config_loader.py` |
| v1 目标文件 | `core/config_loader.py` |
| 描述 | v2 在 `_handle_distributed` 中对 xllm 引擎抛出异常（不支持分布式）；v1 缺失此校验 |
| 影响 | xllm 引擎可能被错误地以分布式模式启动 |

#### 19. `config_loader.py` — `_handle_vllm_distributed` 缺少 DeepseekV32 检查
| 字段 | 内容 |
|------|------|
| v2 文件 | `core/config_loader.py` |
| v1 目标文件 | `core/config_loader.py` |
| 描述 | v2 检查 `DeepseekV32ForCausalLM`（除 V3 外），在分布式模式下补充 expert_parallel 检测 |
| 影响 | DeepSeekV3.2 分布式模式可能遗漏 expert_parallel 配置 |

#### 20. `config_loader.py` — MMGM 仅支持 hunyuan-video，缺少 qwen-image
| 字段 | 内容 |
|------|------|
| v2 文件 | `core/config_loader.py` |
| v1 目标文件 | `core/config_loader.py` |
| 描述 | v2 有 `_get_mmgm_model_name()`、`_check_hunyuan_model()`、`_check_qwen_model()`、`_build_mmgm_engine_config()` 支持 hunyuan-video 和 qwen-image 双模态模型。v1 的 `_build_mmgm_engine_defaults` 仅支持 hunyuan-video |
| 影响 | qwen-image 多模态部署不可用 |

#### 21. `config/engine_parameter_mapping.json` — 缺少 `default_to_xllm_parameter_mapping`
| 字段 | 内容 |
|------|------|
| v2 文件 | `config/engine_parameter_mapping.json` |
| v1 目标文件 | `config/engine_parameter_mapping.json` |
| 描述 | xllm 引擎的参数名映射（model_name→model_id, model_path→model, gpu_memory_utilization→max_memory_utilization 等） |
| 影响 | xllm 引擎参数转换无法工作（即使 `_merge_xllm_params` 被迁移） |

#### 22. `config/wings_default.json` — 整个文件缺失
| 字段 | 内容 |
|------|------|
| v2 文件 | `config/wings_default.json` |
| v1 目标文件 | (不存在) |
| 描述 | Wings 引擎的默认配置 JSON，包含模型路径、量化、并发数、chunked_prefill 等默认值 |
| 影响 | Wings 引擎作为通用 fallback 时缺少默认配置（如果 wings_adapter 需要读取此文件） |

#### 23. `config_loader.py` — `_auto_select_engine` 缺少 spec_decoding / sparse 调用
| 字段 | 内容 |
|------|------|
| v2 文件 | `core/config_loader.py` |
| v1 目标文件 | `core/config_loader.py` |
| 描述 | v2 的 `_auto_select_engine` 在完成引擎选择后调用 `_set_spec_decoding_config()` 和 `_set_sparse_config()` |
| 影响 | 自动引擎选择后不会配置推测解码和稀疏 KV 环境 |

---

### P2 — 低优先级 / 鲁棒性

#### 24. `process_utils.py` — `_decode_bytes(raw)` + `_iter_lines_from_stream(stream)`
| 字段 | 内容 |
|------|------|
| v2 文件 | `utils/process_utils.py` |
| v1 目标文件 | `utils/process_utils.py` |
| 描述 | "永不崩溃"编码解码器（UTF-8 → gb18030 → replace）和基于 `stream.buffer.readline()` 的编码安全行迭代器 |
| 影响 | 在 sidecar 模式下 v1 不直接启动引擎进程，因此影响较小。但 `log_stream` 的健壮性稍弱 |

#### 25. `utils/ascend910_patch.py` — 整个文件缺失
| 字段 | 内容 |
|------|------|
| v2 文件 | `utils/ascend910_patch.py` |
| v1 目标文件 | (不存在) |
| 描述 | Ascend910_9362 设备补丁自动应用模块（`get_device_name()`, `apply_patch()`, `main()`）。通过 `config/set_Ascend910_9362_patch.sh` 调用 |
| 影响 | Ascend910_9362 设备需要的补丁在 v1 中需要通过其他方式应用。在 sidecar 架构下通常由引擎容器自己执行补丁，故影响有限 |

#### 26. `utils/fix_diffusers_custom_op_shim.py` — 整个文件缺失
| 字段 | 内容 |
|------|------|
| v2 文件 | `utils/fix_diffusers_custom_op_shim.py` |
| v1 目标文件 | (不存在) |
| 描述 | 563 行的 `torch.library.custom_op` / `register_fake` 兼容 shim，解决 diffusers 在 NPU 上的兼容性问题 |
| 影响 | 仅对使用 diffusers 的多模态模型（如 HunyuanVideo）有影响；在 sidecar 模式下这是引擎容器内的事情 |

#### 27. `servers/` — 整个目录缺失
| 字段 | 内容 |
|------|------|
| v2 文件 | `servers/` (transformers_server.py, model/hunyuanvideo_server/*, model/qwenimage_server/*) |
| v1 目标文件 | (不存在) |
| 描述 | 独立推理服务器实现：基于 transformers 的 LLM 服务器、HunyuanVideo 视频生成服务器、Qwen 图像生成服务器 |
| 影响 | 在 v1 sidecar 架构中，这些服务器运行在独立的引擎容器中，由 adapter 生成的脚本启动。v1 的 adapter 如果能正确生成启动命令指向这些服务器的入口脚本，则无需包含这些文件。需确认 wings_adapter 和 MMGM 适配是否正确引用了引擎容器中的入口点 |

#### 28. `config/` — v2-only shell 脚本
| 字段 | 内容 |
|------|------|
| v2 文件 | `config/set_Ascend910_9362_patch.sh`、`set_mindie_multi_env.sh`、`set_mindie_single_env.sh`、`set_wings_ascend_env.sh`、`set_wings_nvidia_env.sh`、`set_xllm_env.sh`、`set_sglang_env.sh`、`set_vllm_env.sh`、`set_vllm_ascend_env.sh` |
| v1 目标文件 | (不存在) |
| 描述 | 引擎启动前的环境变量设置脚本。在 v2 中由 `wings.py` 或 adapter 在进程启动前 `source` 执行 |
| 影响 | v1 sidecar 模式下，adapter 的 `build_start_script()` 已将环境变量设置内联到生成的启动脚本中，因此这些独立脚本文件不是必需的。但应确认 adapter 覆盖了所有必要的环境变量 |

---

## 三、按文件索引

| v2 文件 | 差距编号 | 优先级 |
|---------|---------|--------|
| `utils/env_utils.py` | #1, #2, #3 | P0 |
| `utils/process_utils.py` | #24 | P2 |
| `utils/ascend910_patch.py` | #25 | P2 |
| `utils/fix_diffusers_custom_op_shim.py` | #26 | P2 |
| `core/config_loader.py` | #4–#15, #16–#20, #23 | P0/P1 |
| `config/engine_parameter_mapping.json` | #21 | P1 |
| `config/wings_default.json` | #22 | P1 |
| `config/*.sh` | #28 | P2 |
| `servers/` | #27 | P2 |

---

## 四、无功能差距的模块确认

以下模块经逐文件比对确认 **无功能性差距**（仅文档/注释差异或 v1 已有增强）：

| 模块/文件 | 状态 |
|-----------|------|
| `utils/file_utils.py` | ✅ 功能一致 |
| `utils/noise_filter.py` | ✅ 功能一致 |
| `utils/model_utils.py` | ✅ 功能一致（已迁移） |
| `utils/device_utils.py` | ✅ 功能一致（已迁移 `check_pcie_cards`） |
| `utils/mmgm_utils.py` | ✅ 已迁移 |
| `engines/vllm_adapter.py` | ✅ 已迁移 |
| `engines/sglang_adapter.py` | ✅ 架构差异（sidecar 正常） |
| `engines/mindie_adapter.py` | ✅ 架构差异（v1 更完整） |
| `engines/wings_adapter.py` | ✅ 已迁移 |
| `engines/xllm_adapter.py` | ✅ 已迁移 |
| `engines/__init__.py` | ✅ 功能一致 |
| `core/engine_manager.py` | ✅ 架构差异（sidecar 正常） |
| `core/hardware_detect.py` | ✅ 架构差异（sidecar 用 env var） |
| `proxy/gateway.py` | ✅ 功能一致（v1 还有额外改进） |
| `proxy/health.py` | ✅ 功能一致 |
| `proxy/health_service.py` | ✅ 功能一致 |
| `proxy/settings.py` | ✅ 功能一致（v1 有更多超时配置） |
| `proxy/http_client.py` | ✅ 功能一致（v1 使用可配置超时） |
| `proxy/queueing.py` | ✅ 功能一致 |
| `proxy/tags.py` | ✅ 功能一致 |
| `proxy/speaker_logging.py` | ✅ 功能一致 |
| `proxy/rag_acc/` | ✅ 已迁移 |
| `distributed/master.py` | ✅ v1 更完整（有 `_inject_distributed_params`） |
| `distributed/monitor.py` | ✅ 功能一致 |
| `distributed/scheduler.py` | ✅ 功能一致 |
| `distributed/worker.py` | ✅ v1 更完整（有 `node_info` 端点） |
| `config/*.json`（除 engine_parameter_mapping, wings_default） | ✅ 结构/值一致 |

---

## 五、架构差异说明（非功能差距）

以下差异属于 v1 sidecar 架构与 v2 直接启动架构的 **设计差异**，不计为功能差距：

1. **引擎启动方式**: v2 的 adapter 通过 `start_engine()` 直接启动子进程；v1 通过 `build_start_script()`/`build_start_command()` 生成 bash 脚本写入共享卷
2. **硬件检测**: v2 运行时调用 `get_device_info()` 探测 GPU/NPU；v1 从 `WINGS_DEVICE`、`WINGS_DEVICE_COUNT` 等环境变量读取
3. **健康检查代理**: v2 的 gateway 通过 `_proxy_health_request` 代理到独立健康服务；v1 在 gateway 中直接维护健康状态机
4. **入口点**: v2 有 `wings.py`（全功能 CLI）和 `wings_proxy.py`（代理启动器）；v1 通过 `core/wings_entry.py` 和 launcher 脚本启动
5. **engine_adapter.py 基类**: v2 有 `EngineAdapter` 抽象基类用于直接启动；v1 不需要

---

## 六、建议迁移顺序

1. **第一批（P0 核心）**: #1–#3（env_utils 三个函数）→ #4–#6（spec_decoding/sparse/soft_fp4）→ #7–#9（function_call/embedding_rerank/xllm_params）→ #10（_merge_cmd_params 参数扩展）
2. **第二批（P0 引擎选择）**: #11–#15（引擎选择逻辑增强）
3. **第三批（P1）**: #16–#23（辅助验证与配置文件）
4. **第四批（P2）**: #24–#28（可选，视实际需求）
