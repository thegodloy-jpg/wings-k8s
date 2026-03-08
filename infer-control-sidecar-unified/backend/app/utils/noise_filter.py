# =============================================================================
# 文件: utils/noise_filter.py
# 用途: 日志/输出噪声过滤辅助，改善运行时信号清晰度
# 状态: 活跃，复用自 wings 项目
#
# 功能概述:
#   本模块用于过滤高频低价值的日志消息，减少日志噪声:
#   - /health 探针访问日志
#   - "Prefill batch." / "Decode batch." 调试日志
#   - pynvml 的 FutureWarning 警告
#   - stdout/stderr 的覆盖过滤
#
# 配置环境变量:
#   - NOISE_FILTER_DISABLE=1       : 完全禁用所有过滤
#   - HEALTH_FILTER_ENABLE=0/1     : /health 日志过滤（默认 1）
#   - BATCH_NOISE_FILTER_ENABLE=0/1: Prefill/Decode 噪声过滤（默认 1）
#   - PYNVML_FILTER_ENABLE=0/1     : pynvml 警告过滤（默认 1）
#   - STDIO_FILTER_ENABLE=0/1      : stdout/stderr 过滤（默认 1）
#
# Sidecar 架构契约:
#   - 避免过滤有意义的错误诊断信息
#   - 过滤规则透明可维护
#
# =============================================================================
# -*- coding: utf-8 -*-
"""
""
------------------------------------------------

- uvicorn  access  "GET /health ..."
-  "Prefill batch." / "Decode batch."
- pynvml  FutureWarningtorch.cuda
-  stdout/stderr print


- NOISE_FILTER_DISABLE=1
- HEALTH_FILTER_ENABLE=0/1          1
- HEALTH_PATH_REGEX=...             "\"GET\\s+/health\\b"
- BATCH_NOISE_FILTER_ENABLE=0/1     1
- BATCH_NOISE_REGEX=...             "(?i)\\b(?:prefill|decode)\\b[^\\n]{0,200}\\bbatch\\b"
- PYNVML_FILTER_ENABLE=0/1          1
- PYNVML_NOISE_REGEX=...            "(?i)(pynvml package is deprecated|\\bimport pynvml\\b)"
- STDIO_FILTER_ENABLE=0/1           1
"""
from __future__ import annotations
import logging
import os
import re
import sys
import warnings
from typing import Optional, Iterable


# -----------------  -----------------
def _env_bool(k: str, default: bool) -> bool:
    v = os.getenv(k, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")

if _env_bool("NOISE_FILTER_DISABLE", False):
    #
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
        """按正则表达式丢弃日志记录的 logging Filter。

        当日志消息匹配任意一个给定 pattern 时，该记录会被丢弃。

        Attributes:
            pats: 编译后的正则表达式元组
        """
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
                    return 0  #
            return 1


    class _LineFilterIO:
        """按行过滤的 stdout/stderr 包装器。

        将原始输出流包装，匹配指定 pattern 的行会被丢弃。
        用于过滤引擎库内部的高频噪声 print。

        Attributes:
            _under: 原始输出流
            _buf:   待处理的缓冲区
            _pats:  过滤用正则表达式元组
        """
        __slots__ = ("_under", "_buf", "_pats", "_closed", "name")


        def __init__(self, under, patterns: Iterable[re.Pattern], name: str):
            self._under = under
            self._buf = ""
            self._pats = tuple(patterns)
            self._closed = False
            self.name = getattr(under, "name", name)


        def __getattr__(self, item):
            #
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
                #
            if out:
                return self._under.write("".join(out))
            return 0


        def flush(self):
            if self._closed:
                return None
            #
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
                    #  flush
                    logging.getLogger(__name__).debug(
                        f"Flush failed during close: {e}"
                    )
                else:
                    #
                    flush_success = True
                finally:
                    self._closed = True
                return flush_success
            return True


    def _attach_filter_to(logger_name: str, filt: logging.Filter) -> None:
        try:
            lg = logging.getLogger(logger_name)
        except (TypeError, ValueError) as e:
            #
            logging.getLogger(__name__).warning(
                f"Invalid logger name '{logger_name}': {e}"
            )
            return

        #
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

        # root +  loggeruvicorn/httpx/sglang/vllm
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
        #  torch.cuda  pynvml FutureWarning
        try:
            warnings.filterwarnings(
                "ignore",
                message=r".*pynvml package is deprecated.*",
                category=FutureWarning,
                module=r"torch\.cuda(\..*)?$",
            )
        except Exception as e:
            #
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
            #
            logging.getLogger(__name__).debug(
                f"Failed to install stdio filters: {e}"
            )


    def install_noise_filters() -> None:
        """
         import

        """
        _install_logging_filters()
        _install_warning_filters()
        _install_stdio_filters()