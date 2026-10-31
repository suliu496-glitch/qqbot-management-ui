#!/usr/bin/env python3
"""
safety.py — 全局安全网：兜底异常捕获 + 熔断 + 恢复

核心设计：
- SafetyNet.protect：装饰器/包装器，任何异常都不逃逸进程
- 三态熔断器（CLOSED / OPEN / HALF_OPEN）
- sys.excepthook 兜底：捕获所有未处理异常
- panic_handler：记录完整堆栈 + 写入 panic 日志
- graceful_shutdown：优雅关闭，保存状态

所有方法均为 classmethod，作为全局单例使用。
"""
import atexit
import functools
import logging
import sys
import threading
import time
import traceback
from datetime import datetime
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# 尝试引入 messaging.log；如果不可用则回退到标准 logging
# ---------------------------------------------------------------------------
try:
    from messaging import log as _log
except Exception:
    _log = logging.getLogger("safety").info  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# 熔断器状态常量
# ---------------------------------------------------------------------------
STATE_CLOSED = "CLOSED"
STATE_OPEN = "OPEN"
STATE_HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpenError(Exception):
    """熔断器打开时抛出的异常"""
    pass


class SafetyNet:
    """全局安全网：兜底异常捕获 + 熔断 + 恢复"""

    # 熔断器状态表：{name: {"state", "failures", "last_failure", "threshold", "recovery_timeout"}}
    _circuit_breakers: dict[str, dict] = {}
    _lock = threading.RLock()  # RLock: 允许 record_failure→circuit_breaker 重入

    # 优雅关闭钩子列表
    _shutdown_handlers: list[Callable] = []

    @classmethod
    def protect(cls, func: Optional[Callable] = None, *, context: str = "", fallback: Any = None):
        """装饰器/包装器：任何异常都不会逃逸进程。

        用法：
            @SafetyNet.protect(context="think")
            def my_func(): ...

            # 或带 fallback：
            SafetyNet.protect(_do_think_and_reply, context="think", fallback=_safe_fallback)

        参数：
            func:       被包装的函数
            context:    上下文标识（用于日志前缀）
            fallback:   异常时的回退值（如果为 None 则返回 None）
        """
        def decorator(fn: Callable) -> Callable:
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                try:
                    return fn(*args, **kwargs)
                except CircuitBreakerOpenError:
                    # 熔断器打开属于"已知保护行为"，不上报为 panic
                    _log(f"[CIRCUIT] {context or fn.__name__} circuit breaker OPEN, skipped")
                    return fallback
                except Exception as e:
                    cls.panic_handler(e, context or fn.__name__)
                    return fallback
            return wrapper

        if func is not None:
            # 被直接调用 SafetyNet.protect(fn, context=...)
            return decorator(func)
        # 被用作 @SafetyNet.protect(context=...)
        return decorator

    @classmethod
    def circuit_breaker(cls, name: str, failure_threshold: int = 5,
                        recovery_timeout: int = 60) -> dict:
        """获取或创建熔断器状态记录。

        三态模型：
        - CLOSED:   正常，调用通过
        - OPEN:     连续失败 ≥ threshold，调用直接抛 CircuitBreakerOpenError
        - HALF_OPEN: 超过 recovery_timeout 后允许一次试探，成功回 CLOSED，失败回 OPEN

        返回当前熔断器状态 dict：
            {"state", "failures", "last_failure", "threshold", "recovery_timeout"}
        """
        with cls._lock:
            if name not in cls._circuit_breakers:
                cls._circuit_breakers[name] = {
                    "state": STATE_CLOSED,
                    "failures": 0,
                    "last_failure": 0.0,
                    "threshold": failure_threshold,
                    "recovery_timeout": recovery_timeout,
                }
            return cls._circuit_breakers[name]

    @classmethod
    def _check_circuit(cls, name: str) -> str:
        """检查熔断器当前状态，返回 STATE_* 并可能触发状态转换。"""
        cb = cls.circuit_breaker(name)
        if cb["state"] == STATE_CLOSED:
            return STATE_CLOSED
        if cb["state"] == STATE_OPEN:
            # 超时则转为 HALF_OPEN 允许试探
            if time.time() - cb["last_failure"] >= cb["recovery_timeout"]:
                with cls._lock:
                    cb["state"] = STATE_HALF_OPEN
                _log(f"[CIRCUIT] {name} → HALF_OPEN (recovery probe)")
                return STATE_HALF_OPEN
            return STATE_OPEN
        # HALF_OPEN 直接返回允许试探
        return STATE_HALF_OPEN

    @classmethod
    def record_success(cls, name: str):
        """记录一次成功调用，熔断器恢复为 CLOSED"""
        with cls._lock:
            cb = cls.circuit_breaker(name)
            if cb["state"] in (STATE_OPEN, STATE_HALF_OPEN):
                _log(f"[CIRCUIT] {name} → CLOSED (recovered)")
            cb["state"] = STATE_CLOSED
            cb["failures"] = 0

    @classmethod
    def record_failure(cls, name: str):
        """记录一次失败调用，可能触发熔断"""
        with cls._lock:
            cb = cls.circuit_breaker(name)
            cb["failures"] += 1
            cb["last_failure"] = time.time()
            if cb["failures"] >= cb["threshold"]:
                if cb["state"] != STATE_OPEN:
                    _log(f"[CIRCUIT] {name} → OPEN (failures={cb['failures']}/{cb['threshold']})")
                cb["state"] = STATE_OPEN

    @classmethod
    def call_with_circuit(cls, name: str, func: Callable, *args, **kwargs) -> Any:
        """带熔断保护的调用。熔断器 OPEN 时直接抛 CircuitBreakerOpenError。"""
        state = cls._check_circuit(name)
        if state == STATE_OPEN:
            raise CircuitBreakerOpenError(
                f"Circuit breaker '{name}' is OPEN after "
                f"{cls._circuit_breakers[name]['failures']} failures"
            )
        try:
            result = func(*args, **kwargs)
            cls.record_success(name)
            return result
        except CircuitBreakerOpenError:
            raise
        except Exception:
            cls.record_failure(name)
            raise

    @classmethod
    def install_excepthook(cls):
        """安装 sys.excepthook，捕获所有未处理异常"""
        def _excepthook(exc_type, exc_value, exc_tb):
            cls.panic_handler(exc_value, f"unhandled {exc_type.__name__}")

        sys.excepthook = _excepthook
        _log("[SAFETY] sys.excepthook installed")

    @classmethod
    def panic_handler(cls, exc: BaseException, context: str = ""):
        """全局恐慌处理器：记录完整堆栈 + 写入 panic 日志"""
        tb_text = traceback.format_exc() if exc else "No exception"
        prefix = f"[{context}]" if context else "[panic]"
        _log(f"[FATAL] {prefix} {exc.__class__.__name__}: {exc}")
        _log(f"[FATAL] {prefix} traceback:\n{tb_text}")

        # 写入 panic dump 文件
        try:
            from config import DATA_DIR
            dump_dir = DATA_DIR
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            dump_file = dump_dir / f"panic_{ts}.log"
            dump_file.write_text(
                f"[{datetime.now().isoformat()}] {prefix}\n"
                f"{exc.__class__.__name__}: {exc}\n\n{tb_text}",
                encoding="utf-8",
            )
        except Exception:
            pass  # panic 日志写入失败时绝对不能影响主流程

    @classmethod
    def register_shutdown_handler(cls, handler: Callable):
        """注册优雅关闭钩子"""
        cls._shutdown_handlers.append(handler)

    @classmethod
    def graceful_shutdown(cls):
        """优雅关闭：执行所有注册的关闭钩子"""
        _log("[SAFETY] graceful shutdown initiated")
        for handler in cls._shutdown_handlers:
            try:
                handler()
            except Exception as e:
                _log(f"[SAFETY] shutdown handler error: {e}")
        _log("[SAFETY] graceful shutdown complete")
