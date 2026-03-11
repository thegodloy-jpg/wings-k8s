"""
=============================================================================
 Launcher 主入口模块 (main.py)
=============================================================================

功能概述：
    这是 sidecar 控制容器的主入口，负责整个推理服务的生命周期管理。
    它不直接运行推理引擎，而是作为协调器完成以下三个核心职责：

核心职责：
    1. 参数解析与脚本生成
       - 解析 CLI 启动参数和环境变量
       - 结合端口规划（PortPlan）生成 engine 启动脚本
       - 调用 build_launcher_plan() 完成配置合并和命令拼装

    2. 启动脚本传递
       - 将生成的 shell 脚本写入共享卷 (/shared-volume/start_command.sh)
       - engine 容器通过挂载同一共享卷读取脚本并执行
       - 实现跨容器的命令传递和参数同步

    3. 子服务托管
       - 启动并守护 proxy（反向代理）和 health（健康检查）两个 FastAPI 子服务
       - 监控子进程状态，异常退出时自动拉起（守护进程模式）
       - 处理系统信号（SIGINT/SIGTERM）实现优雅退出

    4. 分布式协调（DISTRIBUTED=true 时激活）
       - 通过 NODE_RANK 或 IP 比较自动判断 master/worker 角色
       - Master: 生成 rank0 脚本 + 启动 Master API + 等待 Worker 注册后分发启动指令
       - Worker: 启动 Worker API + 向 Master 注册 + 接收启动指令写入共享卷
       - 支持 NODE_IPS 中的 DNS 名称（通过 _resolve() 解析后比较）

Sidecar 架构说明：
    ┌─────────────────────────────────────────────────────────────┐
    │                      K8s Pod                                │
    │  ┌─────────────────────┐    ┌─────────────────────────────┐ │
    │  │   Launcher 容器     │    │      Engine 容器            │ │
    │  │  (wings-infer)      │    │  (vllm/sglang/mindie)       │ │
    │  │                     │    │                             │ │
    │  │  main.py ───────────┼────┼──> start_command.sh         │ │
    │  │       ↓             │    │         ↓                   │ │
    │  │  proxy:18000        │    │    engine:17000             │ │
    │  │  health:19000       │    │                             │ │
    │  └─────────────────────┘    └─────────────────────────────┘ │
    │              ↑                          ↑                   │
    │              └──────── 共享卷 ───────────┘                   │
    │                   /shared-volume/                           │
    └─────────────────────────────────────────────────────────────┘

关键设计点：
    - launcher 本身不直接启动推理引擎进程，避免跨容器进程管理的复杂性
    - 通过共享卷传递脚本，实现 launcher 与 engine 容器的解耦
    - 分布式场景下，只有 rank0 节点暴露 proxy，其他节点仅保留 health 服务

使用方式：
    # 作为模块运行
    python -m app.main --model-name DeepSeek-R1 --model-path /weights

    # 或通过 run() 函数调用
    from app.main import run
    sys.exit(run(['--model-name', 'MyModel', ...]))
"""

from __future__ import annotations

import dataclasses
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import threading
from dataclasses import dataclass
from threading import Event
from typing import Sequence

from app.config.settings import settings
from app.core.port_plan import PortPlan, derive_port_plan
from app.core.start_args_compat import LaunchArgs, parse_launch_args
from app.core.wings_entry import build_launcher_plan
from app.utils.env_utils import get_local_ip, get_master_ip, get_node_ips
from app.utils.file_utils import safe_write_file
from app.utils.noise_filter import install_noise_filters

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [launcher] %(message)s",
)
logger = logging.getLogger("wings-sidecar-launcher")

# 安装噪声过滤器：抑制 /health 访问日志、batch 噪声、pynvml FutureWarning
# 旧版 wings.py 在模块加载时调用，新版需在 launcher 入口显式调用
install_noise_filters()


