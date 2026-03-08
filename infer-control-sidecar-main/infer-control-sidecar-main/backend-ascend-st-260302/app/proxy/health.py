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
细粒度 /health 实现的全部逻辑与后台健康监控循环：
- 环境变量/配置常量
- 状态结构与初始化
- 严格 200 的后端 /health 探测（区分异常类型）
- 状态机推进、状态码映射、返回体构造
- 后台循环（未就绪 5s 一次，曾就绪 2.5s±10% / sglang 自适应）
- /health 路由在主文件中，仅调用本模块提供的 API
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
 



# ───────────────────────── 配置常量（可被环境或 C 覆盖） ─────────────────────────
# 保留原有含义与默认值；新增 sglang 专用常量仅在 sglang 分支生效。
HEALTH_TIMEOUT_MS = int(os.getenv("HEALTH_TIMEOUT_MS", getattr(C, "HEALTH_TIMEOUT_MS", "5000")))
PRE_READY_POLL_MS = int(os.getenv("PRE_READY_POLL_MS", getattr(C, "PRE_READY_POLL_MS", "5000")))
POLL_INTERVAL_MS = int(os.getenv("POLL_INTERVAL_MS", getattr(C, "POLL_INTERVAL_MS", "5000")))
HEALTH_CACHE_MS = int(os.getenv("HEALTH_CACHE_MS", getattr(C, "HEALTH_CACHE_MS", "500")))
STARTUP_GRACE_MS = int(os.getenv("STARTUP_GRACE_MS", getattr(C, "STARTUP_GRACE_MS", "3600000")))
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", getattr(C, "FAIL_THRESHOLD", "5")))
FAIL_GRACE_MS = int(os.getenv("FAIL_GRACE_MS", getattr(C, "FAIL_GRACE_MS", "25000")))
JITTER_PCT = float(os.getenv("HEALTH_JITTER_PCT", getattr(C, "HEALTH_JITTER_PCT", "0.1")))
BACKEND_PID_FILE = os.getenv("BACKEND_PID_FILE", getattr(C, "BACKEND_PID_FILE", "/var/log/wings/wings.txt"))

# —— 新增：仅 sglang 分支用，其他后端零影响 ——
SGLANG_FAIL_BUDGET = float(os.getenv("SGLANG_FAIL_BUDGET", getattr(C, "SGLANG_FAIL_BUDGET", "6.0")))
SGLANG_PID_GRACE_MS = int(os.getenv("SGLANG_PID_GRACE_MS", getattr(C, "SGLANG_PID_GRACE_MS", "30000")))
SGLANG_DECAY = float(os.getenv("SGLANG_DECAY", getattr(C, "SGLANG_DECAY", "0.5")))  # 成功时指数衰减系数
# “不可见 503”两道上限闸：
SGLANG_SILENCE_MAX_MS = int(os.getenv("SGLANG_SILENCE_MAX_MS", getattr(C, "SGLANG_SILENCE_MAX_MS", "60000")))
SGLANG_CONSEC_TIMEOUT_MAX = int(os.getenv("SGLANG_CONSEC_TIMEOUT_MAX", getattr(C, "SGLANG_CONSEC_TIMEOUT_MAX", "8")))
# K8s sidecar 模式：引擎作为独立容器运行时不写 PID 文件，跳过 pid_alive 检查
# 设置环境变量 WINGS_SKIP_PID_CHECK=true 可开启（默认 false 保持原有行为）
WINGS_SKIP_PID_CHECK = os.getenv("WINGS_SKIP_PID_CHECK", "false").strip().lower() in ("1", "true", "yes", "on")

# ───────────────────────── 内部工具函数 ─────────────────────────

def _now() -> float:
    """monotonic 秒，用于时长计算（不受系统时间回拨影响）。"""
    return time.monotonic()


