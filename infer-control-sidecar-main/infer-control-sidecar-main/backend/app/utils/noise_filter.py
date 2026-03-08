# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: utils/noise_filter.py
# Purpose: Log/output noise filtering helpers to improve runtime signal clarity.
# Status: Active reused utility.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Avoid filtering meaningful error diagnostics.
# - Keep filtering rules transparent and maintainable.
# -----------------------------------------------------------------------------
# -*- coding: utf-8 -*-
"""
通用"噪声日志过滤"工具（引擎无关）
------------------------------------------------
目标：
- 丢弃：uvicorn 等 access 日志中的 "GET /health ..."
- 丢弃：批次噪声行，例如 "Prefill batch." / "Decode batch."
- 丢弃：pynvml 相关的 FutureWarning（torch.cuda 导入时）
- 兜底：对直接写 stdout/stderr 的文本做行级过滤（print 也能拦）

可通过环境变量控制（都可选）：
- NOISE_FILTER_DISABLE=1           关闭所有过滤
- HEALTH_FILTER_ENABLE=0/1         默认 1
- HEALTH_PATH_REGEX=...            默认 "\"GET\\s+/health\\b"
- BATCH_NOISE_FILTER_ENABLE=0/1    默认 1
- BATCH_NOISE_REGEX=...            默认 "(?i)\\b(?:prefill|decode)\\b[^\\n]{0,200}\\bbatch\\b"
- PYNVML_FILTER_ENABLE=0/1         默认 1
- PYNVML_NOISE_REGEX=...           默认 "(?i)(pynvml package is deprecated|\\bimport pynvml\\b)"
- STDIO_FILTER_ENABLE=0/1          默认 1
"""
from __future__ import annotations
import logging
import os
import re
import sys
import warnings
from typing import Optional, Iterable


