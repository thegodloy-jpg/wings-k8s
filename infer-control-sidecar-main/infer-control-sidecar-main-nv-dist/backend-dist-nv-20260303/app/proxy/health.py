# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: proxy/health.py
# Purpose: Health state machine that probes backend/proxy signals and computes readiness phase.
# Status: Active reused health core.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Readiness semantics must remain stable for Kubernetes probes.
# - Keep backend health probing against local engine endpoint.
# -----------------------------------------------------------------------------
# -*- coding: utf-8 -*-
"""
health.py
 /health
- /
-
-  200  /health
-
-  5s  2.5s10% / sglang
- /health  API
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




#   C
#  sglang  sglang
HEALTH_TIMEOUT_MS = int(os.getenv("HEALTH_TIMEOUT_MS", getattr(C, "HEALTH_TIMEOUT_MS", "5000")))
PRE_READY_POLL_MS = int(os.getenv("PRE_READY_POLL_MS", getattr(C, "PRE_READY_POLL_MS", "5000")))
POLL_INTERVAL_MS = int(os.getenv("POLL_INTERVAL_MS", getattr(C, "POLL_INTERVAL_MS", "5000")))
HEALTH_CACHE_MS = int(os.getenv("HEALTH_CACHE_MS", getattr(C, "HEALTH_CACHE_MS", "500")))
STARTUP_GRACE_MS = int(os.getenv("STARTUP_GRACE_MS", getattr(C, "STARTUP_GRACE_MS", "3600000")))
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", getattr(C, "FAIL_THRESHOLD", "5")))
FAIL_GRACE_MS = int(os.getenv("FAIL_GRACE_MS", getattr(C, "FAIL_GRACE_MS", "25000")))
JITTER_PCT = float(os.getenv("HEALTH_JITTER_PCT", getattr(C, "HEALTH_JITTER_PCT", "0.1")))
BACKEND_PID_FILE = os.getenv("BACKEND_PID_FILE", getattr(C, "BACKEND_PID_FILE", "/var/log/wings/wings.txt"))

#   sglang
SGLANG_FAIL_BUDGET = float(os.getenv("SGLANG_FAIL_BUDGET", getattr(C, "SGLANG_FAIL_BUDGET", "6.0")))
SGLANG_PID_GRACE_MS = int(os.getenv("SGLANG_PID_GRACE_MS", getattr(C, "SGLANG_PID_GRACE_MS", "30000")))
SGLANG_DECAY = float(os.getenv("SGLANG_DECAY", getattr(C, "SGLANG_DECAY", "0.5")))  #
#  503
SGLANG_SILENCE_MAX_MS = int(os.getenv("SGLANG_SILENCE_MAX_MS", getattr(C, "SGLANG_SILENCE_MAX_MS", "60000")))
SGLANG_CONSEC_TIMEOUT_MAX = int(os.getenv("SGLANG_CONSEC_TIMEOUT_MAX", getattr(C, "SGLANG_CONSEC_TIMEOUT_MAX", "8")))
# K8s sidecar  PID  pid_alive
#  WINGS_SKIP_PID_CHECK=true  false
WINGS_SKIP_PID_CHECK = os.getenv("WINGS_SKIP_PID_CHECK", "false").strip().lower() in ("1", "true", "yes", "on")

#

def _now() -> float:
    """monotonic """
    return time.monotonic()


def _read_pid_from_file() -> Optional[int]:
    """
     BACKEND_PID_FILE **** PID
     None //<=1
    """
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
    except Exception:
        return None


def _is_pid_alive(pid: Optional[int]) -> bool:
    """
     /proc/<pid>
    /
    """
    if pid is None:
        return False
    return os.path.exists(f"/proc/{pid}")


def _is_mindie() -> bool:
    try:
        with open(BACKEND_PID_FILE, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        return len(lines) >= 2 and lines[1].strip().lower() == "mindie"
    except Exception:
        return False


def _is_sglang() -> bool:
    """
     _is_mindie  BACKEND_PID_FILE
     'sglang' sglang
     or os.getenv("WINGS_BACKEND","").lower()=="sglang"
    """
    try:
        with open(BACKEND_PID_FILE, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        return len(lines) >= 2 and lines[1].strip().lower() == "sglang"
    except Exception:
        return False


def _force_port(url: str, host: str, port: int) -> str:
    #  http://<ip>:<port>/health
    scheme, rest = url.split("://", 1)          # "http", "<ip>:17000/health"
    hostport, path = rest.split("/", 1)         # "<ip>:17000", "health"
    return f"{scheme}://{host}:{port}/{path}"


async def _strict_probe_backend_health(client: httpx.AsyncClient) -> Tuple[bool, int, int, str]:
    """
     200  /health
    -  (True, 200, latency_ms, "")
    -  (False, http_code_or_0, latency_ms, err_kind)
      * http_code 0 /
      * err_kind  {"connect_timeout","read_timeout","connect_error","request_error",""}
    """
    url = build_backend_url("/health")
    if _is_mindie():
        url = _force_port(url, "127.0.0.2", 1026)  # mindie  1026

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
    """ HTTP  p"""
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
    """
     /health
    - status0=/1=-1=
    - ever_ready 201/502  503
    - consecutive_failures
    - last_success_ts
    - pid/pid_alive PID  /proc
    - backend_ok/backend_http_code/backend_http_latency_ms 200
    - last_observed_ts /health
    - fail_score/accum_fail_ms sglang
    - consecutive_timeouts sglang  503
    - last_error_kind/
    - warmup_executedwarmup
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
    """(no description)"""
    pid: Optional[int]
    pid_alive: bool