@dataclass
class ManagedProc:
    """描述一个由 launcher 托管的子进程。

    这个数据类封装了子进程的完整元数据，用于 launcher 的进程守护循环。
    支持进程启动、停止、状态检查等生命周期操作。

    Attributes:
        name: 进程名称标识（如 'proxy'、'health'），用于日志和调试
        argv: 进程启动命令行参数列表，第一个元素通常是 python 解释器路径
        env:  进程环境变量字典，包含 BACKEND_URL、PORT 等运行时配置
        proc: subprocess.Popen 实例，进程未启动时为 None

    使用示例：
        >>> proxy = ManagedProc(
        ...     name='proxy',
        ...     argv=['python', '-m', 'uvicorn', 'app.proxy.gateway:app'],
        ...     env={'PORT': '18000', 'BACKEND_URL': 'http://127.0.0.1:17000'}
        ... )
        >>> _start(proxy)  # 启动进程
        >>> _stop(proxy)   # 停止进程
    """

    name: str           # 进程名称标识，用于日志打印
    argv: list[str]     # 命令行参数列表 [python, -m, uvicorn, ...]
    env: dict[str, str] # 环境变量字典，继承自父进程并添加服务特定变量
    proc: subprocess.Popen | None = None  # 实际的子进程句柄


def _start(proc: ManagedProc) -> None:
    """启动单个托管子进程。

    使用 subprocess.Popen 创建子进程，继承当前进程的标准输入输出。
    启动失败时仅记录错误日志，不抛出异常，允许守护循环继续尝试。

    Args:
        proc: 待启动的托管进程对象，启动后 proc.proc 将被设置为 Popen 实例

    注意事项:
        - 子进程使用指定的 env 字典作为环境变量，不会自动继承父进程变量
        - 启动失败通常是由于命令不存在或权限不足，需检查 argv[0] 路径
        - 启动成功后需要通过 poll() 检查进程是否正常运行
    """
    logger.info("[launcher] 启动子进程 %s: %s", proc.name, " ".join(proc.argv))
    try:
        # 使用 Popen 创建子进程，env 参数完全替换（非继承）父进程环境
        proc.proc = subprocess.Popen(proc.argv, env=proc.env)
    except OSError as e:
        # OSError 通常表示可执行文件不存在或权限问题
        logger.error("[launcher] 启动 %s 失败: %s", proc.name, e)


def _stop(proc: ManagedProc) -> None:
    """优雅停止托管子进程，必要时强制终止。

    采用两阶段停止策略：
    1. 首先发送 SIGTERM 信号请求优雅退出，等待最多 10 秒
    2. 若进程未响应，发送 SIGKILL 强制终止，再等待 5 秒
    3. 若仍未退出，放弃等待并记录警告（进程可能成为僵尸进程）

    Args:
        proc: 待停止的托管进程对象，停止后 proc.proc 将被置为 None

    设计说明:
        - 优先使用 terminate() (SIGTERM) 允许进程完成清理工作
        - 超时后使用 kill() (SIGKILL) 确保进程被终止
        - 在容器环境中，僵尸进程由 init 进程收割，影响较小
    """
    if not proc.proc:
        return  # 进程从未启动或已被清理

    # 检查进程是否仍在运行 (poll() 返回 None 表示运行中)
    if proc.proc.poll() is None:
        # 第一阶段：发送 SIGTERM 请求优雅退出
        logger.info("[launcher] 发送 SIGTERM 到 %s (pid=%d)", proc.name, proc.proc.pid)
        proc.proc.terminate()
        try:
            proc.proc.wait(timeout=10)  # 等待最多 10 秒
        except subprocess.TimeoutExpired:
            # 第二阶段：优雅退出超时，强制杀死
            logger.warning("[launcher] %s 未响应 SIGTERM，发送 SIGKILL", proc.name)
            proc.proc.kill()
            try:
                proc.proc.wait(timeout=5)  # 再等待 5 秒
            except subprocess.TimeoutExpired:
                # 极端情况：进程无法终止（内核级阻塞）
                logger.warning("[launcher] %s 在 SIGKILL 后仍未退出，放弃等待", proc.name)
    proc.proc = None  # 清理引用


def _restart_if_needed(proc: ManagedProc) -> None:
    """检查进程状态，必要时自动重启（守护进程模式）。

    该函数在主循环中周期性调用，实现子进程的自动恢复：
    - 进程从未启动 → 立即启动
    - 进程正在运行 → 不做操作
    - 进程已退出   → 记录退出码并重启

    Args:
        proc: 待检查的托管进程对象

    设计说明:
        - 无条件重启策略：任何退出（包括正常退出 code=0）都会触发重启
        - 适用于需要持续运行的服务（proxy/health）
        - 不实现退避策略，依赖上层 PROCESS_POLL_SEC 控制重启频率
    """
    if not proc.proc:
        # 进程从未启动，直接启动
        _start(proc)
        return

    # poll() 返回 None 表示进程仍在运行
    code = proc.proc.poll()
    if code is None:
        return  # 进程正常运行，无需操作

    # 进程已退出，记录并重启
    logger.warning("[launcher] %s 以退出码 %s 退出，正在重启...", proc.name, code)
    _start(proc)


