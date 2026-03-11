# =============================================================================
# 文件: engines/vllm_adapter.py
# 用途: vLLM / vLLM-Ascend 推理引擎适配器
# 状态: 活跃适配器，sidecar launcher 模式下禁用进程启动 API
#
# 功能概述:
#   本模块负责将统一的参数字典转换为 vLLM 的启动命令和环境变量设置。
#   支持以下部署模式：
#     - 单机模式:       直接运行 vLLM OpenAI API Server
#     - Ray 分布式:    多节点 Ray 集群，rank0 为 head，其他为 worker
#     - DP 分布式:     数据并行模式（dp_deployment 后端）
#     - PD 分离:       Prefill-Decode 分离架构（NIXL 协议）
#     - vLLM-Ascend: 华为昇腾 NPU 版本（需要 CANN 环境）
#
# 核心接口:
#   - build_start_script(params) : 返回完整 bash 脚本（推荐，含环境设置）
#   - build_start_command(params): 返回核心启动命令（兼容旧版）
#   - start_engine(params)       : 已禁用，sidecar 模式不允许直接启动进程
#
# Sidecar 架构契约:
#   - build_start_script 是 launcher 唯一调用的入口
#   - 生成的脚本写入共享卷，由 engine 容器执行
#   - 不得重新引入直接进程启动逻辑
#
# 引擎启动命令格式:
#   python3 -m vllm.entrypoints.openai.api_server \
#       --model <model_path> \
#       --host 0.0.0.0 \
#       --port 17000 \
#       --tensor-parallel-size <tp_size> \
#       [--distributed-executor-backend ray]
#
# =============================================================================
# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

"""
vLLM 引擎适配器。

在 sidecar launcher 模式下，本模块仅负责命令拼装，不启动任何子进程。
生成的 shell 脚本将由 engine 容器读取并执行。

支持的引擎类型:
    - vllm:        NVIDIA GPU 版本，使用 NCCL 通信
    - vllm_ascend: 华为昇腾 NPU 版本，使用 HCCL 通信

分布式后端:
    - ray:           Ray 集群模式，支持多节点 TP
    - dp_deployment: 数据并行模式，支持多节点 DP
"""

import logging
import os
import re
import shlex
from typing import Dict, Any, List

from app.utils.model_utils import ModelIdentifier

from app.utils.env_utils import get_local_ip, get_lmcache_env, \
    get_pd_role_env, get_qat_env


def _sanitize_shell_path(path: str) -> str:
    """对路径进行 shell 安全转义，防止命令注入攻击。

    使用 shlex.quote() 进行标准 POSIX shell 转义，
    相比简单的正则过滤更安全且不会破坏包含空格的合法路径。

    Args:
        path: 原始文件路径字符串

    Returns:
        str: 经过 shell 安全转义的路径
    """
    return shlex.quote(path)

logger = logging.getLogger(__name__)

# 模块根目录：用于定位配置文件和环境脚本
root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _build_base_env_commands(params, engine: str, root: str) -> List[str]:
    """构建基础环境变量设置命令列表。

    根据引擎类型选择对应的环境初始化脚本：
    - vllm:        NVIDIA GPU 环境，使用 set_vllm_env.sh
    - vllm_ascend: 华为昇腾环境，需要加载 CANN 和 ATB 工具包

    Args:
        params: 参数字典，可能包含是否启用昆仑 ATB 等标志
        engine: 引擎类型 ('vllm' 或 'vllm_ascend')
        root:   项目根目录路径

    Returns:
        List[str]: shell 命令列表，每个元素是一条环境设置命令

    注意:
        - vllm_ascend 在找不到本地脚本时，回退到容器内标准路径
        - use_kunlun_atb 启用时会设置 USE_KUNLUN_ATB=1 环境变量
    """
    env_commands = []
    #  app/config/ ?wings/
    config_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config"
    )
    if engine == "vllm":
        local_script = os.path.join(config_dir, "set_vllm_env.sh")
        if os.path.exists(local_script):
            env_commands.append(f"source {local_script}")
    elif engine == "vllm_ascend":
        local_script = os.path.join(config_dir, "set_vllm_ascend_env.sh")
        if os.path.exists(local_script):
            env_commands.append(f"source {local_script}")
        else:
            env_commands.append("source /usr/local/Ascend/ascend-toolkit/set_env.sh")
            env_commands.append("source /usr/local/Ascend/nnal/atb/set_env.sh")
        if params.get("engine_config", {}).get("use_kunlun_atb"):
            env_commands.append(f"export USE_KUNLUN_ATB=1")
            logger.info("kunlun atb is used")
    return env_commands


