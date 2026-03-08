# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: proxy/queueing.py
# Purpose: Queueing and concurrency primitives for controlling proxy request admission.
# Status: Active reused flow-control module.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Do not break fairness and backpressure behavior.
# - Keep interfaces stable for gateway integration.
# -----------------------------------------------------------------------------
# -*- coding: utf-8 -*-
"""
 + FIFO  worker
-
   Gate-0 256  workerC.GATE0_LOCAL_CAP
   Gate-1C.LOCAL_PASS_THROUGH_LIMIT - Gate-0
- asyncio.Queue  = LOCAL_QUEUE_MAXSIZE
- acquire():
    1)  Gate-0 Gate-1
    2)  acquire
- release():
    1)    +
    2)    semaphore
- QUEUE_OVERFLOW_MODE=blockdrop_oldestqueue disabled
"""

import time
import asyncio
import json
from typing import Dict, Optional
from fastapi import HTTPException
from . import settings as C


def _jlog(evt: str, **fields):
    try:
        log_entry = {"evt": evt, **fields}
        C.logger.info(json.dumps(log_entry, ensure_ascii=False))
    except (TypeError, ValueError) as e:
        C.logger.error(f"Failed to serialize log entry: {log_entry}", exc_info=e)
    except Exception as e:
        C.logger.error("Unexpected error while writing log", exc_info=e)


def _elog(evt: str, **fields):
    try:
        log_entry = {"evt": evt, **fields}
        C.logger.error(json.dumps(log_entry, ensure_ascii=False))
    except (TypeError, ValueError) as e:
        C.logger.error(f"Failed to serialize log entry: {log_entry}", exc_info=e)
    except Exception as e:
        C.logger.error("Unexpected error while writing log", exc_info=e)


def _ms(sec: float) -> str:
    return f"{sec*1000:.1f}ms"


class Waiter:
    """(no description)"""
    __slots__ = ("fut", "enq_ts", "pos")

    def __init__(self, fut: asyncio.Future, enq_ts: float, pos: int):
        self.fut = fut          #  set_result(layer:int)  0:Gate-0, 1:Gate-1
        self.enq_ts = enq_ts    #
        self.pos = pos          #


