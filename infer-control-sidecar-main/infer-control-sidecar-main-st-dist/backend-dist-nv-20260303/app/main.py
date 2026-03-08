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
import logging  #
import os  #
import signal  #  SIGINT/SIGTERM
import subprocess  #
import sys  #  argv
import time  #
from dataclasses import dataclass  #
from threading import Event  #
from typing import Sequence  #
# -----------------------------------------------------------------------------  #
from app.config.settings import settings  #
from app.core.port_plan import PortPlan, derive_port_plan  #
from app.core.start_args_compat import parse_launch_args  #
from app.core.wings_entry import build_launcher_plan  #
from app.utils.file_utils import safe_write_file  #
# -----------------------------------------------------------------------------  #
logging.basicConfig(  #
    level=logging.INFO,  # INFO
    format="%(asctime)s [%(levelname)s] [launcher] %(message)s",  #
)  #
logger = logging.getLogger("wings-sidecar-launcher")  #
# -----------------------------------------------------------------------------  #
@dataclass  #  __init__/__repr__
class ManagedProc:  #
    name: str  #
    argv: list[str]  #
    env: dict[str, str]  #
    proc: subprocess.Popen | None = None  #
# -----------------------------------------------------------------------------  #
def _start(proc: ManagedProc) -> None:  #  ManagedProc
    #  argv
    logger.info("starting %s: %s", proc.name, " ".join(proc.argv))  #
    proc.proc = subprocess.Popen(proc.argv, env=proc.env)  #
# -----------------------------------------------------------------------------  #
def _stop(proc: ManagedProc) -> None:  #  ManagedProc
    if not proc.proc:  #
        return  #
    #
    if proc.proc.poll() is None:  #
        proc.proc.terminate()  #  terminate
        try:  #
            proc.proc.wait(timeout=10)  #
        except subprocess.TimeoutExpired:  #
            proc.proc.kill()  #
            proc.proc.wait(timeout=5)  #
    proc.proc = None  #
# -----------------------------------------------------------------------------  #
def _restart_if_needed(proc: ManagedProc) -> None:  #
    if not proc.proc:  #
        _start(proc)  #
        return  #
    code = proc.proc.poll()  #
    if code is None:  #
        return  #
    #  proxy/health
    logger.warning("%s exited with code %s, restarting", proc.name, code)  #
    _start(proc)  #
# -----------------------------------------------------------------------------  #
def _build_child_env(port_plan: PortPlan) -> dict[str, str]:  #
    # //
    env = os.environ.copy()  #
    # MindIE may bind to the node IP rather than 0.0.0.0 -- use the
    # actual node IP (derived from NODE_IPS / NODE_RANK) so the proxy
    # can reach the engine.  Fall back to 127.0.0.1 for non-distributed.
    node_ips = os.getenv("NODE_IPS", "").split(",")
    node_rank = int(os.getenv("NODE_RANK", "0"))
    backend_host = (node_ips[node_rank].strip()
                    if node_rank < len(node_ips) and node_ips[0]
                    else "127.0.0.1")
    env["BACKEND_URL"] = f"http://{backend_host}:{port_plan.backend_port}"  #
    env["BACKEND_HOST"] = backend_host  #
    env["BACKEND_PORT"] = str(port_plan.backend_port)  #
    env["PORT"] = str(port_plan.proxy_port)  #
    env["PROXY_PORT"] = str(port_plan.proxy_port)  # proxy
    env["HEALTH_PORT"] = str(port_plan.health_port)  # health
    env["HEALTH_SERVICE_PORT"] = str(port_plan.health_port)  # health
    return env  #
# -----------------------------------------------------------------------------  #
def _build_processes(port_plan: PortPlan) -> list[ManagedProc]:  #
    #  proxy  health
    env = _build_child_env(port_plan)  #
    python_bin = settings.PYTHON_BIN  #  Python
    uvicorn_mod = settings.UVICORN_MODULE  # uvicorn
    return [  #
        ManagedProc(  # proxy
            name="proxy",  #
            argv=[  #
                python_bin,  # Python
                "-m",  #
                uvicorn_mod,  # uvicorn
                settings.PROXY_APP,  # proxy
                "--host",  #
                "0.0.0.0",  #
                "--port",  #
                str(port_plan.proxy_port),  # proxy
                "--log-level",  #
                "info",  #
            ],  #  argv
            env=env.copy(),  #  env
        ),  #  proxy
        ManagedProc(  # health
            name="health",  #
            argv=[  #
                python_bin,  # Python
                "-m",  #
                uvicorn_mod,  # uvicorn
                settings.HEALTH_APP,  # health
                "--host",  #
                "0.0.0.0",  #
                "--port",  #
                str(port_plan.health_port),  # health
                "--log-level",  #
                "info",  #
            ],  #  argv
            env=env.copy(),  #  env
        ),  #  health
    ]  #
# -----------------------------------------------------------------------------  #
def _write_start_command(script_text: str) -> str:  #
    #
    shared_dir = settings.SHARED_VOLUME_PATH  #
    os.makedirs(shared_dir, exist_ok=True)  #
    path = os.path.join(shared_dir, settings.START_COMMAND_FILENAME)  #
    ok = safe_write_file(path, script_text, is_json=False)  #
    if not ok:  #
        raise RuntimeError(f"failed to write start command: {path}")  #
    logger.info("start command written: %s", path)  #
    return path  #
# -----------------------------------------------------------------------------  #
def run(argv: Sequence[str] | None = None) -> int:  #
    #
    launch_args = parse_launch_args(list(argv) if argv is not None else None)  #
    port_plan = derive_port_plan(  #
        port=launch_args.port,  #
        enable_reason_proxy=settings.ENABLE_REASON_PROXY,  #  proxy
        health_port=settings.HEALTH_PORT,  # health
    )  #
    if not port_plan.enable_proxy:  #  MVP  proxy
        logger.error("ENABLE_REASON_PROXY=false is not supported in v4 MVP")  #
        return 2  #  0
    #
    launcher_plan = build_launcher_plan(launch_args, port_plan)  #
    _write_start_command(launcher_plan.command)  #
    processes = _build_processes(port_plan)
    if getattr(launch_args, 'node_rank', 0) > 0:
        processes = [p for p in processes if p.name != 'proxy']
    for proc in processes:  #
        _start(proc)  #
    #
    stop_event = Event()  #
    def _on_signal(signum: int, _frame: object) -> None:  #
        logger.info("received signal: %s", signum)  #
        stop_event.set()  #
    signal.signal(signal.SIGINT, _on_signal)  #  SIGINT
    signal.signal(signal.SIGTERM, _on_signal)  #  SIGTERM
    logger.info(  #
        "launcher running: backend=%s proxy=%s health=%s",  #
        port_plan.backend_port,  #
        port_plan.proxy_port,  # proxy
        port_plan.health_port,  # health
    )  #
    try:  #
        #
        while not stop_event.is_set():  #
            for proc in processes:  #
                _restart_if_needed(proc)  #
            time.sleep(settings.PROCESS_POLL_SEC)  #
    finally:  #
        #
        for proc in processes:  #
            _stop(proc)  #
        logger.info("launcher shutdown complete")  #
    return 0  #
# -----------------------------------------------------------------------------  #
if __name__ == "__main__":  #
    sys.exit(run(sys.argv[1:]))  #

