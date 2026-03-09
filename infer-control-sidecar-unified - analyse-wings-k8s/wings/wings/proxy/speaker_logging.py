# -*- coding: utf-8 -*-
"""
方案A：最简单的“只打印部分 worker 的 INFO 日志”

用法：
1) 在 FastAPI/uvicorn 入口文件导入并调用 configure_worker_logging()（见文末示例）
2) 通过环境变量控制发言者数量/策略

环境变量：
- LOG_INFO_SPEAKERS      发言者数量（默认 1）
- LOG_WORKER_COUNT       worker 总数（强烈建议设置为 --workers 的值；默认自动从 WEB_CONCURRENCY/UVICORN_WORKERS 推测）
- KEEP_ACCESS_LOG        是否保留 uvicorn.access（默认 0/false：关闭）
- LOG_SPEAKER_INDEXES    （可选）逗号分隔的 worker 索引白名单，如 "0,2"；需要你在外部为每个进程设置 WORKER_INDEX
- WORKER_INDEX           （可选）当前 worker 的索引（由你的进程管理器/脚本注入，uvicorn 默认不会提供）

策略优先级：
若同时提供 LOG_SPEAKER_INDEXES 与 WORKER_INDEX，则按“索引白名单”决定是否发言；
否则按 pid-hash 与 LOG_WORKER_COUNT 做确定性选择。
"""
import logging
import os
import sys
import zlib
from typing import List, Optional

# ==== NEW: 需要的标准库 ====
import re  # 正则过滤 /health 访问日志
# ==== NEW END ====


# =========================
# 常量类与常量 —— 置于文件顶部
# =========================
class LogConstants:
    """集中管理本模块需要用到的常量（不改动下方原实现，通过运行时补丁替换魔法值）"""

    # 环境变量前缀（当前原实现未直接使用，预留扩展）
    ENV_PREFIX = "LOG_"

    # “是否发言者”缓存环境变量名（与原实现兼容，默认仍是 _SPEAKER_DECISION）
    SPEAKER_DECISION_ENV = "_SPEAKER_DECISION"

    # 当无法获知 worker 总数时的回落默认值（替代源码中的 8）
    DEFAULT_WORKER_COUNT = 8

    # 需要统一归一化的子 logger 列表（替代源码中写死的名称集合）
    NORMALIZE_LOGGERS = [
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "uvicorn.server",
        "uvicorn.lifespan",
        "httpx",
        "httpcore",
    ]


# （可选）提供便捷的模块级别常量别名
DEFAULT_WORKER_COUNT = LogConstants.DEFAULT_WORKER_COUNT
NORMALIZE_LOGGERS = LogConstants.NORMALIZE_LOGGERS
SPEAKER_DECISION_ENV = LogConstants.SPEAKER_DECISION_ENV


# 全局标志：避免重复配置日志（例如在应用多次调用时）
_CONFIGURED_ONCE = False

# 模块 logger（用于本模块内部提示）
_lg = logging.getLogger(__name__)


def _env_bool(key: str, default: bool = False) -> bool:
    """从环境变量中读取布尔值，支持多种常见写法；失败或缺省返回 default。"""
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
    """从环境变量中读取整数；解析失败返回 default。"""
    try:
        return int(os.getenv(key, str(default)).strip())
    except Exception:
        return default