def _build_cache_env_commands(engine: str) -> List[str]:
    """构建 KVCache Offload 特性的环境变量设置命令。

    KVCache Offload 允许将 KV 缓存卸载到主机内存或远端存储，
    这一特性需要额外的共享库支持，通过 LD_LIBRARY_PATH 注入。

    支持的引擎和库路径:
        - vllm:        kv_agent 库 (/opt/vllm_env/.../kv_agent/lib)
        - vllm_ascend: lmcache 库 (/opt/ascend_env/.../lmcache)

    Args:
        engine: 引擎类型

    Returns:
        List[str]: LD_LIBRARY_PATH 设置命令列表，未启用时返回空列表

    环境变量:
        - LMCACHE_OFFLOAD: 是否启用 KVCache Offload (true/false)
        - KV_AGENT_LIB_PATH: vLLM kv_agent 库路径
        - LMCACHE_LIB_PATH:  vLLM-Ascend lmcache 库路径
    """
    env_commands = []
    if not get_lmcache_env():
        return env_commands

    if engine == "vllm":
        #  kv_agent
        lib_path = _sanitize_shell_path(os.getenv("KV_AGENT_LIB_PATH", "/opt/vllm_env/lib/python3.10/site-packages/kv_agent/lib"))
        env_commands.append(f'_KV_LIB_PATH={lib_path}')
        env_commands.append('export LD_LIBRARY_PATH="${_KV_LIB_PATH}:${LD_LIBRARY_PATH:-}"')
        logger.info("[KVCache Offload] Added LD_LIBRARY_PATH for vllm: %s", lib_path)
    elif engine == "vllm_ascend":
        #  lmcache
        lib_path = _sanitize_shell_path(os.getenv("LMCACHE_LIB_PATH", "/opt/ascend_env/lib/python3.11/site-packages/lmcache"))
        env_commands.append(f'_LMCACHE_LIB_PATH={lib_path}')
        env_commands.append('export LD_LIBRARY_PATH="${_LMCACHE_LIB_PATH}:${LD_LIBRARY_PATH:-}"')
        logger.info("[KVCache Offload] Added LD_LIBRARY_PATH for vllm_ascend: %s", lib_path)

    return env_commands


def _build_qat_env_commands(engine) -> List[str]:
    """构建 KVCache QAT 压缩特性的环境变量设置命令。

    QAT (QuickAssist Technology) 是 Intel 的硬件压缩加速技术，
    可用于压缩 KV 缓存以减少内存占用和传输开销。

    注意:
        - 当前仅 vllm (NVIDIA) 支持 QAT 压缩
        - vllm_ascend 不支持，会自动禁用并打印警告

    Args:
        engine: 引擎类型

    Returns:
        List[str]: LMCACHE_QAT_ENABLED 设置命令列表

    环境变量:
        - LMCACHE_QAT: 是否启用 QAT 压缩 (true/false)
    """
    env_commands = []
    if not get_qat_env():
        return env_commands

    if engine == "vllm":
        env_commands.append('export LMCACHE_QAT_ENABLED=True')
    else:
        env_commands.append('export LMCACHE_QAT_ENABLED=False')
        logger.warning("[KVCache Offload] QAT compression feature is not supported by the current engine %s, "
                       "it has been automatically disabled", engine)
    return env_commands