# ----------------- 环境读取 -----------------
def _env_bool(k: str, default: bool) -> bool:
    v = os.getenv(k, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")

if _env_bool("NOISE_FILTER_DISABLE", False):
    # 用户显式关闭
    def install_noise_filters(*_args, **_kwargs) -> None:
        return
else:
    _HEALTH_ON = _env_bool("HEALTH_FILTER_ENABLE", True)
    _BATCH_ON = _env_bool("BATCH_NOISE_FILTER_ENABLE", True)
    _PYNVML_ON = _env_bool("PYNVML_FILTER_ENABLE", True)
    _STDIO_ON = _env_bool("STDIO_FILTER_ENABLE", True)

    _HEALTH_RE = os.getenv("HEALTH_PATH_REGEX", r'"GET\s+/health\b')
    _BATCH_RE = os.getenv("BATCH_NOISE_REGEX", 
                         r'(?i)\b(?:prefill|decode)\b[^\n]{0,200}\bbatch\b')
    _PYNVML_RE = os.getenv("PYNVML_NOISE_REGEX", 
                          r'(?i)(pynvml package is deprecated|\bimport pynvml\b)')

    _HEALTH_PAT = re.compile(_HEALTH_RE)
    _BATCH_PAT = re.compile(_BATCH_RE)
    _PYNVML_PAT = re.compile(_PYNVML_RE)


    class _DropByRegex(logging.Filter):
        """满足任一正则时丢弃该条日志记录。"""
        __slots__ = ("pats",)

        
        def __init__(self, patterns: Iterable[re.Pattern]):
            super().__init__()
            self.pats = tuple(patterns)


        def filter(self, record: logging.LogRecord) -> int:
            try:
                msg = record.getMessage()
            except Exception:
                msg = record.msg if isinstance(record.msg, str) else str(record.msg)
            for p in self.pats:
                if p.search(msg):
                    return 0  # 丢弃
            return 1


    class _LineFilterIO:
        """
        包装一个"类似文件"的对象（stdout/stderr），
        做行级缓冲；整行匹配到噪声正则则丢弃。
        """
        __slots__ = ("_under", "_buf", "_pats", "_closed", "name")


        def __init__(self, under, patterns: Iterable[re.Pattern], name: str):
            self._under = under
            self._buf = ""
            self._pats = tuple(patterns)
            self._closed = False
            self.name = getattr(under, "name", name)


        def __getattr__(self, item):
            # 其它属性透传
            return getattr(self._under, item)


        def fileno(self):
            return self._under.fileno() if hasattr(self._under, "fileno") else -1


        def isatty(self):
            return self._under.isatty() if hasattr(self._under, "isatty") else False


        def write(self, s: str):
            if self._closed:
                return 0
            if not isinstance(s, str):
                s = str(s)
            self._buf += s
            out = []
            while True:
                nl = self._buf.find("\n")
                if nl < 0:
                    break
                line = self._buf[:nl+1]
                self._buf = self._buf[nl+1:]
                if not any(p.search(line) for p in self._pats):
                    out.append(line)
                # 匹配到了就丢弃该行
            if out:
                return self._under.write("".join(out))
            return 0


        def flush(self):
            if self._closed:
                return None
            # 刷新半行（没有换行符的尾巴）也过滤一次
            if self._buf:
                tail = self._buf
                self._buf = ""
                if not any(p.search(tail) for p in self._pats):
                    self._under.write(tail)
            result = self._under.flush()
            return result


        def close(self):
            if not self._closed:
                flush_success = False
                try:
                    self.flush()
                except Exception as e:
                    # 记录 flush 失败但不阻止关闭操作
                    logging.getLogger(__name__).debug(
                        f"Flush failed during close: {e}"
                    )
                else:
                    # 只有在没有异常时才标记为成功
                    flush_success = True
                finally:
                    self._closed = True
                return flush_success
            return True


    def _attach_filter_to(logger_name: str, filt: logging.Filter) -> None:
        try:
            lg = logging.getLogger(logger_name)
        except (TypeError, ValueError) as e:
            # 只捕获预期的异常类型
            logging.getLogger(__name__).warning(
                f"Invalid logger name '{logger_name}': {e}"
            )
            return
        
        # 明确指定要处理的异常类型
        try:
            for handler in list(lg.handlers):
                handler.addFilter(filt)
            lg.addFilter(filt)
        except (AttributeError, RuntimeError) as e:
            logging.getLogger(__name__).debug(
                f"Failed to add filter to logger '{logger_name}': {e}"
            )


    def _install_logging_filters() -> None:
        pats = []
        if _HEALTH_ON:
            pats.append(_HEALTH_PAT)
        if _BATCH_ON:
            pats.append(_BATCH_PAT)
        if _PYNVML_ON:
            pats.append(_PYNVML_PAT)
        if not pats:
            return
        filt = _DropByRegex(pats)

        # root + 常见子 logger（uvicorn/httpx/sglang/vllm 等）
        targets = [
            "sglang", "sglang.server", "sglang.runtime",
            "vllm", "vllm.entrypoints", "vllm.engine", 
            "vllm_ascend", "mindie", "wings"
        ]
        for name in targets:
            _attach_filter_to(name, filt)


    def _install_warning_filters() -> None:
        if not _PYNVML_ON:
            return
        # 屏蔽 torch.cuda 的 pynvml FutureWarning
        try:
            warnings.filterwarnings(
                "ignore",
                message=r".*pynvml package is deprecated.*",
                category=FutureWarning,
                module=r"torch\.cuda(\..*)?$",
            )
        except Exception as e:
            # 警告过滤器设置失败不影响主要功能，记录日志后继续
            logging.getLogger(__name__).debug(
                f"Failed to install pynvml warning filter: {e}"
            )


    def _install_stdio_filters() -> None:
        if not _STDIO_ON:
            return
        pats = []
        if _HEALTH_ON:
            pats.append(_HEALTH_PAT)
        if _BATCH_ON:
            pats.append(_BATCH_PAT)
        if _PYNVML_ON:
            pats.append(_PYNVML_PAT)
        if not pats:
            return
        try:
            sys.stdout = _LineFilterIO(sys.stdout, pats, name="stdout")
            sys.stderr = _LineFilterIO(sys.stderr, pats, name="stderr")
        except Exception as e:
            # 某些环境不允许替换（很少见），记录并继续
            logging.getLogger(__name__).debug(
                f"Failed to install stdio filters: {e}"
            )


    def install_noise_filters() -> None:
        """
        在主程序尽早调用（越早越好，最好在 import 大型引擎前）。
        可以多次调用，重复挂载不会产生副作用。
        """
        _install_logging_filters()
        _install_warning_filters()
        _install_stdio_filters()