def _parse_csv_ints(s: str) -> List[int]:
    """解析以逗号分隔的整型列表字符串，如 '0,2,5' -> [0, 2, 5]；非法项忽略。"""
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
    1) LOG_WORKER_COUNT（建议与 --workers 一致）
    2) 回落：WEB_CONCURRENCY / UVICORN_WORKERS
    3) 都拿不到返回 0
    """
    # 1) 优先显式配置
    n = _env_int("LOG_WORKER_COUNT", 0)
    if n > 0:
        return n

    # 2) 常见变量回落（仅接受正整数）
    for k in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        v = os.getenv(k, "").strip()
        if not v:
            continue
        if not v.isdecimal():  # 避免 try/except；只接受十进制数字
            _lg.debug("Env %s=%r is not a positive integer string", k, v)
            continue
        n = int(v)
        if n > 0:
            return n
        _lg.debug("Env %s=%r parsed <= 0, ignored", k, v)

    # 3) 未知
    return 0


def _is_speaker_by_index(allowed_indexes: List[int], worker_index: Optional[int]) -> Optional[bool]:
    """
    若提供了白名单索引（LOG_SPEAKER_INDEXES）且当前进程也提供了 WORKER_INDEX，
    则按白名单判定是否为“发言者”。否则返回 None（表示无法按索引法判定）。
    """
    if worker_index is None:
        return None
    return worker_index in allowed_indexes


def _is_speaker_by_pid_hash(pid: int, speakers_quota: int, worker_count: int) -> bool:
    """
    确定性选择：对 pid 做 crc32，然后对 worker_count 取模，
    落在 [0, speakers_quota) 即为发言者。

    说明：
    - speakers_quota：允许多少个 worker 打 INFO（至少为 1）
    - worker_count：总 worker 数；若未知（<=0），保守地用 max(8, speakers_quota)
    - 该方法能保证在相同 worker_count 下，PID 的选择是稳定/均匀的
    """
    speakers_quota = max(1, speakers_quota)
    if worker_count <= 0:
        # worker 数未知时的保守回落：假设最多 8 个 worker，按配额近似选择
        worker_count = max(8, speakers_quota)
    h = (zlib.crc32(str(pid).encode("utf-8")) & 0xFFFFFFFF)
    return (h % worker_count) < speakers_quota


def _ensure_root_handler():
    """
    确保 root logger 至少有一个 StreamHandler（stderr），并设置统一格式。
    避免某些环境（如仅靠子 logger）导致根本没有输出。
    """
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler(stream=sys.stderr)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s[pid=%(process)d] %(message)s")
        h.setFormatter(fmt)
        root.addHandler(h)


def _quiet_uvicorn_access(keep: bool):
    """
    控制 uvicorn.access 的日志是否保留：
    - keep=False：禁用并清空 handler（减少 HTTP 访问日志噪声）
    - keep=True ：保留默认行为
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
    归一化常见子 logger 配置：
    - 统一把子 logger 的级别设为 NOTSET，让其继承 root 的级别
    - 统一把子 logger 的 propagate 设为 True，让其把日志交给 root 处理
    这样才能保证“只让部分 worker 打 INFO”的策略按 root 级别生效。
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
        lg.setLevel(logging.NOTSET)   # 不在子logger截断级别
        lg.propagate = True           # 交给 root 统一控制