def _build_child_env(port_plan: PortPlan) -> dict[str, str]:
    """为 proxy/health 子进程准备环境变量。"""
    env = os.environ.copy()

    # 后端地址：sidecar 与 engine 在同一 Pod 内共享网络命名空间。
    # 分布式模式下 RANK_IP（Pod IP）可直接访问 engine；
    # 单机/本地开发无 RANK_IP 时回退到 127.0.0.1。
    rank_ip = os.getenv("RANK_IP")
    backend_host = rank_ip if rank_ip else "127.0.0.1"

    env["BACKEND_URL"] = f"http://{backend_host}:{port_plan.backend_port}"
    env["BACKEND_HOST"] = backend_host
    env["BACKEND_PORT"] = str(port_plan.backend_port)
    env["PORT"] = str(port_plan.proxy_port)
    env["PROXY_PORT"] = str(port_plan.proxy_port)
    env["HEALTH_PORT"] = str(port_plan.health_port)
    env["HEALTH_SERVICE_PORT"] = str(port_plan.health_port)
    return env


def _build_processes(port_plan: PortPlan) -> list[ManagedProc]:
    """构造 launcher 需要托管的 proxy 与 health 进程。"""
    env = _build_child_env(port_plan)
    python_bin = settings.PYTHON_BIN
    uvicorn_mod = settings.UVICORN_MODULE
    return [
        ManagedProc(
            name="proxy",
            argv=[
                python_bin,
                "-m",
                uvicorn_mod,
                settings.PROXY_APP,
                "--host",
                "0.0.0.0",
                "--port",
                str(port_plan.proxy_port),
                "--log-level",
                "info",
            ],
            env=env.copy(),
        ),
        ManagedProc(
            name="health",
            argv=[
                python_bin,
                "-m",
                uvicorn_mod,
                settings.HEALTH_APP,
                "--host",
                "0.0.0.0",
                "--port",
                str(port_plan.health_port),
                "--log-level",
                "info",
            ],
            env=env.copy(),
        ),
    ]


def _write_start_command(script_text: str) -> str:
    """将 engine 启动脚本写入共享卷。"""
    shared_dir = settings.SHARED_VOLUME_PATH
    os.makedirs(shared_dir, exist_ok=True)
    path = os.path.join(shared_dir, settings.START_COMMAND_FILENAME)
    ok = safe_write_file(path, script_text, is_json=False)
    if not ok:
        raise RuntimeError(f"failed to write start command: {path}")
    logger.info("start command written: %s", path)
    return path


# ---------------------------------------------------------------------------
# 分布式模式辅助函数
# ---------------------------------------------------------------------------

