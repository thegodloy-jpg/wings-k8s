# =============================================================================
# 文件: proxy/health.py
# 用途: 健康状态机核心，探测后端/代理信号并计算就绪阶段
# 状态: 活跃，复用自 wings 项目的健康检查核心
#
# 功能概述:
#   本模块不直接提供 HTTP 服务，而是维护一套持续更新的健康状态：
#   - 读取 PID 文件，判断 backend 进程是否仍存活
#   - 主动探测 backend 的 /health 端点
#   - 根据启动阶段、历史成功记录和失败累积计算 ready/degraded 状态
#   - 与 gateway/health_service 提供统一的状态映射和响应内容
#
# 状态机设计:
#   status:  0=初始化/启动中, 1=就绪, -1=降级
#   转换规则:
#     0 -> 1 : PID 存活 + /health 返回 200
#     1 -> -1: 连续失败超过 FAIL_THRESHOLD 且超过 FAIL_GRACE_MS
#
# sglang 专用逻辑:
#   - sglang 流式场景下超时更常见，采用“宽容但可退化”计分机制
#   - fail_score 累积到 SGLANG_FAIL_BUDGET 时触发 503
#   - 连续超时数达到 SGLANG_CONSEC_TIMEOUT_MAX 时触发 503
#
# Sidecar 架构契约:
#   - 就绪语义必须保持稳定，供 Kubernetes 探针使用
#   - 后端健康探测对准本地 engine 端点
#
# =============================================================================
# -*- coding: utf-8 -*-
"""健康状态机核心。

这个模块不直接提供 HTTP 服务，而是维护一套持续更新的健康状态：
- 读取 PID 文件，判断 backend 进程是否仍存活；
- 主动探测 backend 的 `/health`；
- 根据启动阶段、历史成功记录和失败累积结果计算 ready/degraded 状态；
- 与 gateway/health_service 提供统一的状态映射和响应内容。
"""

from __future__ import annotations
import asyncio
import contextlib
import os
import random
import time
from typing import Optional, Tuple, Dict
from dataclasses import dataclass
import httpx


from app.proxy import settings as C
from app.proxy.tags import build_backend_url


# 下面这些参数大多与健康状态机的时间窗口有关，实际部署时可通过环境变量调优。
# 对 sglang 做了更多“宽容但可退化”的判定，因为其流式场景下超时更常见。
HEALTH_TIMEOUT_MS = int(os.getenv("HEALTH_TIMEOUT_MS", getattr(C, "HEALTH_TIMEOUT_MS", "5000")))
PRE_READY_POLL_MS = int(os.getenv("PRE_READY_POLL_MS", getattr(C, "PRE_READY_POLL_MS", "5000")))
POLL_INTERVAL_MS = int(os.getenv("POLL_INTERVAL_MS", getattr(C, "POLL_INTERVAL_MS", "5000")))
HEALTH_CACHE_MS = int(os.getenv("HEALTH_CACHE_MS", getattr(C, "HEALTH_CACHE_MS", "500")))
STARTUP_GRACE_MS = int(os.getenv("STARTUP_GRACE_MS", getattr(C, "STARTUP_GRACE_MS", "3600000")))
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", getattr(C, "FAIL_THRESHOLD", "5")))
FAIL_GRACE_MS = int(os.getenv("FAIL_GRACE_MS", getattr(C, "FAIL_GRACE_MS", "25000")))
JITTER_PCT = float(os.getenv("HEALTH_JITTER_PCT", getattr(C, "HEALTH_JITTER_PCT", "0.1")))
BACKEND_PID_FILE = os.getenv("BACKEND_PID_FILE", getattr(C, "BACKEND_PID_FILE", "/var/log/wings/wings.txt"))

