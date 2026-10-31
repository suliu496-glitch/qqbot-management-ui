#!/usr/bin/env python3
"""
resources.py — HTTP 连接池 + WebSocket 连接管理

避免每次 API 调用新建 TCP 连接。httpx AsyncClient 连接复用。
渐进策略：先对高频调用（_api、_call_deepseek）加连接池。
"""
import asyncio
from typing import Optional

try:
    from messaging import log as _log
except Exception:
    def _log(msg):
        print(f"[qq-bot] {msg}", flush=True)


class ConnectionPool:
    """HTTP 连接池管理（httpx AsyncClient 复用）"""

    _client: Optional["httpx.AsyncClient"] = None  # 延迟 import
    _lock = asyncio.Lock()

    @classmethod
    async def get_client(cls):
        """获取共享 AsyncClient（线程安全）"""
        if cls._client is None:
            async with cls._lock:
                if cls._client is None:
                    import httpx
                    cls._client = httpx.AsyncClient(
                        limits=httpx.Limits(
                            max_connections=20,
                            max_keepalive_connections=10,
                        ),
                        timeout=30.0,
                    )
                    _log("[RESOURCES] HTTP connection pool created")
        return cls._client

    @classmethod
    async def close_all(cls):
        """优雅关闭"""
        if cls._client is not None:
            await cls._client.aclose()
            cls._client = None
            _log("[RESOURCES] HTTP connection pool closed")

    @classmethod
    def is_ready(cls) -> bool:
        return cls._client is not None
