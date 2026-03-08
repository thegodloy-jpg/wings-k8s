# =============================================================================
# 文件: proxy/speaker_logging.py
# 用途: 结构化日志辅助，用于代理请求生命周期、诊断和追踪上下文
# 状态: 活跃，复用自 wings 项目的可观测性辅助模块
#
# 功能概述:
#   本模块实现 multi-worker 场景下的日志级别控制:
#   - 用于控制哪些 worker 输出 INFO 级别日志
#   - 避免多 worker 日志重复，减少日志量
#   - 支持关闭 uvicorn.access 访问日志
#   - 支持指定 speaker worker 索引
#
# 配置环境变量:
#   - LOG_INFO_SPEAKERS:    INFO 级别日志的 speaker worker 数量 (默认 1)
#   - LOG_WORKER_COUNT:     总 worker 数，默认从 --workers 或环境变量推断
#   - KEEP_ACCESS_LOG:      是否保留 uvicorn.access 日志
#   - LOG_SPEAKER_INDEXES:  明确指定的 speaker worker 索引 (如 "0,2")
#   - WORKER_INDEX:         当前 worker 索引
#
# 工作原理:
#   如果指定了 LOG_SPEAKER_INDEXES 和 WORKER_INDEX，直接匹配；
#   否则用 pid hash % LOG_WORKER_COUNT 决定是否为 speaker。
#
# Sidecar 架构契约:
#   - 日志 schema 保持稳定，供下游解析
#   - 避免在热路径做昂贵的格式化
#
# =============================================================================
# -*- coding: utf-8 -*-
"""
multi-worker 日志级别控制。

仅让部分 worker 输出 INFO 级别日志，避免多 worker 场景下日志重复。

使用方法:
1) 在 FastAPI/uvicorn 启动时调用 configure_worker_logging()
2) 日志级别会自动根据 worker 索引和配置决定

配置环境变量:
- LOG_INFO_SPEAKERS       : INFO 级别日志的 speaker 数量 (默认 1)
- LOG_WORKER_COUNT        : 总 worker 数，如未设置则读取 --workers 或 WEB_CONCURRENCY/UVICORN_WORKERS
- KEEP_ACCESS_LOG         : 是否保留 uvicorn.access，0/false 关闭
- LOG_SPEAKER_INDEXES     : 明确指定哪些 worker 为 speaker，如 "0,2"，需配合 WORKER_INDEX 使用
- WORKER_INDEX            : 当前 worker 的索引，通常由 uvicorn 设置

若不指定 LOG_SPEAKER_INDEXES 或 WORKER_INDEX，则用 pid-hash % LOG_WORKER_COUNT 决定。
"""
import logging
import os
import sys
import zlib
from typing import List, Optional

# ==== NEW:  ====
import re  #  /health
# ==== NEW END ====


# =========================
#
# =========================
class LogConstants:
    """(no description)"""

    #
    ENV_PREFIX = "LOG_"

    #  _SPEAKER_DECISION
    SPEAKER_DECISION_ENV = "_SPEAKER_DECISION"

    #  worker  8
    DEFAULT_WORKER_COUNT = 8

    #  logger
    NORMALIZE_LOGGERS = [
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "uvicorn.server",
        "uvicorn.lifespan",
        "httpx",
        "httpcore",
    ]


#
DEFAULT_WORKER_COUNT = LogConstants.DEFAULT_WORKER_COUNT
NORMALIZE_LOGGERS = LogConstants.NORMALIZE_LOGGERS
SPEAKER_DECISION_ENV = LogConstants.SPEAKER_DECISION_ENV


#
_CONFIGURED_ONCE = False

#  logger
_lg = logging.getLogger(__name__)