def _build_pd_role_env_commands(engine: str, current_ip: str, network_interface: str) -> List[str]:
    """构建 PD 分离部署的环境变量设置命令。

    PD 分离 (Prefill-Decode Disaggregation) 是一种高级部署架构，
    将 Prefill 和 Decode 阶段分离到不同节点，以优化资源利用率。

    vllm (NVIDIA) 场景:
        - 使用 NIXL 协议进行 KV 传输
        - 设置 VLLM_NIXL_SIDE_CHANNEL_HOST

    vllm_ascend 场景:
        - 使用 HCCL 进行跨节点通信
        - 需要设置多个网络接口环境变量
        - 依赖 CANN 和 ATB 工具包

    Args:
        engine:           引擎类型 ('vllm' 或 'vllm_ascend')
        current_ip:       当前节点 IP 地址
        network_interface: 网络接口名称 (如 'eth0')

    Returns:
        List[str]: PD 分离所需的环境变量设置命令

    环境变量:
        - PD_ROLE: PD 角色 ('P' 或 'D')
        - VLLM_LLMDD_RPC_PORT: LLMDataDist RPC 端口号
    """
    env_commands = []
    if get_pd_role_env():
        if engine == "vllm":
            env_commands.append(f'export VLLM_NIXL_SIDE_CHANNEL_HOST={current_ip}')
        elif engine == "vllm_ascend":
            rpc_port = os.getenv('VLLM_LLMDD_RPC_PORT', "5569")
            env_commands.extend([
                f"source /usr/local/Ascend/ascend-toolkit/set_env.sh",
                f"source /usr/local/Ascend/nnal/atb/set_env.sh",
                f"export HCCL_IF_IP={current_ip}",
                f"export GLOO_SOCKET_IFNAME={network_interface}",
                f"export TP_SOCKET_IFNAME={network_interface}",
                f"export HCCL_SOCKET_IFNAME={network_interface}",
                f"export OMP_PROC_BIND=false",
                f"export OMP_NUM_THREADS={os.getenv('OMP_NUM_THREADS', '100')}",
                f"export VLLM_USE_V1=1",
                f"export LCCL_DETERMINISTIC=1",
                f"export HCCL_DETERMINISTIC=true",
                f"export CLOSE_MATMUL_K_SHIFT=1",
                f"export VLLM_LLMDD_RPC_PORT={rpc_port}",
                f"export PYTORCH_NPU_ALLOC_CONF=max_split_size_mb:{os.getenv('NPU_MAX_SPLIT_SIZE_MB', '256')}"
            ])
    return env_commands


def _build_distributed_env_commands(params: Dict[str, Any], current_ip: str,
                                    network_interface: str, engine: str) -> List[str]:
    """构建分布式环境变量设置命令（扩展点）。

    当前返回空列表，分布式 NCCL/HCCL 环境设置已在
    _build_pd_role_env_commands 和 build_start_script 内部的
    Ray 初始化块中处理。

    保留此存根作为未来扩展点，可用于：
    - 添加特定引擎的分布式环境配置
    - 支持新的分布式后端

    Args:
        params:            参数字典
        current_ip:        当前节点 IP
        network_interface: 网络接口名称
        engine:            引擎类型

    Returns:
        List[str]: 环境变量设置命令列表（当前为空）
    """
    return []


def _build_env_commands(params: Dict[str, Any], current_ip: str, network_interface: str, root: str) -> List[str]:
    """组装完整的环境变量设置命令列表。

    按顺序调用各子模块构建环境设置，创建完整的环境初始化流程：
    1. 基础环境（CANN/ATB 工具包）
    2. KVCache Offload 环境
    3. QAT 压缩环境
    4. PD 分离环境
    5. 分布式环境（扩展点）

    Args:
        params:            参数字典，包含 engine 等配置
        current_ip:        当前节点 IP 地址
        network_interface: 网络接口名称
        root:              项目根目录

    Returns:
        List[str]: 所有环境变量设置命令的有序列表
    """
    engine = params.get("engine")
    env_commands = []

    env_commands.extend(_build_base_env_commands(params, engine, root))
    env_commands.extend(_build_cache_env_commands(engine))
    env_commands.extend(_build_qat_env_commands(engine))
    env_commands.extend(_build_pd_role_env_commands(engine, current_ip, network_interface))
    env_commands.extend(_build_distributed_env_commands(params, current_ip, network_interface, engine))

    return env_commands


