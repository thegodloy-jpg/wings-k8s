# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: main.py
# Purpose: Launcher entrypoint that orchestrates argument parsing, command artifact writing, and child-process supervision.
# Status: Active runtime entrypoint for sidecar launcher mode.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Writes startup artifact to shared volume before starting proxy/health.
# - Supervises only proxy and health; does not launch engine process directly.
# -----------------------------------------------------------------------------
from __future__ import annotations
import logging  # 标准日志模块，用于运行态输出
import os  # 操作系统接口，用于环境变量和路径处理
import signal  # 信号处理模块，用于捕获 SIGINT/SIGTERM
import subprocess  # 子进程管理模块，用于启动和监控服务进程
import sys  # 系统参数模块，用于读取 argv 并返回退出码
import time  # 时间模块，用于轮询间隔控制
from dataclasses import dataclass  # 数据类支持，简化结构定义
from threading import Event  # 线程事件，用于停止循环的同步
from typing import Sequence  # 抽象序列类型，用于函数签名
# -----------------------------------------------------------------------------  # 分隔线，区分应用内部模块导入
from app.config.settings import settings  # 运行配置，集中管理端口与路径
from app.core.port_plan import PortPlan, derive_port_plan  # 端口规划与派生逻辑
from app.core.start_args_compat import parse_launch_args  # 启动参数解析与兼容
from app.core.wings_entry import build_launcher_plan  # 生成引擎启动计划
from app.utils.file_utils import safe_write_file  # 安全写文件工具，支持原子写入
# -----------------------------------------------------------------------------  # 分隔线，配置全局日志
logging.basicConfig(  # 初始化日志系统，保证格式一致
    level=logging.INFO,  # 默认日志级别，INFO 覆盖常规运行信息
    format="%(asctime)s [%(levelname)s] [launcher] %(message)s",  # 日志格式模板
)  # 结束日志配置
logger = logging.getLogger("wings-sidecar-launcher")  # 获取命名日志器
# -----------------------------------------------------------------------------  # 分隔线，定义受控子进程数据结构
@dataclass  # 数据类装饰器，自动生成 __init__/__repr__
class ManagedProc:  # 被管理的子进程描述对象
    name: str  # 进程名称，主要用于日志输出
    argv: list[str]  # 启动参数列表，保持原样传给子进程
    env: dict[str, str]  # 子进程环境变量字典
    proc: subprocess.Popen | None = None  # 实际运行的进程句柄
# -----------------------------------------------------------------------------  # 分隔线，子进程启动逻辑
def _start(proc: ManagedProc) -> None:  # 启动指定 ManagedProc
    # 启动子进程，使用准备好的 argv 与环境变量
    logger.info("starting %s: %s", proc.name, " ".join(proc.argv))  # 输出启动日志
    proc.proc = subprocess.Popen(proc.argv, env=proc.env)  # 实际拉起子进程
# -----------------------------------------------------------------------------  # 分隔线，子进程停止逻辑
def _stop(proc: ManagedProc) -> None:  # 停止指定 ManagedProc
    if not proc.proc:  # 未启动则直接返回
        return  # 无需处理
    # 先优雅停止，超时后再强制终止
    if proc.proc.poll() is None:  # 进程仍在运行
        proc.proc.terminate()  # 发送 terminate 信号
        try:  # 捕获等待超时异常
            proc.proc.wait(timeout=10)  # 等待进程退出
        except subprocess.TimeoutExpired:  # 超时则进入强制杀死流程
            proc.proc.kill()  # 强制杀死进程
            proc.proc.wait(timeout=5)  # 再次等待确保退出
    proc.proc = None  # 清理进程句柄
# -----------------------------------------------------------------------------  # 分隔线，子进程异常重启逻辑
def _restart_if_needed(proc: ManagedProc) -> None:  # 按需重启子进程
    if not proc.proc:  # 未启动则直接启动
        _start(proc)  # 启动子进程
        return  # 启动完成后返回
    code = proc.proc.poll()  # 查询子进程是否退出
    if code is None:  # 未退出则无需处理
        return  # 保持运行状态
    # 异常退出时自动重启，保证 proxy/health 可用
    logger.warning("%s exited with code %s, restarting", proc.name, code)  # 记录重启原因
    _start(proc)  # 重启子进程
# -----------------------------------------------------------------------------  # 分隔线，构建子进程环境变量
def _build_child_env(port_plan: PortPlan) -> dict[str, str]:  # 构建子进程环境
    # 注入后端/代理/健康检查端点到子进程环境
    env = os.environ.copy()  # 继承当前环境变量
    env["BACKEND_URL"] = f"http://127.0.0.1:{port_plan.backend_port}"  # 后端访问地址
    env["BACKEND_HOST"] = "127.0.0.1"  # 后端主机地址
    env["BACKEND_PORT"] = str(port_plan.backend_port)  # 后端端口号
    env["PORT"] = str(port_plan.proxy_port)  # 兼容部分服务默认端口变量
    env["PROXY_PORT"] = str(port_plan.proxy_port)  # proxy 对外端口
    env["HEALTH_PORT"] = str(port_plan.health_port)  # health 服务端口
    env["HEALTH_SERVICE_PORT"] = str(port_plan.health_port)  # health 兼容端口变量
    return env  # 返回环境变量字典
