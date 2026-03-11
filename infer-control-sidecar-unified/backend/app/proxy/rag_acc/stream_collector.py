import asyncio
import json
import re
import time
import requests
from fastapi.responses import StreamingResponse
from app.proxy.settings import logger
from app.proxy.rag_acc.non_blocking_queue import NonBlockingQueue


class StreamCollector:
    def __init__(self, chunk_num, chunk_request_func, combine_request_func, only_combine=False):
        self.chunk_num = chunk_num
        self.chunk_request = chunk_request_func
        self.combine_request = combine_request_func
        self.only_combine = only_combine
        self.collectors = []
        self.queues = []
        self.buffers = []
        self.dones = []
        self.starts = []
        self.combine_started = False
        self.buffered_outputs = []
        self.slow_delay = 0.5
        self.fast_delay = 0
        self.combine_start_time = None
        self.start_time = None

    async def collect_and_stream(self):
        self.start_time = time.time()
        await self._initialize_collectors()
        logger.debug(f"create task for combine")
        combine_queue = NonBlockingQueue()
        self.collectors.append(asyncio.create_task(
            self._combine_results(combine_queue)))
        combine_monitor_task = asyncio.create_task(
            self._monitor_combine_start(combine_queue))
        async for sse_res in self._stream_chunk_results():
            yield sse_res
        await combine_monitor_task
        async for sse_res in self._stream_combine_results(combine_queue):
            yield sse_res
        logger.debug(f"sse res: data: [DONE]\n\n")
        yield "data: [DONE]\n\n"
        logger.debug(f"collectors gather wait")
        await asyncio.gather(*self.collectors)
        logger.debug(f"collectors gather done")

    async def _collect_single_chunk(self, index):
        logger.debug(f"q-{index}, collect init")
        start = self.starts[index]
        await start.get()
        logger.debug(f"q-{index}, collect start")
        queue = self.queues[index]
        done = self.dones[index]
        resp = self.chunk_request(index)
        try:
            async for res in self._process_chunk_response(resp, index):
                await queue.put(res)
                await asyncio.sleep(0)
            logger.debug(f"q-{index}, done")
            await queue.put(None)
            await done.put(None)
        except Exception as e:
            logger.error(f"Error in _collect_single_chunk[{index}]: {e}")
            await queue.put(None)
            await done.put(None)

    async def _process_chunk_response(self, resp, index):
        buffer = ""
        for raw_chunk in resp.iter_content(chunk_size=None):
            text = raw_chunk.decode("utf-8")
            lines = (buffer + text).splitlines(True)
            buffer = "" if text.endswith('\n') else lines.pop()
            for line in lines:
                line = line.strip()
                if line == "":
                    continue
                if line.startswith("data: "):
                    json_str = line[6:]
                    if json_str == "[DONE]":
                        break
                    try:
                        parsed = json.loads(json_str)
                        if "choices" in parsed and len(parsed["choices"]) > 0:
                            delta = parsed["choices"][0].get("delta", {})
                            content = delta.get("content", "")
                            filtered_content = re.sub(r'<\/think>', '', content)
                            self.buffers[index] += filtered_content
                            parsed["choices"][0]["delta"]["content"] = filtered_content
                            yield parsed
                        else:
                            logger.debug(f"No valid choices in chunk [{index}]: {parsed}")
                    except json.JSONDecodeError as e:
                        logger.warning(f"Failed to decode JSON from chunk [{index}]: {line}. Error: {e}")

    def _calculate_slow_delay(self):
        elapsed_time = time.time() - self.start_time
        if elapsed_time < 1:
            return 0.0
        queue_count = sum(queue.qsize() for queue in self.queues)
        logger.debug(f"Queue count: {queue_count}, Elapsed time: {elapsed_time:.2f}s")
        if queue_count <= 5 and elapsed_time >= 3:
            logger.debug("Apply delay of 0.5s: low queue count (<5) and sufficient elapsed time (>3s)")
            return 0.5
        if queue_count <= 10:
            logger.debug("Apply delay of 0.2s: queue count between 6-10")
            return 0.2
        if queue_count <= 30:
            logger.debug("Apply delay of 0.1s: queue count between 11-30")
            return 0.1
        logger.debug("No delay applied: high queue count (>30)")
        return 0.0

    async def _combine_results(self, queue):
        logger.debug(f"combine, start")
        await asyncio.sleep(5)
        resp = self.combine_request("<|preparation|>")
        logger.debug(f"waiting for first-level response to complete")
        for index, done in enumerate(self.dones):
            await done.get()
            logger.debug(f"combine, q-{index} is done")
        logger.debug(f"first-level response completed, starting second-level reasoning, first-level response {self.buffers}")
        resp = self.combine_request("\n\n".join(self.buffers))
        logger.debug(f"combine, resp {resp}")
        try:
            async for res in self._process_combine_response(resp):
                await queue.put(res)
                await asyncio.sleep(0)
            await queue.put(None)
        except Exception as e:
            logger.error(f"Error in _combine_results: {e}", exc_info=True)
            await queue.put(None)

    async def _process_combine_response(self, resp):
        buffer = ""
        for raw_chunk in resp.iter_content(chunk_size=None):
            text = raw_chunk.decode("utf-8")
            lines = (buffer + text).splitlines(True)
            buffer = "" if text.endswith('\n') else lines.pop()
            for line in lines:
                line = line.strip()
                if line == "":
                    continue
                if line.startswith("data: "):
                    json_str = line[6:]
                    if json_str == "[DONE]":
                        break
                    try:
                        parsed = json.loads(json_str)
                        if "choices" in parsed and len(parsed["choices"]) > 0:
                            delta = parsed["choices"][0].get("delta", {})
                            content = delta.get("content", "")
                            filtered_content = re.sub(r'<think>', '', content)
                            parsed["choices"][0]["delta"]["content"] = filtered_content
                            yield parsed
                        else:
                            logger.debug(f"No valid choices in combine response: {parsed}")
                    except json.JSONDecodeError as e:
                        logger.warning(f"Failed to decode JSON from combine response: {line}. Error: {e}")

    async def _monitor_combine_start(self, combine_queue):
        res = await combine_queue.get()
        if res is not None:
            self.combine_started = True
            combine_queue.prepend(res)
        else:
            await combine_queue.put(None)

    async def _stream_chunk_results(self):
        for index, queue in enumerate(self.queues):
            logger.debug(f"try to get res from q-{index}")
            while True:
                res = await queue.get()
                if res is None:
                    break
                sse_res = f"data: {json.dumps(res)}\n\n"
                if not self.only_combine:
                    if not self.combine_started:
                        self.slow_delay = self._calculate_slow_delay()
                    delay = 0 if self.combine_started else self.slow_delay
                    if delay != 0:
                        await asyncio.sleep(delay)
                    yield sse_res

    async def _stream_combine_results(self, combine_queue):
        while True:
            res = await combine_queue.get()
            if res is None:
                break
            sse_res = f"data: {json.dumps(res)}\n\n"
            yield sse_res

    async def _initialize_collectors(self):
        queue = NonBlockingQueue()
        self.queues.append(queue)
        start = NonBlockingQueue()
        self.starts.append(start)
        logger.debug(f"kick off chunk")
        await start.put(None)
        done = NonBlockingQueue()
        self.dones.append(done)
        self.buffers.append("")
        logger.debug(f"create task for chunk")
        self.collectors.append(asyncio.create_task(
            self._collect_single_chunk(0)))