# sglang 专用阈值。
SGLANG_FAIL_BUDGET = float(os.getenv("SGLANG_FAIL_BUDGET", getattr(C, "SGLANG_FAIL_BUDGET", "6.0")))
SGLANG_PID_GRACE_MS = int(os.getenv("SGLANG_PID_GRACE_MS", getattr(C, "SGLANG_PID_GRACE_MS", "30000")))
SGLANG_DECAY = float(os.getenv("SGLANG_DECAY", getattr(C, "SGLANG_DECAY", "0.5")))
SGLANG_SILENCE_MAX_MS = int(os.getenv("SGLANG_SILENCE_MAX_MS", getattr(C, "SGLANG_SILENCE_MAX_MS", "60000")))
SGLANG_CONSEC_TIMEOUT_MAX = int(os.getenv("SGLANG_CONSEC_TIMEOUT_MAX", getattr(C, "SGLANG_CONSEC_TIMEOUT_MAX", "8")))

# sidecar 场景下 engine 不一定是当前容器的子进程，因此允许跳过 PID 校验。
WINGS_SKIP_PID_CHECK = os.getenv("WINGS_SKIP_PID_CHECK", "false").strip().lower() in ("1", "true", "yes", "on")


def _now() -> float:
    """统一使用单调时钟，避免系统时间跳变影响状态机。"""
    return time.monotonic()


def _read_pid_from_file() -> Optional[int]:
    """从 PID 文件第一行读取 backend PID。"""
    try:
        with open(BACKEND_PID_FILE, "r", encoding="utf-8") as f:
            first = f.readline()  #
        if not first:
            return None
        s = first.lstrip("\ufeff").split("#", 1)[0].strip()  #  BOM
        if not s.isdigit():
            return None
        pid = int(s)
        return pid if pid > 1 else None
    except FileNotFoundError:
        return None
    except Exception as e:
        C.logger.debug("pid_file_read_error: %s", e)
        return None


def _is_pid_alive(pid: Optional[int]) -> bool:
    """通过 `/proc/<pid>` 判断进程是否存在。"""
    if pid is None:
        return False
    return os.path.exists(f"/proc/{pid}")