@dataclass
class BackendHealthResult:
    """(no description)"""
    backend_ok: bool
    http_code: int
    latency_ms: int
    err_kind: str


@dataclass
class HealthObservationData:
    """(no description)"""
    process_result: ProcessProbeResult
    health_result: BackendHealthResult
    timestamp: float


@dataclass
class SglangFailureContext:
    """Sglang """
    now: float
    pid_alive: bool
    backend_ok: bool
    http_code: int
    err_kind: str
    latency_ms: int


async def tick_observe_and_advance(h: dict, client: httpx.AsyncClient) -> None:
    """
    " + "
    1)  PID
    2)  200  /health
    3)
    4) //
    5)  sglang/PID /
    """
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
    """ PID """
    pid = _read_pid_from_file()
    pid_alive = _is_pid_alive(pid)
    return ProcessProbeResult(pid=pid, pid_alive=pid_alive)


def _refresh_observation_data(h: dict, observation: HealthObservationData) -> None:
    """(no description)"""
    h["pid"] = observation.process_result.pid
    h["pid_alive"] = observation.process_result.pid_alive
    h["backend_ok"] = observation.health_result.backend_ok
    h["backend_http_code"] = observation.health_result.http_code
    h["backend_http_latency_ms"] = observation.health_result.latency_ms
    h["last_observed_ts"] = observation.timestamp
    h["last_error_kind"] = observation.health_result.err_kind


def _advance_state_machine(h: dict, pid_alive: bool, backend_ok: bool) -> None:
    """//"""
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

        # warmupwarmup
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
    """(no description)"""
    return (h["consecutive_failures"] >= FAIL_THRESHOLD and
            h["consecutive_failures"] * HEALTH_TIMEOUT_MS >= FAIL_GRACE_MS)


def _handle_sglang_specifics(h: dict, observation: HealthObservationData) -> None:
    """ sglang """
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
        #
        # raise  #


def _update_consecutive_timeouts(h: dict, health_result: BackendHealthResult) -> None:
    """ read_timeout"""
    if not health_result.backend_ok and health_result.http_code == 0 and health_result.err_kind == "read_timeout":
        h["consecutive_timeouts"] = int(h.get("consecutive_timeouts", 0)) + 1
    else:
        h["consecutive_timeouts"] = 0 if health_result.backend_ok else h.get("consecutive_timeouts", 0)


def _handle_success_case(h: dict) -> None:
    """(no description)"""
    h["fail_score"] *= SGLANG_DECAY
    h["accum_fail_ms"] = int(h["accum_fail_ms"] * SGLANG_DECAY)


def _handle_failure_case(h: dict, observation: HealthObservationData) -> None:
    """(no description)"""
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
    """
     HTTP
      200: pid_alive && backend_ok && ever_ready  status==1
            sglang
      201: !ever_ready && elapsed < STARTUP_GRACE_MS
      502: !ever_ready && elapsed >= STARTUP_GRACE_MS
      503: //
       200 body
    """
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
    """
     sleep
    -  PRE_READY_POLL_MS 5000ms
    -  POLL_INTERVAL_MS 2500ms
    -  JITTER_PCT
    -  100ms
    - sglang  fail_score
    """
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
    """
    +
    -  warning
    - shutdown
    """
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
    """
     FastAPI startup
    -
    -
    """
    app.state.health = init_health_state()
    app.state.health_task = asyncio.create_task(health_monitor_loop(app), name="wings-health-monitor")
    C.logger.info("Health monitor loop enabled")


async def teardown_health_monitor(app) -> None:
    """
     FastAPI shutdown
    -
    -  asyncio.CancelledError
    """
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
    """
     /health
    - s0/1/-1
    - pready/starting/start_failed/degraded
    - pid_alive
    - backend_ok/backend_code /health  200
    - interruptedever_ready && status==-1
    - ever_ready
    - cf
    - lat_ms /health
    sglang  fail_score/accum_fail_ms
    """
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
    """
     /health
    - X-Wings-Status
    - Cache-Controlno-store
    """
    return {
        "X-Wings-Status": str(h["status"]),
        "Cache-Control": "no-store",
    }


async def _trigger_warmup() -> None:
    """
    chat_completionswarmup
    RAGwarmup
    """
    if not C.RAG_ACC_ENABLED:
        return

    try:
        await _send_warmup_request()
    except Exception as e:
        #
        C.logger.warning("Warmup request failed: %s", str(e))


async def _send_warmup_request() -> None:
    """warmup"""
    #
    model_name = os.getenv("MODEL_NAME", "default-model")
    proxy_port = os.getenv("PROXY_PORT", "18080")

    # HTTP
    async with httpx.AsyncClient() as client:
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
            timeout=300
        )

        #
        C.logger.info("Warmup request completed with status: %d", response.status_code)

        #
        if response.status_code == 200:
            #
            async for chunk in response.aiter_bytes(chunk_size=1024):
                break  # chunk
            await response.aclose()