def _build_vllm_cmd_parts(params: Dict[str, Any]) -> str:
    """构建 vLLM 核心启动命令字符串。

    将 engine_config 字典转换为 vLLM CLI 参数格式：
    python3 -m vllm.entrypoints.openai.api_server --arg1 value1 --arg2 value2 ...

    参数转换规则：
    - 参数名: snake_case → kebab-case (如 tensor_parallel_size → --tensor-parallel-size)
    - 布尔值: True → 仅输出 flag (如 --enable-prefix-caching)
    - 布尔值: False → 跳过
    - 空字符串: 跳过，避免生成空参数
    - JSON 字典: 用单引号包裹 (如 --kv-transfer-config '{...}')
    - 其他值: 直接转为字符串

    特殊处理：
    - use_kunlun_atb: 内部参数，不传递给 vLLM CLI
    - max_num_batched_tokens: 必须为正整数，否则跳过

    Args:
        params: 参数字典，必须包含 engine_config 字典

    Returns:
        str: 完整的 vLLM 启动命令字符串

    示例输出：
        python3 -m vllm.entrypoints.openai.api_server \\
            --model /weights --host 0.0.0.0 --port 17000 \\
            --tensor-parallel-size 4 --trust-remote-code
    """
    engine_config = params.get("engine_config", {})
    # llm
    if "use_kunlun_atb" in engine_config:
        engine_config.pop("use_kunlun_atb")
    # if params.get("distributed"):
        # raise ValueError("Distributed mode is disabled in sidecar launcher MVP.")

    # vllm/vllm-openai image guarantees python3, while python may be absent.
    cmd_parts = ["python3", "-m", "vllm.entrypoints.openai.api_server"]

    for arg, value in engine_config.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            # Skip empty-string values to avoid generating broken args like:
            # --quantization  --gpu-memory-utilization ...
            continue
        if arg == "max_num_batched_tokens":
            try:
                if int(value) <= 0:
                    logger.warning(
                        "Skip invalid max_num_batched_tokens=%s; vLLM requires >=1",
                        value,
                    )
                    continue
            except (TypeError, ValueError):
                logger.warning(
                    "Skip non-integer max_num_batched_tokens=%s",
                    value,
                )
                continue

        arg_name = f"--{arg.replace('_', '-')}"
        if isinstance(value, bool):
            if value:
                cmd_parts.append(arg_name)
        elif isinstance(value, str) and value.strip().startswith('{') and value.strip().endswith('}'):
            cmd_parts.extend([arg_name, f"'{value}'"])
        else:
            cmd_parts.extend([arg_name, str(value)])

    return " ".join(cmd_parts)


def _build_vllm_command(params: Dict[str, Any]) -> str:
    """构建完整的 vLLM 服务启动命令（含环境设置）。

    将环境变量设置和核心启动命令组合为单行命令：
    source env.sh && export VAR=val && python3 -m vllm...

    Args:
        params: 服务器参数字典

    Returns:
        str: 完整的 vLLM 服务启动命令字符串

    注意:
        - 此函数主要用于单机模式
        - 分布式模式应使用 build_start_script()
    """
    current_ip = get_local_ip()
    # Skip netifaces auto-detection to avoid dependency
    network_interface = os.getenv("NETWORK_INTERFACE", os.getenv("GLOO_SOCKET_IFNAME", "eth0"))

    # Build environment variable commands
    env_commands = _build_env_commands(
        params, current_ip, network_interface, root_dir
    )

    # Build main command
    command_str = _build_vllm_cmd_parts(params)

    # Combine full command
    if env_commands:
        return " && ".join(env_commands) + " && " + command_str
    return command_str


def build_start_command(params: Dict[str, Any]) -> str:
    """为 launcher 生成 vLLM 启动命令字符串（旧版接口）。

    此函数仅执行命令拼装，不启动任何子进程。
    返回的命令不包含环境变量设置，适合简单场景。

    Args:
        params: 参数字典

    Returns:
        str: vLLM 启动命令字符串

    Raises:
        ValueError: 分布式模式不支持此简化接口

    建议:
        推荐使用 build_start_script() 获取完整脚本
    """
    if params.get("distributed", False):
        raise ValueError("Launcher MVP does not support distributed mode for vLLM.")
    return _build_vllm_cmd_parts(params)


