#!/usr/bin/env python3
"""
retry.py — 指数退避重试装饰器

为不稳定的 I/O 调用（HTTP API、AI 推理）提供自动重试能力。
仅对可重试异常（ConnectionError/TimeoutError 等）重试，其他异常立即抛出。
"""
import functools
import random
import time
from typing import Callable, Tuple, Type

# 对外便于其他模块引用
__all__ = ["with_retry", "DEFAULT_RETRYABLE"]


def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    backoff: float = 2.0,
    retryable_exceptions: Tuple[Type[BaseException], ...] = (ConnectionError, TimeoutError),
):
    """指数退避重试装饰器。

    用法::
        @with_retry(max_retries=2, retryable_exceptions=(URLError, TimeoutError))
        def _api(...): ...

    - 仅 retryable_exceptions 中的异常会触发重试，其他异常立即抛出
    - 延迟公式：base_delay * backoff^attempt + random_jitter(0~0.5s)
    - 重试耗尽后抛出最后一次异常
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exc = e
                    if attempt < max_retries:
                        delay = base_delay * (backoff ** attempt) + random.uniform(0, 0.5)
                        try:
                            from messaging import log
                            log(f"[RETRY] {func.__name__} attempt {attempt + 1}/{max_retries + 1} failed: {e}, retrying in {delay:.1f}s")
                        except Exception:
                            pass
                        time.sleep(delay)
                    else:
                        try:
                            from messaging import log
                            log(f"[RETRY] {func.__name__} exhausted {max_retries + 1} attempts, giving up")
                        except Exception:
                            pass
                except Exception:
                    # 非 retryable 异常立即抛出，不重试
                    raise
            raise last_exc  # type: ignore[misc]
        return wrapper
    return decorator