def _is_mindie() -> bool:
    # PID 文件第二行约定保存 backend 类型，MindIE 会用特殊 health 端口探测。
    try:
        with open(BACKEND_PID_FILE, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        return len(lines) >= 2 and lines[1].strip().lower() == "mindie"
    except Exception as e:
        C.logger.debug("pid_file_mindie_check_error: %s", e)
        return False


def _is_sglang() -> bool:
    """根据 PID 文件第二行判断当前 backend 是否为 sglang。"""
    try:
        with open(BACKEND_PID_FILE, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        return len(lines) >= 2 and lines[1].strip().lower() == "sglang"
    except Exception as e:
        C.logger.debug("pid_file_sglang_check_error: %s", e)
        return False


def _force_port(url: str, host: str, port: int) -> str:
    # 把原 URL 的 host/port 强制替换掉，主要给 MindIE 健康探测使用。
    scheme, rest = url.split("://", 1)          # "http", "<ip>:17000/health"
    hostport, path = rest.split("/", 1)         # "<ip>:17000", "health"
    return f"{scheme}://{host}:{port}/{path}"


async def _strict_probe_backend_health(client: httpx.AsyncClient) -> Tuple[bool, int, int, str]:
    """严格探测 backend `/health` 端点，返回 (ok, http_code, latency_ms, err_kind) 四元组。"""
    url = build_backend_url("/health")
    if _is_mindie():
        url = _force_port(url, os.getenv("MINDIE_HEALTH_HOST", "127.0.0.2"), int(os.getenv("MINDIE_HEALTH_PORT", "1026")))  # mindie health probe

    t0 = time.perf_counter()
    code = 0
    err_kind = "request_error"
    try:
        resp = await client.get(
            url,
            timeout=httpx.Timeout(
                connect=HEALTH_TIMEOUT_MS / 1000.0,
                read=HEALTH_TIMEOUT_MS / 1000.0,
                write=None,
                pool=None,
            ),
            headers={"X-Proxy-Probe": "1"},
        )
        code = resp.status_code
        await resp.aclose()
        ok = (code == 200)  #  200 ""
    except httpx.ConnectTimeout:
        ok, code, err_kind = False, 0, "connect_timeout"
    except httpx.ReadTimeout:
        ok, code, err_kind = False, 0, "read_timeout"
    except httpx.ConnectError:
        ok, code, err_kind = False, 0, "connect_error"
    except httpx.RequestError as e:
        C.logger.debug("backend_health_probe_error: %s", e.__class__.__name__)
        ok, code, err_kind = False, 0, "request_error"
    else:
        #  err_kind
        err_kind = ""

    latency_ms = int((time.perf_counter() - t0) * 1000)
    return ok, code, latency_ms, err_kind


def _phase_from_code(code: int) -> str:
    """将 HTTP 状态码映射成可读的阶段字符串（ready/starting/start_failed/degraded）。"""
    return {
        200: "ready",
        201: "starting",
        502: "start_failed",
        503: "degraded",
    }.get(code, "unknown")


#  sglang  /  /

def _sglang_weight(http_code: int, err_kind: str) -> float:
    """

      - http_code==0 err_kind
          connect_error  1.0
          connect_timeout  0.75
          read_timeout  0.25
           request_error  0.5
      - http_code==503  1.0
      -  5xx  0.5
      - 2xx  0.0
    """
    if http_code == 0:
        if err_kind == "connect_error":
            return 1.0
        if err_kind == "connect_timeout":
            return 0.75
        if err_kind == "read_timeout":
            return 0.25
        return 0.5
    if http_code == 503:
        return 1.0
    if 500 <= http_code < 600:
        return 0.5
    return 0.0


def _sglang_pid_grace(context: SglangFailureContext, h: dict) -> bool:
    """
    PID  &
      -  pid_alive  backend_ok=False  http_code==0  err_kind=read_timeout
      -  now - last_success_ts  SGLANG_PID_GRACE_MS
    """
    if not context.pid_alive or context.backend_ok:
        return False
    if not (context.http_code == 0 and context.err_kind == "read_timeout"):
        return False
    last_ok = h.get("last_success_ts")
    if last_ok is None:
        return False
    return (context.now - last_ok) * 1000.0 <= SGLANG_PID_GRACE_MS


#

def init_health_state() -> dict:
    """初始化健康状态字典。

    这里保存的不只是"当前是否健康"，还包括：
    - 是否曾经 ready 过；
    - 连续失败次数；
    - 最近一次成功时间；
    - sglang 专属的 fail_score / timeout 累积信息；
    - 是否已经执行过 warmup。
    """
    return {
        "first_seen": _now(),
        "status": 0,                    # 0=/, 1=, -1=
        "ever_ready": False,
        "last_success_ts": None,
        "consecutive_failures": 0,

        "pid": None,
        "pid_alive": False,
        "backend_ok": False,
        "backend_http_code": 0,
        "backend_http_latency_ms": 0,
        "last_observed_ts": 0.0,

        # sglang
        "fail_score": 0.0,
        "accum_fail_ms": 0,
        "consecutive_timeouts": 0,
        "last_error_kind": "",

        # warmup
        "warmup_executed": False,
    }


@dataclass
class ProcessProbeResult:
    """一次进程探测结果：PID + 进程活跃标志。"""
    pid: Optional[int]
    pid_alive: bool


@dataclass
class BackendHealthResult:
    """一次后端 `/health` 探测结果：成功标志 + HTTP 码 + 延迟 + 错误类型。"""
    backend_ok: bool
    http_code: int
    latency_ms: int
    err_kind: str


@dataclass
class HealthObservationData:
    """一次完整观测：进程状态 + HTTP 健康探测结果 + 时间戳。"""
    process_result: ProcessProbeResult
    health_result: BackendHealthResult
    timestamp: float


@dataclass
class SglangFailureContext:
    """sglang 失败处理时需要的上下文，用于计算宽限期和积分权重。"""
    now: float
    pid_alive: bool
    backend_ok: bool
    http_code: int
    err_kind: str
    latency_ms: int


async def tick_observe_and_advance(h: dict, client: httpx.AsyncClient) -> None:
    """执行一次完整的健康观测并推进内部状态机：读进程→探测/health→刷新字典→推进状态。"""
    # 1)
    process_result = _probe_process()

    # 2)
    backend_ok, http_code, latency_ms, err_kind = await _strict_probe_backend_health(client)
    health_result = BackendHealthResult(
        backend_ok=backend_ok,
        http_code=http_code,
        latency_ms=latency_ms,
        err_kind=err_kind
    )

    #
    observation = HealthObservationData(
        process_result=process_result,
        health_result=health_result,
        timestamp=_now()
    )

    # 3)
    _refresh_observation_data(h, observation)

    # 4)
    _advance_state_machine(h, process_result.pid_alive, health_result.backend_ok)

    # 5) sglang
    if _is_sglang():
        _handle_sglang_specifics(h, observation)


def _probe_process() -> ProcessProbeResult:
    """读取 PID 文件并检查对应进程是否仍存在。"""
    pid = _read_pid_from_file()
    pid_alive = _is_pid_alive(pid)
    return ProcessProbeResult(pid=pid, pid_alive=pid_alive)


def _refresh_observation_data(h: dict, observation: HealthObservationData) -> None:
    """把本次观测结果写回状态字典，供后续状态机和 HTTP 响应使用。"""
    h["pid"] = observation.process_result.pid
    h["pid_alive"] = observation.process_result.pid_alive
    h["backend_ok"] = observation.health_result.backend_ok
    h["backend_http_code"] = observation.health_result.http_code
    h["backend_http_latency_ms"] = observation.health_result.latency_ms
    h["last_observed_ts"] = observation.timestamp
    h["last_error_kind"] = observation.health_result.err_kind


def _advance_state_machine(h: dict, pid_alive: bool, backend_ok: bool) -> None:
    """根据进程和后端探测结果推进主状态机（starting→ready→degraded）。"""
    # K8s sidecar  PID  pid_alive
    effective_pid_ok = True if WINGS_SKIP_PID_CHECK else pid_alive
    if effective_pid_ok and backend_ok:
        #
        # warmup
        first_time_ready = not h["ever_ready"]
        h["status"] = 1
        h["ever_ready"] = True
        h["consecutive_failures"] = 0
        h["last_success_ts"] = _now()

                # 首次进入 ready 后只触发一次 warmup，避免反复打热身流量。
        if first_time_ready and not h["warmup_executed"]:
            h["warmup_executed"] = True
            # warmup
            asyncio.create_task(_trigger_warmup())
    else:
        #
        if h["status"] == 1:
            h["consecutive_failures"] += 1
            #  sglang
            if (_should_degrade(h)):
                h["status"] = -1


def _should_degrade(h: dict) -> bool:
    """判断是否应该从 ready 状态降级到 degraded。"""
    return (h["consecutive_failures"] >= FAIL_THRESHOLD and
            h["consecutive_failures"] * HEALTH_TIMEOUT_MS >= FAIL_GRACE_MS)


def _handle_sglang_specifics(h: dict, observation: HealthObservationData) -> None:
    """处理 sglang 专属的失败积分累积和 PID 宽限逻辑。"""
    try:

        #
        _update_consecutive_timeouts(h, observation.health_result)

        if observation.process_result.pid_alive and observation.health_result.backend_ok:
            #
            _handle_success_case(h)
        else:
            #  PID ""
            _handle_failure_case(h, observation)

    except Exception as e:
        C.logger.error("Error in sglang specifics handling: %s", str(e))
        raise


def _update_consecutive_timeouts(h: dict, health_result: BackendHealthResult) -> None:
    """单独跟踪 sglang 的连续读超时次数（用于触发宽限期判断）。"""
    if not health_result.backend_ok and health_result.http_code == 0 and health_result.err_kind == "read_timeout":
        h["consecutive_timeouts"] = int(h.get("consecutive_timeouts", 0)) + 1
    else:
        h["consecutive_timeouts"] = 0 if health_result.backend_ok else h.get("consecutive_timeouts", 0)


def _handle_success_case(h: dict) -> None:
    """sglang 成功后让历史失败积分指数衰减（SGLANG_DECAY 倍）。"""
    h["fail_score"] *= SGLANG_DECAY
    h["accum_fail_ms"] = int(h["accum_fail_ms"] * SGLANG_DECAY)


def _handle_failure_case(h: dict, observation: HealthObservationData) -> None:
    """sglang 失败后根据失败类型累积积分（fail_score）和累计时长（accum_fail_ms）。"""
    context = SglangFailureContext(
        now=_now(),
        pid_alive=observation.process_result.pid_alive,
        backend_ok=observation.health_result.backend_ok,
        http_code=observation.health_result.http_code,
        err_kind=observation.health_result.err_kind,
        latency_ms=observation.health_result.latency_ms
    )

    #  PID ""
    if not _sglang_pid_grace(context, h):
        w = _sglang_weight(context.http_code, context.err_kind)
        if w > 0.0:
            h["fail_score"] += w
            h["accum_fail_ms"] += int(w * min(context.latency_ms, HEALTH_TIMEOUT_MS))


def map_http_code_from_state(h: dict) -> int:
    """把内部状态机映射成对外 HTTP 状态码（200/201/502/503）。"""
    now = _now()
    elapsed_ms = int((now - h["first_seen"]) * 1000)

    # K8s sidecar  pid_alive
    effective_pid_ok = True if WINGS_SKIP_PID_CHECK else h["pid_alive"]
    is_ready = (
        effective_pid_ok and
        h["backend_ok"] and
        h["ever_ready"] and
        h["status"] == 1
    )
    if is_ready:
        return 200

    #  201/502
    if not h["ever_ready"]:
        return 201 if elapsed_ms < STARTUP_GRACE_MS else 502

    #  sglang  503  +
    if _is_sglang():
        last_ok = h.get("last_success_ts")
        silence_hit = False
        consec_to_hit = False
        if last_ok is not None:
            since_last_ok_ms = (now - last_ok) * 1000.0
            silence_hit = since_last_ok_ms >= SGLANG_SILENCE_MAX_MS
            consec_to_hit = (
                h.get("consecutive_timeouts", 0) >= SGLANG_CONSEC_TIMEOUT_MAX
                and since_last_ok_ms >= SGLANG_PID_GRACE_MS
            )

        budget_hit = (h.get("fail_score", 0.0) >= SGLANG_FAIL_BUDGET or
                      h.get("accum_fail_ms", 0) >= FAIL_GRACE_MS)

        #   503 PID
        if silence_hit or consec_to_hit or budget_hit:
            return 503

        # PID  backend
        if h["pid_alive"] and not h["backend_ok"]:
            return 200

        #  200body
        return 200

    #   sglang
    if (h["consecutive_failures"] >= FAIL_THRESHOLD) and \
       (h["consecutive_failures"] * HEALTH_TIMEOUT_MS >= FAIL_GRACE_MS):
        return 503
    return 200


#

def _jittered_sleep_base(h: dict) -> float:
    """计算下一次轮询间隔（秒），并引入少量抖动避免多 worker 探测齐步走。"""
    base_ms = PRE_READY_POLL_MS if not h["ever_ready"] else POLL_INTERVAL_MS

    if _is_sglang():
        fs = float(h.get("fail_score", 0.0))
        if fs >= max(0.0, SGLANG_FAIL_BUDGET - 1.0):
            base_ms = max(base_ms, 9000)   # /~9-10s
        elif fs >= 2.0:
            base_ms = max(base_ms, 5000)   # ~5s

    r = 1.0 + random.uniform(-JITTER_PCT, JITTER_PCT)
    return max(100.0, base_ms * r) / 1000.0


async def health_monitor_loop(app) -> None:
    """后台持续轮询健康状态，直到被取消。"""
    try:
        while True:
            try:
                await tick_observe_and_advance(app.state.health, app.state.client)
            except Exception as e:
                C.logger.warning("health_monitor_error: %s", e)
            await asyncio.sleep(_jittered_sleep_base(app.state.health))
    except asyncio.CancelledError:
        C.logger.info("health_monitor_loop cancelled")
        raise


#   API

def setup_health_monitor(app) -> None:
    """在 FastAPI startup 阶段初始化健康状态并启动后台轮询任务。"""
    app.state.health = init_health_state()
    app.state.health_task = asyncio.create_task(health_monitor_loop(app), name="wings-health-monitor")
    C.logger.info("Health monitor loop enabled")


async def teardown_health_monitor(app) -> None:
    """在 FastAPI shutdown 阶段取消并等待后台健康轮询任务退出。"""
    try:
        task = getattr(app.state, "health_task", None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            app.state.health_task = None
    except Exception as e:
        #
        C.logger.warning("Health monitor teardown encountered an error: %s", str(e))


def build_health_body(h: dict, code: int) -> dict:
    """构造 `/health` 接口的 JSON 响应体，包含状态机各维度信息。"""
    return {
        "s": h["status"],
        "p": _phase_from_code(code),
        "pid_alive": h["pid_alive"],
        "backend_ok": h["backend_ok"],
        "backend_code": h["backend_http_code"],
        "interrupted": (h["ever_ready"] and h["status"] == -1),
        "ever_ready": h["ever_ready"],
        "cf": h["consecutive_failures"],
        "lat_ms": h["backend_http_latency_ms"],
    }


def build_health_headers(h: dict) -> dict:
    """构造 `/health` 响应头（含状态标记和禁缓存指令）。"""
    return {
        "X-Wings-Status": str(h["status"]),
        "Cache-Control": "no-store",
    }


async def _trigger_warmup() -> None:
    """在首次 ready 后触发一次可选的 RAG 预热请求（仅当 RAG_ACC_ENABLED=true 时生效）。"""
    if not C.RAG_ACC_ENABLED:
        return

    try:
        await _send_warmup_request()
    except Exception as e:
        #
        C.logger.warning("Warmup request failed: %s", str(e))


async def _send_warmup_request() -> None:
    """向本地 proxy 发送 RAG 预热请求，加速 KV cache 初始化。"""
    #
    model_name = os.getenv("MODEL_NAME", "default-model")
    proxy_port = os.getenv("PROXY_PORT", "18000")

    # HTTP
    connect_timeout = float(os.getenv("WARMUP_CONNECT_TIMEOUT", "10"))
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=connect_timeout, read=int(os.getenv("WARMUP_REQUEST_TIMEOUT", "300")), write=10.0, pool=5.0)) as client:
        # warmup
        warmup_data = {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": "/rag_acc_warm_up"
                }
            ],
            "stream": True
        }

        url = f"http://127.0.0.1:{proxy_port}/v1/chat/completions"
        headers = {
            "content-type": "application/json",
            "accept-encoding": "identity",
            "connection": "keep-alive",
        }

        # warmup
        C.logger.info("Sending warmup request to %s with model: %s", url, model_name)
        response = await client.post(
            url,
            json=warmup_data,
            headers=headers,
            timeout=int(os.getenv("WARMUP_REQUEST_TIMEOUT", "300"))
        )

        #
        C.logger.info("Warmup request completed with status: %d", response.status_code)

        #
        if response.status_code == 200:
            #
            async for chunk in response.aiter_bytes(chunk_size=1024):
                break  # chunk
            await response.aclose()