def _env_bool(key: str, default: bool = False) -> bool:
    """ default"""
    v = os.getenv(key, "")
    if v is None:
        return default
    v = v.strip().lower()
    if v in ("1", "true", "yes", "y", "on"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    return default


def _env_int(key: str, default: int) -> int:
    """ default"""
    try:
        return int(os.getenv(key, str(default)).strip())
    except Exception:
        return default


def _parse_csv_ints(s: str) -> List[int]:
    """ '0,2,5' -> [0, 2, 5]"""
    out: List[int] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            _lg.warning("ignore non-int csv item: %r", part)
    return out


def _discover_worker_count() -> int:
    """
    1) LOG_WORKER_COUNT --workers
    2) WEB_CONCURRENCY / UVICORN_WORKERS
    3)  0
    """
    # 1)
    n = _env_int("LOG_WORKER_COUNT", 0)
    if n > 0:
        return n

    # 2)
    for k in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        v = os.getenv(k, "").strip()
        if not v:
            continue
        if not v.isdecimal():  #  try/except
            _lg.debug("Env %s=%r is not a positive integer string", k, v)
            continue
        n = int(v)
        if n > 0:
            return n
        _lg.debug("Env %s=%r parsed <= 0, ignored", k, v)

    # 3)
    return 0


def _is_speaker_by_index(allowed_indexes: List[int], worker_index: Optional[int]) -> Optional[bool]:
    """
    LOG_SPEAKER_INDEXES WORKER_INDEX
     None
    """
    if worker_index is None:
        return None
    return worker_index in allowed_indexes


def _is_speaker_by_pid_hash(pid: int, speakers_quota: int, worker_count: int) -> bool:
    """
     pid  crc32 worker_count
     [0, speakers_quota)


    - speakers_quota worker  INFO 1
    - worker_count worker <=0 max(8, speakers_quota)
    -  worker_count PID /
    """
    speakers_quota = max(1, speakers_quota)
    if worker_count <= 0:
        # worker  8  worker
        worker_count = max(8, speakers_quota)
    h = (zlib.crc32(str(pid).encode("utf-8")) & 0xFFFFFFFF)
    return (h % worker_count) < speakers_quota


def _ensure_root_handler():
    """
     root logger  StreamHandlerstderr
     logger
    """
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler(stream=sys.stderr)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s[pid=%(process)d] %(message)s")
        h.setFormatter(fmt)
        root.addHandler(h)


def _quiet_uvicorn_access(keep: bool):
    """
     uvicorn.access
    - keep=False handler HTTP
    - keep=True
    """
    lg = logging.getLogger("uvicorn.access")
    if not keep:
        lg.disabled = True
        lg.propagate = False
        try:
            lg.handlers.clear()
        except Exception:
            lg.handlers[:] = []
    else:
        lg.disabled = False


def _normalize_children():
    """
     logger
    -  logger  NOTSET root
    -  logger  propagate  True root
     worker  INFO root
    """
    for name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "uvicorn.server",
        "uvicorn.lifespan",
        "httpx",
        "httpcore",
    ):
        lg = logging.getLogger(name)
        lg.setLevel(logging.NOTSET)   # logger
        lg.propagate = True           #  root


# ==== NEW: /health  ===================================
class _DropByRegex(logging.Filter):
    """
    Filter record.getMessage()

    -  message uvicorn  AccessLogger
    -
        * uvicorn.access   "GET /health HTTP/1.1"
        * httpx / httpcore   /health
    """
    def __init__(self, patterns: List[re.Pattern]):
        super().__init__()
        self._patterns = patterns

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for p in self._patterns:
            if p.search(msg):
                return False
        return True


def _install_health_log_filters() -> None:
    """
     /health
    - DROP_HEALTH_ACCESS=1 uvicorn.access  "GET /health"
      * HEALTH_ACCESS_DROP_REGEX \"GET\\s+/health\\b
    - DROP_OUTBOUND_HEALTH=1 httpx/httpcore  '/health'
      * OUTBOUND_HEALTH_DROP_REGEX /health
    """
    # 1)  inbound  /health
    if _env_bool("DROP_HEALTH_ACCESS", True):
        pat = os.getenv("HEALTH_ACCESS_DROP_REGEX", r"\"GET\s+/health\b")
        try:
            patterns = [re.compile(pat)]
            access_logger = logging.getLogger("uvicorn.access")
            access_logger.addFilter(_DropByRegex(patterns))
            _lg.debug("Installed uvicorn.access /health drop filter: %r", pat)
        except re.error as e:
            _lg.warning("Invalid HEALTH_ACCESS_DROP_REGEX=%r, skip filter. err=%s", pat, e)

    # 2)  outbound  /health httpx/httpcore  DEBUG
    if _env_bool("DROP_OUTBOUND_HEALTH", True):
        pat2 = os.getenv("OUTBOUND_HEALTH_DROP_REGEX", r"/health")
        try:
            patterns2 = [re.compile(pat2)]
            for name in ("httpx", "httpcore"):
                lg = logging.getLogger(name)
                lg.addFilter(_DropByRegex(patterns2))
            _lg.debug("Installed httpx/httpcore /health drop filter: %r", pat2)
        except re.error as e:
            _lg.warning("Invalid OUTBOUND_HEALTH_DROP_REGEX=%r, skip filter. err=%s", pat2, e)
# ==== NEW END =================================================================