def _read_pid_from_file() -> Optional[int]:
    """
    仅读取 BACKEND_PID_FILE 的**首行**作为 PID。
    返回 None 表示不可用（不存在/非数字/<=1）。
    """
    try:
        with open(BACKEND_PID_FILE, "r", encoding="utf-8") as f:
            first = f.readline()  # 只读第一行
        if not first:
            return None
        s = first.lstrip("\ufeff").split("#", 1)[0].strip()  # 去 BOM、去行内注释、去空白
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
    通过 /proc/<pid> 是否存在粗略判定进程是否存活。
    注：容器/命名空间环境中此方法仍然有效。
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
    与 _is_mindie 同一判定方式：约定 BACKEND_PID_FILE 第二行标识后端类型。
    当第二行为 'sglang'（大小写不敏感）时，认为当前后端为 sglang。
    若你倾向用环境变量，可在此追加 or os.getenv("WINGS_BACKEND","").lower()=="sglang"
    """
    try:
        with open(BACKEND_PID_FILE, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        return len(lines) >= 2 and lines[1].strip().lower() == "sglang"
    except Exception:
        return False


def _force_port(url: str, host: str, port: int) -> str:
    # 假设始终是 http://<ip>:<port>/health 这种结构
    scheme, rest = url.split("://", 1)          # "http", "<ip>:17000/health"
    hostport, path = rest.split("/", 1)         # "<ip>:17000", "health"
    return f"{scheme}://{host}:{port}/{path}"


async def _strict_probe_backend_health(client: httpx.AsyncClient) -> Tuple[bool, int, int, str]:
    """
    严格 200 判定后端 /health，并识别异常类型：
    - 成功：返回 (True, 200, latency_ms, "")
    - 失败：返回 (False, http_code_or_0, latency_ms, err_kind)
      * http_code 为后端实际返回码；0 表示无响应（连接失败/超时等）
      * err_kind ∈ {"connect_timeout","read_timeout","connect_error","request_error",""} 用于细分权重
    """
    url = build_backend_url("/health")
    if _is_mindie():
        url = _force_port(url, "127.0.0.2", 1026)  # mindie 时强制改端口为 1026

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
        ok = (code == 200)  # 严格 200 视为"后端就绪"
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
        # 如果没有异常发生，设置 err_kind 为空字符串
        err_kind = ""

    latency_ms = int((time.perf_counter() - t0) * 1000)
    return ok, code, latency_ms, err_kind


def _phase_from_code(code: int) -> str:
    """将对外 HTTP 状态码映射为简洁阶段标识（仅用于响应体字段 p）。"""
    return {
        200: "ready",
        201: "starting",
        502: "start_failed",
        503: "degraded",
    }.get(code, "unknown")


# ───────────────────────── sglang 专用：超时加权 / 保护 / 上限闸 ─────────────────────────

def _sglang_weight(http_code: int, err_kind: str) -> float:
    """
    为一次失败分配权重：
      - http_code==0（无响应）：按 err_kind 细分
          connect_error → 1.0
          connect_timeout → 0.75
          read_timeout → 0.25
          其它 request_error → 0.5
      - http_code==503 → 1.0
      - 其他 5xx → 0.5
      - 2xx → 0.0（不会被调用）
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
    PID 保护窗口（仅读超时 & 近期有成功）：
      - 仅当 pid_alive 且 backend_ok=False 且 http_code==0 且 err_kind=read_timeout
      - 且 now - last_success_ts ≤ SGLANG_PID_GRACE_MS
    """
    if not context.pid_alive or context.backend_ok:
        return False
    if not (context.http_code == 0 and context.err_kind == "read_timeout"):
        return False
    last_ok = h.get("last_success_ts")
    if last_ok is None:
        return False
    return (context.now - last_ok) * 1000.0 <= SGLANG_PID_GRACE_MS


# ───────────────────────── 状态生命周期与状态机推进 ─────────────────────────

def init_health_state() -> dict:
    """
    构造健康状态初始结构（字段语义需与网关 /health 响应保持一致）：
    - status：0=未启动/未就绪，1=就绪，-1=曾就绪后中断
    - ever_ready：曾经达到过就绪（用于 201/502 与 503 的路径判断）
    - consecutive_failures：连续失败计数（用于退化判断）
    - last_success_ts：最近一次成功观测时间
    - pid/pid_alive：来自 PID 文件与 /proc 的判定
    - backend_ok/backend_http_code/backend_http_latency_ms：严格 200 探测结果
    - last_observed_ts：最近一次完整观测完成时间（用于 /health 缓存窗口）
    - fail_score/accum_fail_ms：仅 sglang 使用的失败积分与失败时长积分
    - consecutive_timeouts：连续读超时计数（仅 sglang 用于“不可见 503”上限闸）
    - last_error_kind：最近一次错误类型（仅调试/日志）
    - warmup_executed：是否已经执行过warmup
    """
    return {
        "first_seen": _now(),
        "status": 0,                    # 0=未就绪/启动中, 1=就绪, -1=曾就绪后退化
        "ever_ready": False,
        "last_success_ts": None,
        "consecutive_failures": 0,

        "pid": None,
        "pid_alive": False,
        "backend_ok": False,
        "backend_http_code": 0,
        "backend_http_latency_ms": 0,
        "last_observed_ts": 0.0,

        # sglang 专用积分与计数
        "fail_score": 0.0,
        "accum_fail_ms": 0,
        "consecutive_timeouts": 0,
        "last_error_kind": "",
        
        # warmup标志
        "warmup_executed": False,
    }


@dataclass
class ProcessProbeResult:
    """进程探测结果"""
    pid: Optional[int]
    pid_alive: bool


@dataclass
class BackendHealthResult:
    """后端健康检查结果"""
    backend_ok: bool
    http_code: int
    latency_ms: int
    err_kind: str


@dataclass
class HealthObservationData:
    """健康观测数据"""
    process_result: ProcessProbeResult
    health_result: BackendHealthResult
    timestamp: float


@dataclass
class SglangFailureContext:
    """Sglang 失败处理上下文"""
    now: float
    pid_alive: bool
    backend_ok: bool
    http_code: int
    err_kind: str
    latency_ms: int


async def tick_observe_and_advance(h: dict, client: httpx.AsyncClient) -> None:
    """
    执行"一次观测 + 推进状态机"：
    1) 读取 PID 文件并判断存活
    2) 严格 200 访问后端 /health（区分异常类型）
    3) 刷新状态字段
    4) 推进状态机（就绪/失败累加/退化判定）
    5) （仅 sglang）基于权重/PID 保护/上限闸计算失败积分与读超时计数
    """
    # 1) 进程探测
    process_result = _probe_process()
    
    # 2) 后端健康检查
    backend_ok, http_code, latency_ms, err_kind = await _strict_probe_backend_health(client)
    health_result = BackendHealthResult(
        backend_ok=backend_ok,
        http_code=http_code,
        latency_ms=latency_ms,
        err_kind=err_kind
    )
    
    # 创建观测数据对象
    observation = HealthObservationData(
        process_result=process_result,
        health_result=health_result,
        timestamp=_now()
    )
    
    # 3) 刷新观测数据
    _refresh_observation_data(h, observation)
    
    # 4) 状态机推进
    _advance_state_machine(h, process_result.pid_alive, health_result.backend_ok)
    
    # 5) sglang 专用处理
    if _is_sglang():
        _handle_sglang_specifics(h, observation)


def _probe_process() -> ProcessProbeResult:
    """进程探测：读取 PID 文件并判断存活"""
    pid = _read_pid_from_file()
    pid_alive = _is_pid_alive(pid)
    return ProcessProbeResult(pid=pid, pid_alive=pid_alive)


def _refresh_observation_data(h: dict, observation: HealthObservationData) -> None:
    """刷新观测数据"""
    h["pid"] = observation.process_result.pid
    h["pid_alive"] = observation.process_result.pid_alive
    h["backend_ok"] = observation.health_result.backend_ok
    h["backend_http_code"] = observation.health_result.http_code
    h["backend_http_latency_ms"] = observation.health_result.latency_ms
    h["last_observed_ts"] = observation.timestamp
    h["last_error_kind"] = observation.health_result.err_kind


def _advance_state_machine(h: dict, pid_alive: bool, backend_ok: bool) -> None:
    """推进状态机（就绪/失败累加/退化判定）"""
    # K8s sidecar 模式下引擎跑在独立容器中，不会写 PID 文件，跳过 pid_alive 检查
    effective_pid_ok = True if WINGS_SKIP_PID_CHECK else pid_alive
    if effective_pid_ok and backend_ok:
        # 达到就绪：清零失败，标记曾就绪
        # 检查是否是第一次就绪，如果是则触发warmup
        first_time_ready = not h["ever_ready"]
        h["status"] = 1
        h["ever_ready"] = True
        h["consecutive_failures"] = 0
        h["last_success_ts"] = _now()
        
        # 如果是第一次就绪且还未执行过warmup，则触发warmup
        if first_time_ready and not h["warmup_executed"]:
            h["warmup_executed"] = True
            # 触发warmup调用
            asyncio.create_task(_trigger_warmup())
    else:
        # 未同时满足 → 当前视为一次失败
        if h["status"] == 1:
            h["consecutive_failures"] += 1
            # 原有退化阈值（保留，非 sglang 仍依赖）
            if (_should_degrade(h)):
                h["status"] = -1


def _should_degrade(h: dict) -> bool:
    """判断是否应该退化状态"""
    return (h["consecutive_failures"] >= FAIL_THRESHOLD and
            h["consecutive_failures"] * HEALTH_TIMEOUT_MS >= FAIL_GRACE_MS)


def _handle_sglang_specifics(h: dict, observation: HealthObservationData) -> None:
    """处理 sglang 专用逻辑：积分与读超时计数"""
    try:

        # 更新连续读超时计数
        _update_consecutive_timeouts(h, observation.health_result)
        
        if observation.process_result.pid_alive and observation.health_result.backend_ok:
            # 成功：指数衰减，避免长尾惩罚
            _handle_success_case(h)
        else:
            # 失败：若处于 PID 保护窗口且仅"读超时"，则不计分
            _handle_failure_case(h, observation)
                
    except Exception as e:
        C.logger.error("Error in sglang specifics handling: %s", str(e))
        # 根据业务需求决定是否重新抛出异常
        # raise  # 如果需要中断流程，可以取消注释


def _update_consecutive_timeouts(h: dict, health_result: BackendHealthResult) -> None:
    """更新连续读超时计数（只追踪 read_timeout）"""
    if not health_result.backend_ok and health_result.http_code == 0 and health_result.err_kind == "read_timeout":
        h["consecutive_timeouts"] = int(h.get("consecutive_timeouts", 0)) + 1
    else:
        h["consecutive_timeouts"] = 0 if health_result.backend_ok else h.get("consecutive_timeouts", 0)


def _handle_success_case(h: dict) -> None:
    """处理成功情况：指数衰减失败分数"""
    h["fail_score"] *= SGLANG_DECAY
    h["accum_fail_ms"] = int(h["accum_fail_ms"] * SGLANG_DECAY)


def _handle_failure_case(h: dict, observation: HealthObservationData) -> None:
    """处理失败情况：计算失败积分"""
    context = SglangFailureContext(
        now=_now(),
        pid_alive=observation.process_result.pid_alive,
        backend_ok=observation.health_result.backend_ok,
        http_code=observation.health_result.http_code,
        err_kind=observation.health_result.err_kind,
        latency_ms=observation.health_result.latency_ms
    )
    
    # 若处于 PID 保护窗口且仅"读超时"，则不计分
    if not _sglang_pid_grace(context, h):
        w = _sglang_weight(context.http_code, context.err_kind)
        if w > 0.0:
            h["fail_score"] += w
            h["accum_fail_ms"] += int(w * min(context.latency_ms, HEALTH_TIMEOUT_MS))


def map_http_code_from_state(h: dict) -> int:
    """
    基于状态机映射到 HTTP 状态码（对外规范）：
      200: 就绪（pid_alive && backend_ok && ever_ready 且 status==1）
           或（仅 sglang）主进程存活但后端探测失败且未触发任一闸门
      201: 冷启动期内未就绪（!ever_ready && elapsed < STARTUP_GRACE_MS）
      502: 冷启动超时仍未就绪（!ever_ready && elapsed >= STARTUP_GRACE_MS）
      503: 曾成功后当前不可用（达到退化阈值/预算阈值/上限闸）
      其它：未达退化阈值时为稳定性保守仍返回 200（但 body 会携带失败计数）
    """
    now = _now()
    elapsed_ms = int((now - h["first_seen"]) * 1000)

    # 确认就绪（原语义；K8s sidecar 模式下跳过 pid_alive 检查）
    effective_pid_ok = True if WINGS_SKIP_PID_CHECK else h["pid_alive"]
    is_ready = (
        effective_pid_ok and
        h["backend_ok"] and
        h["ever_ready"] and
        h["status"] == 1
    )
    if is_ready:
        return 200

    # 从未就绪过：按冷启动窗口划分 201/502（原语义）
    if not h["ever_ready"]:
        return 201 if elapsed_ms < STARTUP_GRACE_MS else 502

    # —— sglang 分支：不可见 503 的两道上限闸 + 预算闸门 ——
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

        # 任何一条闸门命中 → 503（不再被 PID 存活放行）
        if silence_hit or consec_to_hit or budget_hit:
            return 503

        # 未命中闸门：允许“PID 存活但 backend 未就绪”的短期放行
        if h["pid_alive"] and not h["backend_ok"]:
            return 200

        # 其它情况：保守返回 200（避免抖动），body 暴露细节
        return 200

    # —— 非 sglang：保持原逻辑 ——
    if (h["consecutive_failures"] >= FAIL_THRESHOLD) and \
       (h["consecutive_failures"] * HEALTH_TIMEOUT_MS >= FAIL_GRACE_MS):
        return 503
    return 200


# ───────────────────────── 轮询周期与后台循环 ─────────────────────────

def _jittered_sleep_base(h: dict) -> float:
    """
    计算下一轮观测的 sleep 秒数：
    - 未曾就绪：使用 PRE_READY_POLL_MS（默认 5000ms）
    - 曾就绪：使用 POLL_INTERVAL_MS（默认 2500ms）
    - 引入 ±JITTER_PCT 抖动，避免多实例并发对齐
    - 下限 100ms，保护事件循环
    - sglang 分支：根据 fail_score 自适应延长，以减少峰值期探针干扰
    """
    base_ms = PRE_READY_POLL_MS if not h["ever_ready"] else POLL_INTERVAL_MS

    if _is_sglang():
        fs = float(h.get("fail_score", 0.0))
        if fs >= max(0.0, SGLANG_FAIL_BUDGET - 1.0):
            base_ms = max(base_ms, 9000)   # 接近退化阈值/已退化：~9-10s
        elif fs >= 2.0:
            base_ms = max(base_ms, 5000)   # 中等失败密度：~5s

    r = 1.0 + random.uniform(-JITTER_PCT, JITTER_PCT)
    return max(100.0, base_ms * r) / 1000.0


async def health_monitor_loop(app) -> None:
    """
    后台健康循环：按阶段节奏进行“观测+推进”，直至被取消。
    - 任何观测异常仅记录 warning，不影响代理主循环。
    - 收到取消信号（shutdown）时优雅退出，不向上抛出非期望错误。
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