def build_start_script(params: Dict[str, Any]) -> str:
    """生成完整的 bash 启动脚本体（start_command.sh 内容，不含 shebang）。

    这是 vLLM 适配器的主要入口，生成的脚本将写入共享卷，
    由 engine 容器读取并执行。

    支持的部署模式:

    1. 单机 vllm:
       exec python3 -m vllm.entrypoints.openai.api_server ...

    2. 单机 vllm_ascend:
       source /usr/local/Ascend/.../set_env.sh  # 加载 CANN 环境
       exec python3 -m vllm.entrypoints.openai.api_server ...

    3. Ray 分布式 (rank0 - head 节点):
       [Ascend 环境设置]
       [Triton NPU 驱动补丁]  # vllm_ascend 特有
       export VLLM_HOST_IP=...
       ray start --head --port=6379 ...
       # 等待 worker 加入
       exec python3 -m vllm... --distributed-executor-backend ray

    4. Ray 分布式 (rank>0 - worker 节点):
       [Ascend 环境设置]
       [Triton NPU 驱动补丁]
       # 探测并连接 head IP
       exec ray start --address=$HEAD_IP:6379 --block

    5. DP 分布式 (dp_deployment 后端):
       exec python3 -m vllm... --data-parallel-address ... --data-parallel-rank ...

    Args:
        params: 参数字典，包含以下关键字段:
            - engine: 'vllm' 或 'vllm_ascend'
            - engine_config: 引擎配置参数
            - distributed: 是否分布式
            - nnodes: 节点数
            - node_rank: 当前节点编号
            - distributed_executor_backend: 'ray' 或 'dp_deployment'
            - head_node_addr: head 节点地址

    Returns:
        str: 完整的 bash 脚本体（不含 shebang）

    环境变量:
        - NODE_IPS: 所有节点 IP 列表（逗号分隔）
        - RAY_PORT: Ray head 端口号，默认 6379
        - VLLM_DP_RPC_PORT: DP 模式 RPC 端口，默认 13355
    """
    engine = params.get("engine", "vllm")
    cmd = _build_vllm_cmd_parts(params)
    is_distributed = params.get("distributed", False)
    node_rank = params.get("node_rank", 0)
    nnodes = params.get("nnodes", 1)
    backend = params.get("distributed_executor_backend", "ray")
    head_addr = params.get("head_node_addr", "infer-0.infer-hl")
    # NODE_IPS: params["nodes"] 优先（由 config_loader / Master 注入），其次环境变量
    node_ips = params.get("nodes", os.getenv("NODE_IPS", head_addr))
    # ray_head_port: params 优先（config_loader 从 distributed_config.json 注入 28020），
    # 其次环境变量，最后回退到 28020（与 wings 对齐）
    ray_port = str(params.get("ray_head_port", os.getenv("RAY_PORT", "28020")))

    if is_distributed and nnodes > 1:
        script_parts = []
        is_ascend = (engine == "vllm_ascend")

        if backend == "ray":
            # ── Ascend CANN env setup (vllm_ascend only) ─────────────────────
            if is_ascend:
                ascend_env_block = [
                    "# set +u: Ascend env scripts may reference unbound vars (e.g. ZSH_VERSION)",
                    "set +u",
                    "[ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ] "
                    "&& source /usr/local/Ascend/ascend-toolkit/set_env.sh "
                    "|| echo 'WARN: ascend-toolkit/set_env.sh not found'",
                    "[ -f /usr/local/Ascend/nnal/atb/set_env.sh ] "
                    "&& source /usr/local/Ascend/nnal/atb/set_env.sh "
                    "|| echo 'WARN: nnal/atb/set_env.sh not found'",
                    "set -u",
                ]
                script_parts.extend(ascend_env_block)
                script_parts.append("")

                # ── Patch Triton driver for Ascend NPU ──────────────────────
                # vllm-ascend worker.py unconditionally imports torch_npu._inductor,
                # which triggers triton.runtime.driver._create_driver().
                # Ascend NPU has no Triton backend �?"0 active drivers" RuntimeError.
                # Patch the source file so ALL child processes (Ray workers) get the fix.
                triton_patch_block = [
                    "# Patch triton driver.py: Ascend NPU has no Triton backend, return dummy driver",
                    "python3 << 'TRITON_PATCH_EOF'",
                    "try:",
                    "    import triton.runtime, os",
                    "    drv_path = os.path.join(os.path.dirname(triton.runtime.__file__), 'driver.py')",
                    "    with open(drv_path) as f:",
                    "        src = f.read()",
                    "    if 'raise RuntimeError' in src and 'PATCHED_NPU' not in src:",
                    "        patch = '''",
                    "        # PATCHED_NPU: Ascend NPU has no Triton backend, provide dummy driver",
                    "        class _NpuDummyDrv:",
                    "            def get_current_target(self):",
                    "                import types; return types.SimpleNamespace(backend='npu', arch='Ascend910B', warp_size=0)",
                    "            def get_current_device(self): return 0",
                    "            def get_device_capability(self, *a): return (0, 0)",
                    "            def get_device_properties(self, device=0):",
                    "                try:",
                    "                    import torch_npu; n = torch_npu.npu.get_device_name(device); c = 20 if '910B' in str(n) else 30",
                    "                except Exception: c = 20",
                    "                return {'num_aicore': c, 'num_vectorcore': c}",
                    "            def __getattr__(self, name): return _NpuDummyDrv()",
                    "            def __call__(self, *a, **k): return self",
                    "            def __repr__(self): return '<NpuDummy>'",
                    "            def __int__(self): return 0",
                    "            def __bool__(self): return False",
                    "        return _NpuDummyDrv()'''",
                    "        src = src.replace(",
                    '            \'raise RuntimeError(f"{len(active_drivers)} active drivers ({active_drivers}). There should only be one.")\',',
                    "            patch.strip()",
                    "        )",
                    "        with open(drv_path, 'w') as f:",
                    "            f.write(src)",
                    "        print('[triton-patch] Patched', drv_path, 'for Ascend NPU')",
                    "    else:",
                    "        print('[triton-patch] Already patched or not needed')",
                    "except Exception as e:",
                    "    print(f'[triton-patch] Skip: {e}')",
                    "TRITON_PATCH_EOF",
                ]
                script_parts.extend(triton_patch_block)
                script_parts.append("")

            if node_rank == 0:
                # Detect this node's IP for Ray placement group scheduling.
                # 'ip' command is NOT available in vllm-ascend container, so use
                # POD_IP from Kubernetes Downward API (status.podIP = node IP for hostNetwork pods)
                # with Python UDP trick as fallback.
                if is_ascend:
                    script_parts.append("export VLLM_HOST_IP=${POD_IP:-$(python3 -c \"import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print(s.getsockname()[0]);s.close()\" 2>/dev/null || hostname -i)}")
                else:
                    # For NV with hostNetwork, POD_IP = node's external IP (routable between nodes).
                    # 'hostname -i' returns the container bridge IP (172.17.x.x), NOT suitable for Ray.
                    script_parts.append("export VLLM_HOST_IP=${POD_IP:-$(python3 -c \"import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print(s.getsockname()[0]);s.close()\" 2>/dev/null || hostname -i)}")
                if is_ascend:
                    # Ascend: use HCCL instead of NCCL
                    script_parts.append("export HCCL_WHITELIST_DISABLE=1")
                    script_parts.append("export HCCL_IF_IP=$VLLM_HOST_IP")
                    # Detect default-route interface from /proc/net/route
                    # (ip command unavailable in vllm-ascend image; awk is universally present)
                    script_parts.append("export HCCL_SOCKET_IFNAME=$(awk '$2==\"00000000\"{print $1;exit}' /proc/net/route 2>/dev/null || echo eth0)")
                else:
                    script_parts.append(f"export NCCL_SOCKET_IFNAME={os.getenv('NCCL_SOCKET_IFNAME', 'eth0')}")
                script_parts.append("export GLOO_SOCKET_IFNAME=$(awk '$2==\"00000000\"{print $1;exit}' /proc/net/route 2>/dev/null || echo eth0)\n")
                # Ascend: use --resources='{"NPU": 1}' instead of --num-gpus=1
                # vllm-ascend v0.14.0rc1 requires NPU resource in Ray cluster, not GPU
                ray_head_resource = "--resources='{\"NPU\": 1}'" if is_ascend else "--num-gpus=1"
                script_parts.append(f"ray start --head --port={ray_port} --node-ip-address=$VLLM_HOST_IP {ray_head_resource} --dashboard-host=0.0.0.0\n")
                script_parts.append("for i in $(seq 1 60); do")
                script_parts.append("  COUNT=$(python3 -c \"import ray; ray.init(address='auto',ignore_reinit_error=True); print(len([n for n in ray.nodes() if n['alive']])); ray.shutdown()\" 2>/dev/null || echo 0)")
                script_parts.append("  [ \"$COUNT\" -ge \"2\" ] && break")
                script_parts.append("  sleep 5")
                script_parts.append("done\n")
                # Ascend: --enforce-eager bypasses Triton compilation
                # (vllm-ascend v0.14.0rc1 Triton NPU driver detection fails in k3s containers)
                eager_flag = " --enforce-eager" if is_ascend else ""
                script_parts.append(f"exec {cmd}{eager_flag} --distributed-executor-backend ray")
            else:
                if is_ascend:
                    script_parts.append("export HCCL_WHITELIST_DISABLE=1")
                    # Dynamically detect Ray head IP from NODE_IPS list
                    # (pod-to-node mapping is non-deterministic with StatefulSet Parallel mode)
                    node_ips_bash = node_ips  # e.g. "192.168.1.100,192.168.1.101"
                    script_parts.append(f"NODE_IPS_LIST=\"{node_ips_bash}\"")
                    script_parts.append("HEAD_IP=\"\"")
                    script_parts.append(f"echo \"[worker] Scanning NODE_IPS for Ray head on port {ray_port}...\"")
                    script_parts.append("for attempt in $(seq 1 120); do")
                    script_parts.append("  for ip in $(echo $NODE_IPS_LIST | tr ',' ' '); do")
                    script_parts.append(f"    if python3 -c \"import socket; s=socket.socket(); s.settimeout(2); s.connect(('$ip',{ray_port})); s.close()\" 2>/dev/null; then")
                    script_parts.append("      HEAD_IP=$ip")
                    script_parts.append(f"      echo \"[worker] Found Ray head at $HEAD_IP:{ray_port}\"")
                    script_parts.append("      break 2")
                    script_parts.append("    fi")
                    script_parts.append("  done")
                    script_parts.append("  sleep 5")
                    script_parts.append("done")
                    script_parts.append("if [ -z \"$HEAD_IP\" ]; then echo '[worker] ERROR: Could not find Ray head'; exit 1; fi\n")
                    # Set HCCL env using discovered head IP
                    script_parts.append(f"export HCCL_IF_IP=$(python3 -c \"import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(('$HEAD_IP',{ray_port})); print(s.getsockname()[0]); s.close()\" 2>/dev/null || hostname -i)")
                    script_parts.append("export HCCL_SOCKET_IFNAME=$(awk '$2==\"00000000\"{print $1;exit}' /proc/net/route 2>/dev/null || echo eth0)")
                    # Worker also needs VLLM_HOST_IP for Ray node matching
                    script_parts.append("export VLLM_HOST_IP=${POD_IP:-$(python3 -c \"import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print(s.getsockname()[0]);s.close()\" 2>/dev/null || hostname -i)}")
                else:
                    script_parts.append(f"export NCCL_SOCKET_IFNAME={os.getenv('NCCL_SOCKET_IFNAME', 'eth0')}")
                    # Worker's own routable IP (for Ray node-ip-address)
                    script_parts.append("export VLLM_HOST_IP=${POD_IP:-$(python3 -c \"import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print(s.getsockname()[0]);s.close()\" 2>/dev/null || hostname -i)}")
                    script_parts.append("for i in $(seq 1 60); do")
                    script_parts.append(f"  python3 -c \"import socket; s=socket.socket(); s.settimeout(2); s.connect(('{head_addr}',{ray_port})); s.close()\" 2>/dev/null && break")
                    script_parts.append("  sleep 5")
                    script_parts.append("done")
                    script_parts.append(f"HEAD_IP=\"{head_addr}\"")
                script_parts.append("export GLOO_SOCKET_IFNAME=$(awk '$2==\"00000000\"{print $1;exit}' /proc/net/route 2>/dev/null || echo eth0)\n")
                # Ascend: use --resources='{"NPU": 1}' instead of --num-gpus=1
                ray_worker_resource = "--resources='{\"NPU\": 1}'" if is_ascend else "--num-gpus=1"
                script_parts.append(f"exec ray start --address=$HEAD_IP:{ray_port} --node-ip-address=$VLLM_HOST_IP {ray_worker_resource} --block")
        else: # dp_deployment
            # rpc_port: params 优先（config_loader 从 distributed_config.json 注入），其次环境变量
            dp_rpc_port = str(params.get("rpc_port", os.getenv('VLLM_DP_RPC_PORT', '13355')))

            # DeepseekV3ForCausalLM 在 vllm_ascend DP 模式下使用专用并行参数
            model_info = ModelIdentifier(
                params.get("model_name"), params.get("model_path"), params.get("model_type"))
            if (model_info.model_architecture == "DeepseekV3ForCausalLM"
                    and engine == "vllm_ascend"):
                dp_size = "4"
                dp_size_local = "2"
                dp_start_rank = "2" if node_rank != 0 else "0"
            else:
                dp_size = str(nnodes)
                dp_size_local = "1"
                dp_start_rank = str(node_rank)

            # dp_deployment 模式分布式通信环境变量（对齐 A）
            net_if = os.getenv("NETWORK_INTERFACE", os.getenv("GLOO_SOCKET_IFNAME", "eth0"))
            if is_ascend:
                script_parts.extend([
                    f"export HCCL_IF_IP=${{POD_IP:-$(python3 -c \"import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print(s.getsockname()[0]);s.close()\" 2>/dev/null || hostname -i)}}",
                    f"export GLOO_SOCKET_IFNAME={net_if}",
                    f"export TP_SOCKET_IFNAME={net_if}",
                    f"export HCCL_SOCKET_IFNAME={net_if}",
                    "export OMP_PROC_BIND=false",
                    f"export OMP_NUM_THREADS={os.getenv('OMP_NUM_THREADS', '100')}",
                    "export HCCL_BUFFSIZE=1024",
                ])
            else:
                script_parts.extend([
                    f"export GLOO_SOCKET_IFNAME={net_if}",
                    f"export TP_SOCKET_IFNAME={net_if}",
                    f"export NCCL_SOCKET_IFNAME={net_if}",
                    f"export VLLM_NIXL_SIDE_CHANNEL_PORT={params.get('nixl_port', os.getenv('VLLM_NIXL_SIDE_CHANNEL_PORT', '12345'))}",
                    "export NCCL_IB_DISABLE=0",
                    "export NCCL_CUMEM_ENABLE=0",
                    "export NCCL_NET_GDR_LEVEL=SYS",
                ])

            if node_rank == 0:
                script_parts.append(f"exec {cmd} --data-parallel-address {head_addr} --data-parallel-rpc-port {dp_rpc_port} --data-parallel-size {dp_size} --data-parallel-size-local {dp_size_local} --data-parallel-external-lb --data-parallel-rank 0")
            else:
                script_parts.append(f"exec {cmd} --data-parallel-address {head_addr} --data-parallel-rpc-port {dp_rpc_port} --data-parallel-size {dp_size} --data-parallel-size-local {dp_size_local} --data-parallel-external-lb --headless --data-parallel-start-rank {dp_start_rank}")
        return "\n".join(script_parts) + "\n"

    if engine == "vllm_ascend":
        # start_command.sh is generated by wings-infer but executed inside vllm-ascend engine container.
        # The engine container does not include wings-infer's /app/app/config/ path.
        # Must use inline image-internal standard paths, not source files from wings-infer container.
        # vllm-ascend official image pre-installs CANN toolkit at fixed paths:
        #   /usr/local/Ascend/ascend-toolkit/set_env.sh
        #   /usr/local/Ascend/nnal/atb/set_env.sh
        logger.info("vllm_ascend: inline Ascend CANN env setup in start_command.sh")
        # NOTE: k3s embedded env device selection:
        # - ASCEND_VISIBLE_DEVICES requires Ascend Docker Runtime hook; no-op inside k3s
        # - vllm TP=1 hardcodes local_rank=0 -> torch.npu.set_device(0), unaffected by ASCEND_DEVICE_ID
        # - In verification env use NPU 0 (physical card 0), no device switching
        # - To specify specific NPU, need Ascend Device Plugin for k8s or restructure deployment
        env_block = (
            "# set +u: nnal/atb/set_env.sh references ZSH_VERSION without default\n"
            "set +u\n"
            "[ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ] "
            "&& source /usr/local/Ascend/ascend-toolkit/set_env.sh "
            "|| echo 'WARN: ascend-toolkit/set_env.sh not found'\n"
            "[ -f /usr/local/Ascend/nnal/atb/set_env.sh ] "
            "&& source /usr/local/Ascend/nnal/atb/set_env.sh "
            "|| echo 'WARN: nnal/atb/set_env.sh not found'\n"
            "set -u\n"
        )
        return env_block + f"exec {cmd}\n"

    return f"exec {cmd}\n"


def start_vllm_distributed(params: Dict):
    """分布式模式入口（sidecar MVP 中不支持）。

    Raises:
        RuntimeError: sidecar 架构不允许直接启动进程
    """
    raise RuntimeError("分布式模式在 sidecar launcher MVP 中已禁用。")


def start_engine(params: Dict[str, Any]):
    """旧版兼容接口（sidecar launcher 模式中已禁用）。

    在 sidecar 架构中，适配器不允许直接启动推理进程。
    应使用 build_start_script() 生成脚本，写入共享卷，
    由 engine 容器执行。

    Raises:
        RuntimeError: 始终抛出，阻止意外调用
    """
    raise RuntimeError(
        "start_engine 在 launcher 模式中已禁用。"
        "请使用 build_start_command() 并将结果写入共享卷。"
    )

