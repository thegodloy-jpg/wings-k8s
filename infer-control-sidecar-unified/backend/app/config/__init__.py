# -----------------------------------------------------------------------------
# 文件: config/__init__.py
# 用途: 配置模块包标记文件。
#
# 本包包含以下配置资源:
#   - settings.py                   —— 全局配置类（端口、路径、引擎参数等）
#   - vllm_default.json             —— vLLM 引擎默认部署参数
#   - sglang_default.json           —— SGLang 引擎默认部署参数
#   - mindie_default.json           —— MindIE 引擎默认部署参数
#   - distributed_config.json       —— 分布式部署配置（Ray/HCCL 端口等）
#   - engine_parameter_mapping.json —— CLI 参数名到引擎参数名的映射表
#
# Sidecar 契约:
#   - 不在包初始化时产生运行时副作用
#   - 配置值由 launcher、proxy、health 三个子系统共同使用
#   - 修改端口或路径时，需同步检查 K8s 清单和 Dockerfile
# -----------------------------------------------------------------------------
"""配置模块包。

包含全局 Settings 单例以及各引擎的默认部署参数 JSON 文件。
"""