# -----------------------------------------------------------------------------  # 分隔线，构建受控子进程列表
def _build_processes(port_plan: PortPlan) -> list[ManagedProc]:  # 构建子进程配置
    # 构建并受控管理 proxy 与 health 子进程
    env = _build_child_env(port_plan)  # 生成通用环境变量
    python_bin = settings.PYTHON_BIN  # 运行 Python 可执行路径
    uvicorn_mod = settings.UVICORN_MODULE  # uvicorn 启动模块路径
    return [  # 返回子进程配置列表
        ManagedProc(  # proxy 进程定义
            name="proxy",  # 进程名称
            argv=[  # 启动参数列表
                python_bin,  # Python 解释器
                "-m",  # 模块运行标志
                uvicorn_mod,  # uvicorn 模块
                settings.PROXY_APP,  # proxy 应用入口
                "--host",  # 监听地址参数
                "0.0.0.0",  # 监听所有地址
                "--port",  # 端口参数
                str(port_plan.proxy_port),  # proxy 端口值
                "--log-level",  # 日志级别参数
                "info",  # 日志级别值
            ],  # 结束 argv 列表
            env=env.copy(),  # 使用独立 env 副本
        ),  # 结束 proxy 定义
        ManagedProc(  # health 进程定义
            name="health",  # 进程名称
            argv=[  # 启动参数列表
                python_bin,  # Python 解释器
                "-m",  # 模块运行标志
                uvicorn_mod,  # uvicorn 模块
                settings.HEALTH_APP,  # health 应用入口
                "--host",  # 监听地址参数
                "0.0.0.0",  # 监听所有地址
                "--port",  # 端口参数
                str(port_plan.health_port),  # health 端口值
                "--log-level",  # 日志级别参数
                "info",  # 日志级别值
            ],  # 结束 argv 列表
            env=env.copy(),  # 使用独立 env 副本
        ),  # 结束 health 定义
    ]  # 结束列表返回
# -----------------------------------------------------------------------------  # 分隔线，写入共享卷启动命令
def _write_start_command(script_text: str) -> str:  # 写入启动命令文本
    # 写入共享卷启动命令，供执行层消费
    shared_dir = settings.SHARED_VOLUME_PATH  # 共享卷根路径
    os.makedirs(shared_dir, exist_ok=True)  # 确保目录存在
    path = os.path.join(shared_dir, settings.START_COMMAND_FILENAME)  # 目标文件路径
    ok = safe_write_file(path, script_text, is_json=False)  # 安全写入文件
    if not ok:  # 写入失败时抛错
        raise RuntimeError(f"failed to write start command: {path}")  # 明确错误信息
    logger.info("start command written: %s", path)  # 写入成功日志
    return path  # 返回写入路径
# -----------------------------------------------------------------------------  # 分隔线，主运行入口
def run(argv: Sequence[str] | None = None) -> int:  # 运行入口函数
    # 解析启动参数并推导一致的端口规划
    launch_args = parse_launch_args(list(argv) if argv is not None else None)  # 解析命令行参数
    port_plan = derive_port_plan(  # 生成端口规划
        port=launch_args.port,  # 传入主端口
        enable_reason_proxy=settings.ENABLE_REASON_PROXY,  # 是否启用 proxy
        health_port=settings.HEALTH_PORT,  # health 端口
    )  # 结束端口规划
    if not port_plan.enable_proxy:  # 当前 MVP 必须启用 proxy
        logger.error("ENABLE_REASON_PROXY=false is not supported in v4 MVP")  # 输出错误日志
        return 2  # 返回非 0 退出码
    # 生成引擎启动命令并写入共享卷
    launcher_plan = build_launcher_plan(launch_args, port_plan)  # 构建启动计划
    _write_start_command(launcher_plan.command)  # 写入共享卷命令
    processes = _build_processes(port_plan)  # 构建子进程列表
    for proc in processes:  # 遍历启动子进程
        _start(proc)  # 启动子进程
    # 注册信号处理，触发优雅退出
    stop_event = Event()  # 创建停止事件
    def _on_signal(signum: int, _frame: object) -> None:  # 定义信号回调
        logger.info("received signal: %s", signum)  # 输出收到的信号
        stop_event.set()  # 触发停止事件
    signal.signal(signal.SIGINT, _on_signal)  # 绑定 SIGINT 信号
    signal.signal(signal.SIGTERM, _on_signal)  # 绑定 SIGTERM 信号
    logger.info(  # 输出启动完成信息
        "launcher running: backend=%s proxy=%s health=%s",  # 日志模板
        port_plan.backend_port,  # 后端端口
        port_plan.proxy_port,  # proxy 端口
        port_plan.health_port,  # health 端口
    )  # 结束日志输出
    try:  # 进入运行循环
        # 监督循环：子进程异常退出则重启
        while not stop_event.is_set():  # 未收到停止事件则持续运行
            for proc in processes:  # 遍历子进程列表
                _restart_if_needed(proc)  # 按需重启子进程
            time.sleep(settings.PROCESS_POLL_SEC)  # 轮询间隔
    finally:  # 无论异常或退出都执行清理
        # 退出前尽力清理子进程
        for proc in processes:  # 遍历子进程
            _stop(proc)  # 停止子进程
        logger.info("launcher shutdown complete")  # 输出关闭完成日志
    return 0  # 正常退出码
# -----------------------------------------------------------------------------  # 分隔线，脚本入口
if __name__ == "__main__":  # 仅当直接运行脚本时执行
    sys.exit(run(sys.argv[1:]))  # 传入命令行参数并退出