class QueueGate:
    """(no description)"""
    def __init__(self):
        #  app/healthz
        self.max_inflight = int(C.LOCAL_PASS_THROUGH_LIMIT)

        #   +
        g0_cap = max(0, int(getattr(C, "GATE0_LOCAL_CAP", 0)))
        g1_cap = max(0, int(getattr(C, "GATE1_LOCAL_CAP", max(0, self.max_inflight - g0_cap))))

        self.g0_cap = g0_cap
        self.g1_cap = g1_cap
        self.g0 = asyncio.Semaphore(self.g0_cap) if self.g0_cap > 0 else None
        self.g1 = asyncio.Semaphore(self.g1_cap) if self.g1_cap > 0 else None

        #
        self.max_qsize = int(C.LOCAL_QUEUE_MAXSIZE)
        self.q: Optional[asyncio.Queue[Waiter]] = (
            asyncio.Queue(maxsize=self.max_qsize) if self.max_qsize > 0 else None
        )

        # 0/1
        self._holders: Dict[int, int] = {}

        _jlog("qgate_init",
              max_inflight=self.max_inflight,
              g0_cap=self.g0_cap, g1_cap=self.g1_cap,
              qmax=self.max_qsize)
    #  Gate-0 + Gate-1  #

    @property
    def inflight(self) -> int:
        return self._sem_inflight(self.g0, self.g0_cap) + self._sem_inflight(self.g1, self.g1_cap)


    #    #


    @staticmethod
    def _task_id() -> int:
        t = asyncio.current_task()
        return 0 if t is None else id(t)


    @staticmethod
    def _has_ticket(sem: Optional[asyncio.Semaphore]) -> bool:
        if sem is None:
            return False
        v = getattr(sem, "_value", 0)
        return v > 0


    @staticmethod
    def _sem_inflight(sem: Optional[asyncio.Semaphore], cap: int) -> int:
        if sem is None or cap <= 0:
            return 0
        rem = getattr(sem, "_value", None)
        if rem is None:
            return 0
        return max(0, cap - int(rem))


    @staticmethod
    def _queue_disabled_raise(self, rid: str | None) -> None:
        _elog("qgate_queue_disabled", rid=rid)
        raise HTTPException(
            status_code=503, detail="server busy: queue disabled",
            headers={"Retry-After": "1", "Connection": "close", "X-Queue-Disabled": "true"}
        )


    #  QueueGate /


    def obs_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """/"""
        hdr = {
            "X-InFlight": str(self.inflight),
            "X-Queue-Size": str(self.queue_size()),
            "X-Local-MaxInflight": str(self.max_inflight),
            "X-Local-QueueMax": str(self.max_qsize),
            "X-Workers": str(C.WORKERS),
            "X-Global-MaxInflight": str(C.GLOBAL_PASS_THROUGH_LIMIT),
            "X-Global-QueueMax": str(C.GLOBAL_QUEUE_MAXSIZE),
            "X-Queue-Timeout-Sec": str(C.QUEUE_TIMEOUT),
            #
            "X-InFlight-G0": str(self._sem_inflight(self.g0, self.g0_cap)),
            "X-InFlight-G1": str(self._sem_inflight(self.g1, self.g1_cap)),
            "X-MaxInflight-G0": str(self.g0_cap),
            "X-MaxInflight-G1": str(self.g1_cap),
        }
        if extra:
            hdr.update(extra)
        return hdr


    async def release(self):
        """

          -    semaphore
          -    sem inflight-1
        """
        # 0/1 0
        task_id = self._task_id()
        layer = self._holders.pop(task_id, 0)

        #  sem
        if self.q is not None:
            while not self.q.empty():
                waiter: Waiter = await self.q.get()
                if waiter.fut.cancelled() or waiter.fut.done():
                    continue
                try:
                    waiter.fut.set_result(layer)  #
                    _jlog("qgate_handover", layer=layer, remain_qsize=self.queue_size())
                except Exception as e:
                    _elog("qgate_handover_error", layer=layer, error=str(e))
                return

        #
        sem = self.g0 if layer == 0 else self.g1
        if sem is not None:
            try:
                sem.release()
                _jlog("qgate_release", layer=layer)
            except ValueError:
                #
                _elog("qgate_release_double", layer=layer)


    def queue_size(self) -> int:
        """(no description)"""
        return 0 if self.q is None else self.q.qsize()

    async def acquire(self, req_headers: Dict[str, str]) -> Dict[str, str]:
        """

        1)  Gate-0    Gate-0
        2)  Gate-1    Gate-1
        3)   //  503
        4)  acquire
        """
        headers_out: Dict[str, str] = {}
        t0 = time.perf_counter()
        rid = req_headers.get("x-request-id")

        self._log_acquire_try(rid, t0)

        #  Gate-0 Gate-1
        if await self._try_direct_gate(self.g0, self.g0_cap, 0, rid, t0):
            headers_out["X-Queued-Wait"] = "0.0ms"
            return headers_out

        if await self._try_direct_gate(self.g1, self.g1_cap, 1, rid, t0):
            headers_out["X-Queued-Wait"] = "0.0ms"
            return headers_out


        #
        if self.q is None:
            self._queue_disabled_raise(rid)

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        waiter = Waiter(fut=fut, enq_ts=time.perf_counter(), pos=0)

        #
        if self.q.full():
            self._handle_queue_full(rid)

        #
        await self._enqueue_waiter(waiter, headers_out, rid)

        #  sem.acquire
        layer = await self._wait_for_wakeup(fut, waiter, rid)
        return self._inherit_occupy(layer, headers_out, waiter, rid)

    def _log_acquire_try(self, rid: str | None, t0: float) -> None:
        _jlog(
            "qgate_acquire_try",
            rid=rid,
            inflight=self.inflight,
            g0_inflight=self._sem_inflight(self.g0, self.g0_cap),
            g1_inflight=self._sem_inflight(self.g1, self.g1_cap),
            qsize=self.queue_size(),
        )

    def _handle_queue_full(self, rid: str | None) -> None:
        policy = C.QUEUE_REJECT_POLICY
        if policy == "drop_oldest":
            dropped = None
            while not self.q.empty():
                w: Waiter = self.q.get_nowait()
                if not (w.fut.cancelled() or w.fut.done()):
                    w.fut.set_exception(HTTPException(
                        status_code=503, detail="server busy: dropped oldest",
                        headers={"Retry-After": "1", "Connection": "close", "X-Queue-Dropped": "oldest"}
                    ))
                    dropped = w
                    break
            _elog("qgate_drop_oldest", rid=rid, dropped=bool(dropped))
            if dropped is None and C.QUEUE_OVERFLOW_MODE != "block":
                _elog("qgate_queue_full_reject", rid=rid)
                raise HTTPException(
                    status_code=503, detail="server busy: queue full",
                    headers={"Retry-After": "1", "Connection": "close", "X-Queue-Full": "true"}
                )
            #  block put()
        elif C.QUEUE_OVERFLOW_MODE != "block":
            _elog("qgate_queue_full_reject", rid=rid)
            raise HTTPException(
                status_code=503, detail="server busy: queue full",
                headers={"Retry-After": "1", "Connection": "close", "X-Queue-Full": "true"}
            )
        # else:  block put()

    async def _enqueue_waiter(self, waiter: "Waiter", headers_out: Dict[str, str], rid: str | None) -> None:
        pos = self.q.qsize() + 1
        waiter.pos = pos
        await self.q.put(waiter)
        headers_out["X-Queue-Position"] = str(pos)
        if C.QUEUE_OVERFLOW_MODE == "block":
            headers_out["X-Queue-Overflow"] = "block"
        _jlog("qgate_enqueued", rid=rid, pos=pos, qsize=self.queue_size())

    async def _wait_for_wakeup(self, fut: asyncio.Future, waiter: "Waiter", rid: str | None) -> int:
        try:
            layer = await asyncio.wait_for(fut, timeout=C.QUEUE_TIMEOUT)
            return int(layer) if layer in (0, 1) else 0
        except asyncio.TimeoutError as e:
            if not fut.done():
                fut.cancel()
            _elog("qgate_timeout", rid=rid, waited=_ms(time.perf_counter() - waiter.enq_ts))
            raise HTTPException(
                status_code=503,
                detail="server busy: queue timeout",
                headers={"Retry-After": "1", "Connection": "close", "X-Queue-Timeout": "true"}
            ) from e

    def _inherit_occupy(
            self, layer: int,
            headers_out: Dict[str, str],
            waiter: "Waiter",
            rid: str | None) -> Dict[str, str]:
        self._holders[self._task_id()] = layer
        headers_out["X-Queued-Wait"] = f"{(time.perf_counter() - waiter.enq_ts) * 1e3:.1f}ms"
        _jlog("qgate_wakeup", rid=rid, layer=layer, waited=headers_out["X-Queued-Wait"])
        return headers_out

    # async def _try_direct_gate(self, gate, cap, layer, headers_out, rid, t0) -> bool:
    async def _try_direct_gate(
        self,
        gate: asyncio.Semaphore | None,
        cap: int,
        layer: int,
        rid: str | None,
        t0: float,
    ) -> bool:
        if cap > 0 and self._has_ticket(gate):
            await gate.acquire()
            self._holders[self._task_id()] = layer
            _jlog("qgate_acquire_direct", rid=rid, layer=layer, elapsed=_ms(time.perf_counter() - t0))
            return True
        return False