# ───────────────────────── 主文件调用的 API ─────────────────────────

def setup_health_monitor(app) -> None:
    """
    在 FastAPI startup 中调用：
    - 初始化健康状态字典
    - 创建后台健康循环任务（异步、非阻塞启动路径）
    """
    app.state.health = init_health_state()
    app.state.health_task = asyncio.create_task(health_monitor_loop(app), name="wings-health-monitor")
    C.logger.info("Health monitor loop enabled")


async def teardown_health_monitor(app) -> None:
    """
    在 FastAPI shutdown 中调用：
    - 取消后台任务并等待其结束
    - 关键：抑制 asyncio.CancelledError，避免生命周期管理报错
    """
    try:
        task = getattr(app.state, "health_task", None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            app.state.health_task = None
    except Exception as e:
        # 记录异常信息，但不中断关闭流程：
        C.logger.warning("Health monitor teardown encountered an error: %s", str(e))


def build_health_body(h: dict, code: int) -> dict:
    """
    构造 /health 返回体（精简且实用）：
    - s：内部三态（0/1/-1）
    - p：阶段字符串（ready/starting/start_failed/degraded）
    - pid_alive：主进程是否存活
    - backend_ok/backend_code：后端 /health 严格 200 结果与状态码
    - interrupted：曾就绪后当前不可用（ever_ready && status==-1）
    - ever_ready：是否曾达就绪
    - cf：连续失败次数
    - lat_ms：后端 /health 探测耗时
    注：保持对外结构不变；sglang 的 fail_score/accum_fail_ms 等为内部使用，不在返回体暴露。
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
    为 /health 补充少量响应头：
    - X-Wings-Status：内部三态值
    - Cache-Control：no-store，避免中间层缓存健康检查响应
    """
    return {
        "X-Wings-Status": str(h["status"]),
        "Cache-Control": "no-store",
    }


async def _trigger_warmup() -> None:
    """
    触发一次chat_completions调用进行warmup
    使用RAG场景中的warmup逻辑
    """
    if not C.RAG_ACC_ENABLED:
        return
    
    try:
        await _send_warmup_request()
    except Exception as e:
        # 记录错误但不中断健康检查流程
        C.logger.warning("Warmup request failed: %s", str(e))


async def _send_warmup_request() -> None:
    """发送warmup请求到本地代理"""
    # 获取模型名称，如果没有设置则使用默认值
    model_name = os.getenv("MODEL_NAME", "default-model")
    proxy_port = os.getenv("PROXY_PORT", "18080")
    
    # 创建HTTP客户端
    async with httpx.AsyncClient() as client:
        # 构造warmup请求数据
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
        
        # 发送warmup请求
        C.logger.info("Sending warmup request to %s with model: %s", url, model_name)
        response = await client.post(
            url,
            json=warmup_data,
            headers=headers,
            timeout=300
        )
        
        # 记录响应状态
        C.logger.info("Warmup request completed with status: %d", response.status_code)
        
        # 如果是流式响应，读取一些数据然后关闭
        if response.status_code == 200:
            # 读取少量数据确保连接建立
            async for chunk in response.aiter_bytes(chunk_size=1024):
                break  # 只读取第一个chunk然后退出
            await response.aclose()