def configure_worker_logging(force: bool = False) -> bool:
    """
     worker  worker  speakerINFO
     FastAPI startup  force=False


    1)
    2) worker  access
    3)  PID
    4)  root handler uvicorn.access logger
    5)  root =INFO=WARNING
    6)  INFO
    7)  _SPEAKER_DECISION
    """
    global _CONFIGURED_ONCE
    if _CONFIGURED_ONCE and not force:
        #
        return bool(int(os.getenv("_SPEAKER_DECISION", "0")))

    #  worker  INFO 1
    speakers_quota = max(1, _env_int("LOG_INFO_SPEAKERS", 1))
    # / worker
    worker_count = _discover_worker_count()
    #  uvicorn.accessHTTP
    keep_access = _env_bool("KEEP_ACCESS_LOG", False)

    #  +  WORKER_INDEX
    allowed_indexes_env = os.getenv("LOG_SPEAKER_INDEXES", "").strip()
    allowed_indexes = _parse_csv_ints(allowed_indexes_env) if allowed_indexes_env else []
    worker_index_env = os.getenv("WORKER_INDEX")
    worker_index = None
    if worker_index_env and worker_index_env.strip().lstrip("-").isdigit():
        worker_index = int(worker_index_env)

    pid = os.getpid()

    #
    # 1)  WORKER_INDEX
    # 2)  PID  worker_count
    decision_by_index = None
    if allowed_indexes:
        decision_by_index = _is_speaker_by_index(allowed_indexes, worker_index)
    if decision_by_index is None:
        is_speaker = _is_speaker_by_pid_hash(pid, speakers_quota, worker_count)
    else:
        is_speaker = bool(decision_by_index)

    #  root  handler
    _ensure_root_handler()

    # / uvicorn.access
    _quiet_uvicorn_access(keep_access)

    #  logger root  handler
    _normalize_children()

    # ==== NEW:  /health wings  ====
    _install_health_log_filters()
    # ==== NEW END ====

    # root
    root = logging.getLogger()
    root.setLevel(logging.INFO if is_speaker else logging.WARNING)

    #  INFO
    if is_speaker:
        logging.getLogger("log-center").info(
            "worker(pid=%s) is SPEAKER=1  (quota=%s, workers=%s, idx=%s, allowed=%s)",
            pid, speakers_quota, worker_count, worker_index, allowed_indexes or None
        )

    #
    os.environ["_SPEAKER_DECISION"] = "1" if is_speaker else "0"
    _CONFIGURED_ONCE = True
    return is_speaker


# =========================
#
# =========================
def _patch_worker_logging(constants: type = LogConstants) -> None:
    """

    -  LogConstants.NORMALIZE_LOGGERS  _normalize_children
    -  LogConstants.DEFAULT_WORKER_COUNT  _is_speaker_by_pid_hash  8
    -  configure_worker_logging  SPEAKER_DECISION_ENV
    """
    mod = sys.modules[__name__]

    # 1)  _normalize_children
    def _normalize_children_patched():
        for name in list(constants.NORMALIZE_LOGGERS):
            lg = logging.getLogger(name)
            lg.setLevel(logging.NOTSET)
            lg.propagate = True

    setattr(mod, "_normalize_children", _normalize_children_patched)

    # 2)  _is_speaker_by_pid_hash
    _orig_pid_hash = getattr(mod, "_is_speaker_by_pid_hash")

    def _is_speaker_by_pid_hash_patched(pid: int, speakers_quota: int, worker_count: int) -> bool:
        speakers_quota_local = max(1, speakers_quota)
        if worker_count <= 0:
            worker_count_local = max(constants.DEFAULT_WORKER_COUNT, speakers_quota_local)
        else:
            worker_count_local = worker_count
        h = (zlib.crc32(str(pid).encode("utf-8")) & 0xFFFFFFFF)
        return (h % worker_count_local) < speakers_quota_local

    setattr(mod, "_is_speaker_by_pid_hash", _is_speaker_by_pid_hash_patched)

    # 3)  configure_worker_logging
    _orig_configure = getattr(mod, "configure_worker_logging")

    def configure_worker_logging_wrapped(*args, **kwargs):
        alias_key = getattr(constants, "SPEAKER_DECISION_ENV", "_SPEAKER_DECISION")
        default_key = "_SPEAKER_DECISION"

        #
        if alias_key != default_key and alias_key in os.environ and default_key not in os.environ:
            os.environ[default_key] = os.environ[alias_key]

        try:
            result = _orig_configure(*args, **kwargs)
        finally:
            #
            if alias_key != default_key and default_key in os.environ:
                os.environ[alias_key] = os.environ[default_key]

        return result

    setattr(mod, "configure_worker_logging", configure_worker_logging_wrapped)


#  LOG_PATCH_DISABLE=1
if os.getenv("LOG_PATCH_DISABLE", "").strip().lower() not in ("1", "true", "yes", "y", "on"):
    _patch_worker_logging(LogConstants)
