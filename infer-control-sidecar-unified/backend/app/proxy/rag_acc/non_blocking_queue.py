import asyncio
import collections


class NonBlockingQueue:
    def __init__(self):
        self._queue = collections.deque()
        self._lock = asyncio.Lock()
        self._finished = False

    def qsize(self):
        return len(self._queue)

    def empty(self):
        return len(self._queue) == 0

    async def put(self, item):
        async with self._lock:
            self._queue.append(item)

    def get_nowait(self):
        if self.empty():
            raise asyncio.QueueEmpty
        return self._queue.popleft()

    def peek_nowait(self):
        if self.empty():
            raise asyncio.QueueEmpty
        return self._queue[0]

    async def get(self):
        while True:
            try:
                return self.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.01)

    def finish(self):
        self._finished = True

    def is_finished(self):
        return self._finished and self.empty()

    def prepend(self, item):
        self._queue.appendleft(item)
