# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: proxy/settings.py
# Purpose: Proxy runtime settings loader with safe argument parsing behavior.
# Status: Active reused config module with launcher-friendly adaptation.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Must not consume launcher argv on import.
# - Prefer env-driven configuration to avoid process argument conflicts.
# -----------------------------------------------------------------------------
# -*- coding: utf-8 -*-
"""
 / app/queueing
- RETRY_TRIES / RETRY_INTERVAL_MS 3 100ms
"""

import os
import argparse
import logging

#
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("reason-proxy")

#


def parse_args():
    # Parse only proxy-known flags to avoid consuming launcher argv.
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--backend", default=os.getenv("BACKEND_URL", "http://172.17.0.3:17000 "))
    p.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.getenv("PORT", 6688)))
    args, _ = p.parse_known_args()
    return args

args = parse_args()
BACKEND_URL = args.backend.strip()
HOST = args.host
PORT = args.port


#
#  3600 1
BACKEND_PROBE_TIMEOUT = int(os.getenv("BACKEND_PROBE_TIMEOUT", "3600"))


#
for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)

#  httpx &
FAST_PATH_BYTES = int(os.getenv("FAST_PATH_BYTES", "128"))
FIRST_FLUSH_BYTES = int(os.getenv("FIRST_FLUSH_BYTES", "256"))
FIRST_FLUSH_MS = float(os.getenv("FIRST_FLUSH_MS", "0.0"))
STREAM_FLUSH_BYTES = int(os.getenv("STREAM_FLUSH_BYTES", "8192"))
STREAM_FLUSH_MS = float(os.getenv("STREAM_FLUSH_MS", "0.006"))
NONSTREAM_THRESHOLD = int(os.getenv("NONSTREAM_PIPE_THRESHOLD", str(256 * 1024)))

# /keepalive
MAX_CONN = int(os.getenv("HTTPX_MAX_CONNECTIONS", "2048"))
MAX_KEEPALIVE = int(os.getenv("HTTPX_MAX_KEEPALIVE", "256"))
KEEPALIVE_EXPIRY = float(os.getenv("HTTPX_KEEPALIVE_EXPIRY", "30"))

# HTTP/2 +
HTTP2_ENABLED = os.getenv("HTTP2_ENABLED", "true").lower() != "false"
H2_MAX_STREAMS = int(os.getenv("HTTP2_MAX_STREAMS", "128"))

#
# RETRY_TRIES  3   +  2
RETRY_TRIES = int(os.getenv("RETRY_TRIES", "3"))
RETRY_INTERVAL_MS = int(os.getenv("RETRY_INTERVAL_MS", "100"))  #  sleep
ENABLE_DELIM_FLUSH = os.getenv("ENABLE_DELIM_FLUSH", "true").lower() != "false"

#
WARMUP_CONN = int(os.getenv("WARMUP_CONN", str(min(MAX_KEEPALIVE or 50, 200))))
WARMUP_PROMPT = os.getenv("WARMUP_PROMPT", "").strip()
WARMUP_ROUNDS = int(os.getenv("WARMUP_ROUNDS", "1"))
WARMUP_TIMEOUT = float(os.getenv("WARMUP_TIMEOUT", "10"))

#
GLOBAL_PASS_THROUGH_LIMIT = int(os.getenv("GLOBAL_PASS_THROUGH_LIMIT", "1024"))
GLOBAL_QUEUE_MAXSIZE = int(os.getenv("GLOBAL_QUEUE_MAXSIZE", "1024"))

# / uvicorn --workers =CPU-1=1
#WORKERS = max(1, os.cpu_count() - 1)
WORKERS = 1
WORKER_INDEX = int(os.getenv("WORKER_INDEX", "-1"))

#  RAG
RAG_ACC_ENABLED = os.getenv("RAG_ACC_ENABLED", "false").lower() != "false"


def _split_strict(total: int, workers: int, idx: int) -> int:
    """
     extra  worker +1 base
     idx  floor
    """
    if workers <= 0:
        return total
    base = total // workers
    extra = total % workers
    if 0 <= idx < workers:
        return base + (1 if idx < extra else 0)
    return base

#  worker
LOCAL_PASS_THROUGH_LIMIT = _split_strict(GLOBAL_PASS_THROUGH_LIMIT, WORKERS, WORKER_INDEX)
LOCAL_QUEUE_MAXSIZE = _split_strict(GLOBAL_QUEUE_MAXSIZE, WORKERS, WORKER_INDEX)

#  app/healthz
MAX_INFLIGHT = LOCAL_PASS_THROUGH_LIMIT
QUEUE_MAXSIZE = LOCAL_QUEUE_MAXSIZE

#
QUEUE_TIMEOUT = float(os.getenv("QUEUE_TIMEOUT", "15.0"))

# reject | drop_oldest | drop_newest
QUEUE_REJECT_POLICY = os.getenv("QUEUE_REJECT_POLICY", "drop_oldest").lower()

# block | reject
QUEUE_OVERFLOW_MODE = os.getenv("QUEUE_OVERFLOW_MODE", "block").lower()

#
GATE0_TOTAL = WORKERS
GATE0_LOCAL_CAP = _split_strict(GATE0_TOTAL, WORKERS, WORKER_INDEX)
GATE1_LOCAL_CAP = max(0, LOCAL_PASS_THROUGH_LIMIT - GATE0_LOCAL_CAP)

#
USE_GLOBAL_GATE = os.getenv("USE_GLOBAL_GATE", "false").lower() == "true"
GATE_SOCK = os.getenv("GATE_SOCK", "")



def log_boot_plan():
    logger.info("Backend = %s", BACKEND_URL)
    logger.info(
        "Plan: WORKERS=%s INDEX=%s | GLOBAL(inflight=%s, queue=%s) -> LOCAL(inflight=%s, queue=%s) | "
        "GATE0_TOTAL=%s -> G0_LOCAL=%s, G1_LOCAL=%s | HTTP2=%s H2_MAX_STREAMS=%s | RETRY_TRIES=%s INTERVAL=%sms",
        WORKERS, WORKER_INDEX,
        GLOBAL_PASS_THROUGH_LIMIT, GLOBAL_QUEUE_MAXSIZE,
        LOCAL_PASS_THROUGH_LIMIT, LOCAL_QUEUE_MAXSIZE,
        GATE0_TOTAL, GATE0_LOCAL_CAP, GATE1_LOCAL_CAP,
        HTTP2_ENABLED, H2_MAX_STREAMS, RETRY_TRIES, RETRY_INTERVAL_MS,
    )
