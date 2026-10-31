#!/usr/bin/env python3
"""
queue.py — 异步优先级任务队列

替代 bot.py 中 `threading.Thread(...).start()` 的无控制并发：
- 按优先级（HIGH/NORMAL/LOW）排队
- 固定数量 worker 协程
- 超时任务降级重试
- 重试耗尽记录错误
"""
import asyncio
import time
import uuid
from enum import IntEnum
from dataclasses import dataclass, field
from typing import Callable, Any

try:
    from messaging import log as _log
except Exception:
    def _log(msg):
        print(f"[qq-bot] {msg}", flush=True)


class Priority(IntEnum):
    HIGH = 1     # 管理员指令
    NORMAL = 2   # 普通消息
    LOW = 3      # 后台分析（人物画像、摘要）


@dataclass
class Task:
    id: str
    priority: Priority
    handler: Callable
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    timeout: float = 30.0
    retry_count: int = 0


class TaskQueue:
    """优先级队列 + 工作协程池。

    用法::
        queue = TaskQueue(max_workers=3)
        await queue.start()
        await queue.submit(Task(id="1", priority=Priority.NORMAL, handler=fn, args=(event,)))
        # 关闭：
        await queue.shutdown()
    """

    def __init__(self, max_workers: int = 3):
        self._queues = {p: asyncio.Queue() for p in Priority}
        self._max_workers = max_workers
        self._workers = []
        self._running = False

    async def start(self):
        """启动 worker 协程"""
        if self._running:
            return
        self._running = True
        self._workers = [asyncio.create_task(self._worker(i)) for i in range(self._max_workers)]
        _log(f"[QUEUE] started {self._max_workers} workers")

    async def submit(self, task: Task):
        """提交任务到对应优先级队列"""
        if not task.id:
            task.id = f"task_{uuid.uuid4().hex[:8]}"
        await self._queues[Priority(task.priority)].put(task)

    async def _worker(self, worker_id: int):
        """工作协程：按优先级取任务 → 执行 → 超时降级重试"""
        while self._running:
            task = await self._get_next_task()
            if task is None:
                continue
            try:
                result = await asyncio.wait_for(
                    self._run_sync(task),
                    timeout=task.timeout,
                )
            except asyncio.TimeoutError:
                if task.retry_count < 2:
                    task.retry_count += 1
                    task.priority = Priority.LOW  # 降级到低优先级重试
                    task.id = f"{task.id}_r{task.retry_count}"
                    await self._queues[Priority.LOW].put(task)
                    _log(f"[QUEUE] task {task.id} timed out, demoted to LOW (retry {task.retry_count})")
                else:
                    _log(f"[QUEUE] task {task.id} EXHAUSTED retries after timeout")
            except Exception as e:
                _log(f"[QUEUE] task {task.id} failed: {e}")

    async def _run_sync(self, task: Task) -> Any:
        """在线程中运行同步 handler，避免阻塞事件循环"""
        handler = task.handler
        if asyncio.iscoroutinefunction(handler):
            return await handler(*task.args, **task.kwargs)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: handler(*task.args, **task.kwargs))

    async def _get_next_task(self):
        """按优先级从高到低非阻塞取任务"""
        for priority in sorted(Priority, key=lambda p: p.value):
            try:
                return self._queues[priority].get_nowait()
            except asyncio.QueueEmpty:
                continue
        # 所有队列空 → 等一小段时间
        await asyncio.sleep(0.1)
        return None

    async def shutdown(self):
        """停止 worker"""
        self._running = False
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        _log("[QUEUE] shutdown complete")