def _determine_role() -> str:
    """判断当前 Pod 在分布式集群中的角色。

    通过 DISTRIBUTED 环境变量判断是否为分布式模式:
      - 非分布式 → "standalone"（沿用原有单机流程）
      - 分布式且本机 IP == MASTER_IP → "master"
      - 分布式且本机 IP != MASTER_IP → "worker"

    注意：MASTER_IP 可能是 DNS 名称（如 K8s StatefulSet headless service），
    因此比较前会尝试做 DNS 解析，避免因格式差异导致角色判断错误。

    Returns:
        "standalone" | "master" | "worker"
    """
    distributed = os.getenv("DISTRIBUTED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if not distributed:
        return "standalone"

    # 优先检查 NODE_RANK 环境变量：
    # hostNetwork 模式下同一宿主机的多个 Pod 共享 IP，
    # 无法通过 RANK_IP vs MASTER_IP 区分角色。
    # 此时显式设置 NODE_RANK 可直接确定角色，跳过 IP 比较。
    node_rank_env = os.getenv("NODE_RANK", "").strip()
    if node_rank_env:
        try:
            rank = int(node_rank_env)
            if rank == 0:
                logger.info("Role determined: MASTER (NODE_RANK=0)")
                return "master"
            else:
                logger.info("Role determined: WORKER (NODE_RANK=%d)", rank)
                return "worker"
        except ValueError:
            logger.warning("NODE_RANK=%s is not an integer, falling back to IP comparison", node_rank_env)

    master_ip = get_master_ip()
    local_ip = get_local_ip()

    if not master_ip:
        logger.warning(
            "DISTRIBUTED=true but MASTER_IP not set, falling back to standalone"
        )
        return "standalone"

    # MASTER_IP 可能是 DNS 名称（如 "infer-0.infer-hl.svc.cluster.local"），
    # 而 RANK_IP/local_ip 始终是数字 IP；需要解析后比较。
    try:
        master_resolved = socket.gethostbyname(master_ip)
    except socket.error:
        master_resolved = master_ip

    try:
        local_resolved = socket.gethostbyname(local_ip)
    except socket.error:
        local_resolved = local_ip

    if local_resolved == master_resolved:
        logger.info(
            "Role determined: MASTER (local_ip=%s, master_ip=%s, resolved=%s)",
            local_ip, master_ip, master_resolved,
        )
        return "master"

    logger.info(
        "Role determined: WORKER (local_ip=%s → %s, master_ip=%s → %s)",
        local_ip, local_resolved, master_ip, master_resolved,
    )
    return "worker"


def _get_expected_nodes() -> list[str]:
    """从 NODE_IPS 环境变量获取集群全部节点 IP 列表。

    若 NODE_IPS 未设置，回退到仅包含本机 IP 的单元素列表。
    """
    node_ips_str = get_node_ips()
    if not node_ips_str:
        return [get_local_ip()]
    return [ip.strip() for ip in node_ips_str.split(",") if ip.strip()]


def _override_distributed_args(
    launch_args: LaunchArgs,
    *,
    distributed: bool,
    nnodes: int,
    node_rank: int,
    head_node_addr: str,
) -> LaunchArgs:
    """创建 LaunchArgs 副本，覆盖分布式相关字段。

    由于 LaunchArgs 是 frozen dataclass，使用 dataclasses.replace 创建变体。
    """
    return dataclasses.replace(
        launch_args,
        distributed=distributed,
        nnodes=nnodes,
        node_rank=node_rank,
        head_node_addr=head_node_addr,
    )


def _load_distributed_config() -> dict:
    """加载 config/distributed_config.json 配置。"""
    import json
    from pathlib import Path

    config_path = Path(__file__).parent / "config" / "distributed_config.json"
    with open(config_path) as f:
        return json.load(f)


def _wait_and_distribute_to_workers(
    node_ips: list[str],
    launch_args: LaunchArgs,
    master_url: str,
) -> None:
    """后台线程：等待所有 Worker 注册后向其分发引擎启动指令。

    流程:
      1. 轮询 Master /api/nodes 接口，等待所有 worker 节点就绪（最多 5 分钟）
      2. 注册完成后，逐个向 worker 的 /api/start_engine 发送启动请求
      3. 为每个 worker 注入正确的 nnodes / node_rank / head_node_addr

    Args:
        node_ips:    全部节点 IP 列表（index 0 = master/rank0）
        launch_args: 标准化启动参数
        master_url:  Master API 地址（用于查询节点注册情况）
    """
    import requests as _requests

    dist_config = _load_distributed_config()
    worker_port = int(
        os.getenv("WORKER_PORT", str(dist_config["workers"]["port"]))
    )

    worker_ips = node_ips[1:]  # 排除 rank 0（Master 自身已处理）
    if not worker_ips:
        logger.info("No worker nodes to distribute to (single-node distributed)")
        return

    # ---- 等待所有 Worker 注册到 Master ----
    max_wait_sec = 300
    poll_interval = 5
    start_time = time.time()

    def _resolve(host: str) -> str:
        """将 DNS 名称解析为 IP 地址；已是 IP 或解析失败时原样返回。

        用于处理 NODE_IPS 中可能包含的 DNS 名称（如 'infer-1.infer-hl'），
        使其能与 Worker 注册时上报的 Pod IP 进行正确比较。
        """
        try:
            return socket.gethostbyname(host)
        except socket.error:
            return host

    while time.time() - start_time < max_wait_sec:
        try:
            resp = _requests.get(f"{master_url}/api/nodes", timeout=10)
            resp.raise_for_status()
            registered = {n["ip"] for n in resp.json().get("nodes", [])}
            # NODE_IPS may contain DNS names (e.g. "infer-1.infer-hl");
            # registered set contains actual Pod IPs. Resolve before comparison.
            resolved_workers = {_resolve(ip) for ip in worker_ips}
            if resolved_workers.issubset(registered):
                logger.info(
                    "All %d worker nodes registered with master (resolved: %s)",
                    len(worker_ips), resolved_workers,
                )
                break
        except Exception as exc:
            logger.debug("Waiting for workers to register: %s", exc)
        time.sleep(poll_interval)
    else:
        logger.error(
            "Timed out (%ds) waiting for worker registration. Expected: %s",
            max_wait_sec,
            worker_ips,
        )
        return

    # ---- 向每个 Worker 分发启动指令 ----
    nnodes = len(node_ips)
    head_addr = node_ips[0]
    base_params = launch_args.to_namespace().__dict__

    for rank, worker_ip in enumerate(worker_ips, start=1):
        params = {
            **base_params,
            "distributed": True,
            "nnodes": nnodes,
            "node_rank": rank,
            "head_node_addr": head_addr,
        }
        try:
            resp = _requests.post(
                f"http://{worker_ip}:{worker_port}/api/start_engine",
                json={"engine": params.get("engine", "vllm"), "params": params},
                timeout=30,
            )
            resp.raise_for_status()
            logger.info(
                "Distributed start command to worker rank %d (%s): %s",
                rank,
                worker_ip,
                resp.json(),
            )
        except Exception as exc:
            logger.error(
                "Failed to distribute to worker rank %d (%s): %s",
                rank,
                worker_ip,
                exc,
            )


def _run_master_mode(
    launch_args: LaunchArgs,
    port_plan: PortPlan,
) -> int:
    """Master 模式主流程。

    1. 生成 rank 0 引擎启动脚本并写入共享卷 → engine 容器自动执行
    2. 后台启动 Master FastAPI 协调服务（端口来自 distributed_config.json）
    3. 启动 proxy + health 子服务
    4. 后台等待 Worker 注册完成后分发启动指令到各 Worker
    5. 进入守护循环，监控 proxy/health 子进程状态
    """
    local_ip = get_local_ip()
    node_ips = _get_expected_nodes()
    nnodes = len(node_ips)

    # ---- 1. 生成 rank 0 脚本写入共享卷 ----
    master_args = _override_distributed_args(
        launch_args,
        distributed=True,
        nnodes=nnodes,
        node_rank=0,
        head_node_addr=local_ip,
    )
    launcher_plan = build_launcher_plan(master_args, port_plan)
    _write_start_command(launcher_plan.command)

    # ---- 2. 后台启动 Master FastAPI ----
    dist_config = _load_distributed_config()
    master_port = int(
        os.getenv("MASTER_PORT", str(dist_config["master"]["port"]))
    )
    master_url = f"http://127.0.0.1:{master_port}"

    def _run_master_api():
        import uvicorn
        from app.distributed.master import app as master_app

        uvicorn.run(master_app, host="0.0.0.0", port=master_port)

    master_thread = threading.Thread(target=_run_master_api, daemon=True)
    master_thread.start()
    logger.info("Master API starting on port %d", master_port)
    time.sleep(2)  # 等待 Master FastAPI 就绪

    # ---- 3. 启动 proxy + health 子服务 ----
    processes = _build_processes(port_plan)
    for proc in processes:
        _start(proc)

    # ---- 4. 后台等待 Worker 注册并分发 ----
    dist_thread = threading.Thread(
        target=_wait_and_distribute_to_workers,
        args=(node_ips, launch_args, master_url),
        daemon=True,
    )
    dist_thread.start()

    # ---- 5. 守护循环 ----
    stop_event = Event()

    def _on_signal(signum: int, _frame: object) -> None:
        logger.info("received signal: %s", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    logger.info(
        "Master mode running: master_api=%d backend=%d proxy=%d health=%d",
        master_port,
        port_plan.backend_port,
        port_plan.proxy_port,
        port_plan.health_port,
    )

    try:
        while not stop_event.is_set():
            for proc in processes:
                _restart_if_needed(proc)
            time.sleep(settings.PROCESS_POLL_SEC)
    finally:
        for proc in processes:
            _stop(proc)
        logger.info("Master mode shutdown complete")
    return 0


def _run_worker_mode(
    launch_args: LaunchArgs,
    port_plan: PortPlan,
) -> int:
    """Worker 模式主流程。

    1. 后台启动 Worker FastAPI 服务（自动向 Master 注册 + 心跳守护）
    2. 仅启动 health 子服务（非 rank0 不暴露 proxy）
    3. 进入守护循环
    4. 引擎启动脚本由 Master 分发后通过 Worker API 写入共享卷

    注意:
      Worker 启动时不写 start_command.sh。脚本在 Master 完成分发后由
      Worker 的 /api/start_engine 端点生成并写入共享卷。
    """
    master_ip = get_master_ip()

    # ---- 1. 后台启动 Worker FastAPI ----
    def _run_worker_api():
        from app.distributed.worker import WorkerConfig, start_worker

        worker_cfg = WorkerConfig(master_ip=master_ip)
        start_worker(worker_cfg)

    worker_thread = threading.Thread(target=_run_worker_api, daemon=True)
    worker_thread.start()
    logger.info("Worker API starting, registering with master at %s", master_ip)
    time.sleep(2)  # 等待 Worker 就绪

    # ---- 2. 启动 health 子服务（使用偏移端口避免 hostNetwork 冲突） ----
    # Worker 的 health 端口在基准端口上偏移 +1（如 19000 → 19001），
    # 避免 hostNetwork 模式下与同一宿主机上 Master Pod 的 19000 端口冲突。
    # K8s StatefulSet 中 Worker Pod 的 readinessProbe/livenessProbe 需对应配置。
    worker_health_port = port_plan.health_port + 1
    worker_port_plan = PortPlan(
        enable_proxy=port_plan.enable_proxy,
        backend_port=port_plan.backend_port,
        proxy_port=port_plan.proxy_port,
        health_port=worker_health_port,
    )
    processes = [p for p in _build_processes(worker_port_plan) if p.name == "health"]
    for proc in processes:
        _start(proc)

    # ---- 3. 守护循环 ----
    stop_event = Event()

    def _on_signal(signum: int, _frame: object) -> None:
        logger.info("received signal: %s", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    logger.info(
        "Worker mode running: health=%d "
        "(waiting for master to dispatch engine start)",
        worker_health_port,
    )

    try:
        while not stop_event.is_set():
            for proc in processes:
                _restart_if_needed(proc)
            time.sleep(settings.PROCESS_POLL_SEC)
    finally:
        for proc in processes:
            _stop(proc)
        logger.info("Worker mode shutdown complete")
    return 0


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def run(argv: Sequence[str] | None = None) -> int:
    """launcher 主流程。

    根据 _determine_role() 判断角色:
      - standalone: 沿用原有单机流程（build_launcher_plan → 写脚本 → 守护 proxy/health）
      - master:     Master 协调模式（写 rank0 脚本 + Master API + 分发 Worker）
      - worker:     Worker 等待模式（Worker API + 仅 health，等 Master 分发脚本）
    """
    launch_args = parse_launch_args(list(argv) if argv is not None else None)
    port_plan = derive_port_plan(
        port=launch_args.port,
        enable_reason_proxy=settings.ENABLE_REASON_PROXY,
        health_port=settings.HEALTH_PORT,
    )

    # 当前版本必须启用 proxy。
    if not port_plan.enable_proxy:
        logger.error("ENABLE_REASON_PROXY=false is not supported in v4 MVP")
        return 2

    # ---- 分布式角色分支 ----
    role = _determine_role()
    logger.info("Launcher role: %s", role)

    if role == "master":
        return _run_master_mode(launch_args, port_plan)
    if role == "worker":
        return _run_worker_mode(launch_args, port_plan)

    # ---- standalone 模式（原有逻辑不变） ----
    launcher_plan = build_launcher_plan(launch_args, port_plan)
    _write_start_command(launcher_plan.command)

    processes = _build_processes(port_plan)
    # 分布式场景下只有 rank0 暴露 proxy，其余 rank 保留 health 即可。
    if getattr(launch_args, "node_rank", 0) > 0:
        processes = [p for p in processes if p.name != "proxy"]

    for proc in processes:
        _start(proc)

    stop_event = Event()

    def _on_signal(signum: int, _frame: object) -> None:
        logger.info("received signal: %s", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    logger.info(
        "launcher running: backend=%s proxy=%s health=%s",
        port_plan.backend_port,
        port_plan.proxy_port,
        port_plan.health_port,
    )

    try:
        while not stop_event.is_set():
            for proc in processes:
                _restart_if_needed(proc)
            time.sleep(settings.PROCESS_POLL_SEC)
    finally:
        for proc in processes:
            _stop(proc)
        logger.info("launcher shutdown complete")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