# ==== NEW: /health 访问日志过滤（精确、可开关） ===================================
class _DropByRegex(logging.Filter):
    """
    通用“按正则丢弃”Filter：只要 record.getMessage() 命中任一正则，就不记录该条日志。
    说明：
    - 仅依赖格式化后的 message，简单可靠；不侵入 uvicorn 的 AccessLogger 实现
    - 默认用于：
        * uvicorn.access —— 丢弃 "GET /health HTTP/1.1" 的访问日志
        * httpx / httpcore —— 尽量丢弃包含 /health 的调试日志（通常不开启）
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
    安装与 /health 相关的丢弃过滤器（按环境变量控制）：
    - DROP_HEALTH_ACCESS=1（默认开）：对 uvicorn.access 过滤 "GET /health" 的行
      * HEALTH_ACCESS_DROP_REGEX 可自定义，默认：\"GET\\s+/health\\b
    - DROP_OUTBOUND_HEALTH=1（默认开）：对 httpx/httpcore 过滤任意包含 '/health' 的行
      * OUTBOUND_HEALTH_DROP_REGEX 可自定义，默认：/health
    """
    # 1) 过滤 inbound 的 /health 访问日志（仅代理自身）
    if _env_bool("DROP_HEALTH_ACCESS", True):
        pat = os.getenv("HEALTH_ACCESS_DROP_REGEX", r"\"GET\s+/health\b")
        try:
            patterns = [re.compile(pat)]
            access_logger = logging.getLogger("uvicorn.access")
            access_logger.addFilter(_DropByRegex(patterns))
            _lg.debug("Installed uvicorn.access /health drop filter: %r", pat)
        except re.error as e:
            _lg.warning("Invalid HEALTH_ACCESS_DROP_REGEX=%r, skip filter. err=%s", pat, e)

    # 2) 过滤 outbound 的 /health 日志（httpx/httpcore 的 DEBUG 级别输出）
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
    在“每个 worker 进程”里调用。返回值：当前 worker 是否被选为 speaker（INFO 发言者）。
    可重复调用（例如在 FastAPI startup 里再调用），若 force=False 会自动避免重复配置。

    逻辑步骤：
    1) 如已配置且非强制，不重复做（直接返回上次决策）
    2) 读取环境变量（配额、worker 数、是否保留 access、索引白名单等）
    3) 决策“是否发言者”：优先索引白名单；否则用 PID 哈希算法
    4) 安装 root handler、关闭或保留 uvicorn.access、归一化子 logger
    5) 设置 root 级别（发言者=INFO，非发言者=WARNING）
    6) 发言者打印一条自报家门的 INFO
    7) 记录决策到环境变量 _SPEAKER_DECISION，便于重复调用快速返回
    """
    global _CONFIGURED_ONCE
    if _CONFIGURED_ONCE and not force:
        # 已经配置过，直接返回之前的判定（从环境变量读取缓存）
        return bool(int(os.getenv("_SPEAKER_DECISION", "0")))

    # 发言者配额（允许多少个 worker 打 INFO），至少为 1
    speakers_quota = max(1, _env_int("LOG_INFO_SPEAKERS", 1))
    # 推测/指定 worker 总数
    worker_count = _discover_worker_count()
    # 是否保留 uvicorn.access（HTTP 访问日志）
    keep_access = _env_bool("KEEP_ACCESS_LOG", False)

    # 可选：索引白名单 + 外部提供的 WORKER_INDEX（由你的进程管理器注入）
    allowed_indexes_env = os.getenv("LOG_SPEAKER_INDEXES", "").strip()
    allowed_indexes = _parse_csv_ints(allowed_indexes_env) if allowed_indexes_env else []
    worker_index_env = os.getenv("WORKER_INDEX")
    worker_index = None
    if worker_index_env and worker_index_env.strip().lstrip("-").isdigit():
        worker_index = int(worker_index_env)

    pid = os.getpid()

    # 选择策略：
    # 1) 若提供了白名单且当前进程有 WORKER_INDEX，则按白名单判定；
    # 2) 否则退回到 PID 哈希法（对 worker_count 做取模）
    decision_by_index = None
    if allowed_indexes:
        decision_by_index = _is_speaker_by_index(allowed_indexes, worker_index)
    if decision_by_index is None:
        is_speaker = _is_speaker_by_pid_hash(pid, speakers_quota, worker_count)
    else:
        is_speaker = bool(decision_by_index)

    # 确保 root 至少有一个 handler，并设置统一输出格式
    _ensure_root_handler()

    # 关闭/保留 uvicorn.access
    _quiet_uvicorn_access(keep_access)

    # 归一化子 logger：统一交给 root 控制级别和 handler
    _normalize_children()

    # ==== NEW: 安装 /health 日志过滤（仅在本进程内生效；wings 需自行调用同函数） ====
    _install_health_log_filters()
    # ==== NEW END ====

    # 关键：root 级别根据“是否发言者”决定
    root = logging.getLogger()
    root.setLevel(logging.INFO if is_speaker else logging.WARNING)

    # 可选：让发言者打一个自报家门的 INFO，非发言者静默（不额外污染）
    if is_speaker:
        logging.getLogger("log-center").info(
            "worker(pid=%s) is SPEAKER=1  (quota=%s, workers=%s, idx=%s, allowed=%s)",
            pid, speakers_quota, worker_count, worker_index, allowed_indexes or None
        )

    # 标记与缓存决策（供重复调用时快速返回）
    os.environ["_SPEAKER_DECISION"] = "1" if is_speaker else "0"
    _CONFIGURED_ONCE = True
    return is_speaker


# =========================
# 运行时补丁：让上面的“已存在实现”无侵入地使用顶部常量
# =========================
def _patch_worker_logging(constants: type = LogConstants) -> None:
    """
    运行时补丁：
    - 用 LogConstants.NORMALIZE_LOGGERS 替换 _normalize_children 的硬编码列表
    - 用 LogConstants.DEFAULT_WORKER_COUNT 替换 _is_speaker_by_pid_hash 中的 8
    - 为 configure_worker_logging 增加对 SPEAKER_DECISION_ENV 的别名同步
    """
    mod = sys.modules[__name__]

    # 1) 替换 _normalize_children
    def _normalize_children_patched():
        for name in list(constants.NORMALIZE_LOGGERS):
            lg = logging.getLogger(name)
            lg.setLevel(logging.NOTSET)
            lg.propagate = True

    setattr(mod, "_normalize_children", _normalize_children_patched)

    # 2) 替换 _is_speaker_by_pid_hash（仅替换回落常量）
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

    # 3) 包装 configure_worker_logging：增加别名环境变量同步
    _orig_configure = getattr(mod, "configure_worker_logging")

    def configure_worker_logging_wrapped(*args, **kwargs):
        alias_key = getattr(constants, "SPEAKER_DECISION_ENV", "_SPEAKER_DECISION")
        default_key = "_SPEAKER_DECISION"

        # 调用前：若别名有值而默认键无值，则同步到默认键（兼容原实现读取默认键）
        if alias_key != default_key and alias_key in os.environ and default_key not in os.environ:
            os.environ[default_key] = os.environ[alias_key]

        try:
            result = _orig_configure(*args, **kwargs)
        finally:
            # 调用后：把默认键写回别名（便于外部仅依赖别名）
            if alias_key != default_key and default_key in os.environ:
                os.environ[alias_key] = os.environ[default_key]

        return result

    setattr(mod, "configure_worker_logging", configure_worker_logging_wrapped)


# 默认自动打补丁；如需关闭可设置环境变量 LOG_PATCH_DISABLE=1
if os.getenv("LOG_PATCH_DISABLE", "").strip().lower() not in ("1", "true", "yes", "y", "on"):
    _patch_worker_logging(LogConstants)
