# -*- coding: utf-8 -*-
"""
并发闸门 + FIFO 等待队列（每 worker）：
- 双层闸门：
  · Gate-0（底座）：按全局 256 等分到各 worker（C.GATE0_LOCAL_CAP）
  · Gate-1（余量）：C.LOCAL_PASS_THROUGH_LIMIT - Gate-0
- asyncio.Queue 作为等待缓冲（长度 = LOCAL_QUEUE_MAXSIZE）
- acquire():
    1) 优先占 Gate-0，无票再占 Gate-1；否则入队等待
    2) 被唤醒时直接“继承”上一请求的闸门层级（不再 acquire）
- release():
    1) 若队列有人 → 直接把“占用权 + 所在层级”移交队首（保持稳定并发）
    2) 否则 → 归还本次所占用层级的 semaphore
- 兼容：QUEUE_OVERFLOW_MODE=block、drop_oldest、queue disabled 等策略
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
    """
    等待队列的等待者类，用于在请求等待过程中保存相关信息。
    """
    __slots__ = ("fut", "enq_ts", "pos")

    def __init__(self, fut: asyncio.Future, enq_ts: float, pos: int):
        self.fut = fut          # 被唤醒时 set_result(layer:int) —— 0:Gate-0, 1:Gate-1
        self.enq_ts = enq_ts    # 入队时间
        self.pos = pos          # 入队位置（仅观测）


class QueueGate:
    """
    队列控制器类，负责控制并发资源的分配和排队机制。
    """
    def __init__(self):
        # 总并发上限（保持原名以兼容 app/healthz）
        self.max_inflight = int(C.LOCAL_PASS_THROUGH_LIMIT)

        # ── 双层闸门：底座 + 余量 ──
        g0_cap = max(0, int(getattr(C, "GATE0_LOCAL_CAP", 0)))
        g1_cap = max(0, int(getattr(C, "GATE1_LOCAL_CAP", max(0, self.max_inflight - g0_cap))))

        self.g0_cap = g0_cap
        self.g1_cap = g1_cap
        self.g0 = asyncio.Semaphore(self.g0_cap) if self.g0_cap > 0 else None
        self.g1 = asyncio.Semaphore(self.g1_cap) if self.g1_cap > 0 else None

        # 等待队列：限制“等待队列”的长度
        self.max_qsize = int(C.LOCAL_QUEUE_MAXSIZE)
        self.q: Optional[asyncio.Queue[Waiter]] = (
            asyncio.Queue(maxsize=self.max_qsize) if self.max_qsize > 0 else None
        )

        # 记录每个任务当前占用的是哪一层（0/1），用于移交或最终释放
        self._holders: Dict[int, int] = {}

        _jlog("qgate_init",
              max_inflight=self.max_inflight,
              g0_cap=self.g0_cap, g1_cap=self.g1_cap,
              qmax=self.max_qsize)
    # —— 实时推导在途并发：Gate-0 + Gate-1 —— #

    @property
    def inflight(self) -> int:
        return self._sem_inflight(self.g0, self.g0_cap) + self._sem_inflight(self.g1, self.g1_cap)
    

    # —— 工具 —— #


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


    # ───────── QueueGate 内部私有辅助方法（仅拆分，不改逻辑/变量名） ─────────

    
    def obs_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """观测头（用于响应/错误返回统一携带）"""
        hdr = {
            "X-InFlight": str(self.inflight),
            "X-Queue-Size": str(self.queue_size()),
            "X-Local-MaxInflight": str(self.max_inflight),
            "X-Local-QueueMax": str(self.max_qsize),
            "X-Workers": str(C.WORKERS),
            "X-Global-MaxInflight": str(C.GLOBAL_PASS_THROUGH_LIMIT),
            "X-Global-QueueMax": str(C.GLOBAL_QUEUE_MAXSIZE),
            "X-Queue-Timeout-Sec": str(C.QUEUE_TIMEOUT),
            # 附加观测（不改变原有键名）
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
        完成请求时调用：
          - 若队列有人等待 → 直接把“处理权（含层级）”移交给队首（不释放 semaphore，保持并发稳定）
          - 否则 → 释放对应层级的 sem 许可（inflight-1）
        """
        # 找出当前任务持有的是哪一层（0/1）；默认按 0 兜底
        task_id = self._task_id()
        layer = self._holders.pop(task_id, 0)

        # 优先移交队首（继承层级，不释放 sem）
        if self.q is not None:
            while not self.q.empty():
                waiter: Waiter = await self.q.get()
                if waiter.fut.cancelled() or waiter.fut.done():
                    continue
                try:
                    waiter.fut.set_result(layer)  # 直接把层级移交
                    _jlog("qgate_handover", layer=layer, remain_qsize=self.queue_size())
                except Exception as e:
                    _elog("qgate_handover_error", layer=layer, error=str(e))
                return

        # 无人等待 → 释放并发名额（按层级归还）
        sem = self.g0 if layer == 0 else self.g1
        if sem is not None:
            try:
                sem.release()
                _jlog("qgate_release", layer=layer)
            except ValueError:
                # 防御性：避免重复释放
                _elog("qgate_release_double", layer=layer)

    
    def queue_size(self) -> int:
        """
        获取当前队列大小。
        """
        return 0 if self.q is None else self.q.qsize()
    
    async def acquire(self, req_headers: Dict[str, str]) -> Dict[str, str]:
        """
        获取处理权：
        1) 若 Gate-0 有名额 → 占用 Gate-0（直通）
        2) 否则若 Gate-1 有名额 → 占用 Gate-1（直通）
        3) 否则 → 尝试入队（无队列/队列满/超时 → 503 或阻塞）
        4) 被唤醒后直接拥有处理权（继承层级；不再额外 acquire）
        """
        headers_out: Dict[str, str] = {}
        t0 = time.perf_counter()
        rid = req_headers.get("x-request-id")

        self._log_acquire_try(rid, t0)

        # 直通：优先 Gate-0，其次 Gate-1
        if await self._try_direct_gate(self.g0, self.g0_cap, 0, rid, t0):
            headers_out["X-Queued-Wait"] = "0.0ms"
            return headers_out

        if await self._try_direct_gate(self.g1, self.g1_cap, 1, rid, t0):
            headers_out["X-Queued-Wait"] = "0.0ms"
            return headers_out


        # 无并发名额 → 排队
        if self.q is None:
            self._queue_disabled_raise(rid)

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        waiter = Waiter(fut=fut, enq_ts=time.perf_counter(), pos=0)

        # 队列满 → 根据策略处理
        if self.q.full():
            self._handle_queue_full(rid)

        # 入队（可能阻塞直到出现空位）
        await self._enqueue_waiter(waiter, headers_out, rid)

        # 被唤醒：继承占用层级（无需再次 sem.acquire）
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
            # 若允许 block，则继续阻塞 put()
        elif C.QUEUE_OVERFLOW_MODE != "block":
            _elog("qgate_queue_full_reject", rid=rid)
            raise HTTPException(
                status_code=503, detail="server busy: queue full",
                headers={"Retry-After": "1", "Connection": "close", "X-Queue-Full": "true"}
            )
        # else: 溢出模式为 block，直接阻塞 put()

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