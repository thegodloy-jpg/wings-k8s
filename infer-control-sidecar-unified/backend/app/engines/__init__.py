# -----------------------------------------------------------------------------
# 文件: engines/__init__.py
# 用途: 推理引擎适配器包标记文件。
#
# 本包包含各推理引擎的适配器，负责将统一的参数格式转换为引擎专属的启动命令：
#
#   - vllm_adapter.py   —— vLLM / vLLM-Ascend 引擎适配器
#                          生成 python3 -m vllm.entrypoints.openai.api_server 命令
#                          支持单机、Ray 分布式、DP 分布式、PD 分离等模式
#   - sglang_adapter.py —— SGLang 引擎适配器
#                          生成 python3 -m sglang.launch_server 命令
#                          支持单机和多节点分布式
#   - mindie_adapter.py —— MindIE（华为昇腾）引擎适配器
#                          生成 config.json 合并脚本 + mindieservice_daemon 启动命令
#                          支持单节点 TP 和多节点 DP 模式
#   - wings_adapter.py  —— Wings 多模态引擎适配器 (v2 新增)
#                          支持 HunyuanVideo 文生视频、QwenImage 文生图、
#                          Transformers LLM 服务
#   - xllm_adapter.py   —— XLLM 华为昇腾原生引擎适配器 (v2 新增)
#                          支持单节点和多节点部署
#
# 适配器接口契约:
#   - build_start_script(params) -> str  : 返回完整 bash 脚本体（不含 shebang）
#   - build_start_command(params) -> str : 返回核心启动命令（兼容旧接口）
#   - start_engine() 已禁用，sidecar 模式下不允许直接启动进程
#
# Sidecar 契约:
#   - 保持模块导入路径稳定（engine_manager.py 依赖固定路径规则加载适配器）
#   - 不产生运行时副作用
# -----------------------------------------------------------------------------
"""推理引擎适配器包。

包含 vLLM、SGLang、MindIE、Wings、XLLM 等引擎的启动命令生成适配器，
负责将统一参数格式转换为引擎专属的启动脚本。
"""
