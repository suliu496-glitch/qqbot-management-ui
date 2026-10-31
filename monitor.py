#!/usr/bin/env python3
"""
monitor.py — 运行时指标收集 + 告警

环形缓冲区记录关键指标：
- AI 回复耗时、工具执行耗时、错误率、消息吞吐量

不引入重度依赖（无 Prometheus/Datadog），纯 stdlib 实现。
"""
import time
import threading
from typing import Optional

# ==================== 错误计数（效果四）====================
_error_counts: dict[str, int] = {}
_error_lock = threading.Lock()


def count_error(module: str) -> None:
    """记录某模块错误。每累积 10 次触发一次告警日志。"""
    with _error_lock:
        _error_counts[module] = _error_counts.get(module, 0) + 1
        count = _error_counts[module]
    if count % 10 == 0:
        _log(f"[ERROR] {module} 已累计出错 {count} 次")


def get_error_counts() -> dict[str, int]:
    """获取各模块错误计数副本。"""
    with _error_lock:
        return dict(_error_counts)

try:
    from messaging import log as _log
except Exception:
    def _log(msg):
        print(f"[qq-bot] {msg}", flush=True)


class Monitor:
    """指标收集 + 告警（环形缓冲区，保留 1000 条）"""

    _metrics: dict[str, list[float]] = {
        "ai_response_time_ms": [],
        "tool_execution_time_ms": [],
        "error_count": [],
        "message_in_rate": [],
        "message_out_rate": [],
    }
    _max_samples = 1000
    _lock = threading.Lock()
    # 告警状态：{metric: last_alert_time}
    _last_alert: dict[str, float] = {}

    # 告警阈值
    THRESHOLDS = {
        "ai_response_time_ms": {"p95": 10000},   # AI 回复 p95 >10s → 告警
        "error_count": {"count_per_min": 10},      # 每分钟错误 >10 → 告警
    }

    @classmethod
    def record(cls, metric: str, value: float):
        """记录指标（环形缓冲区，超量时丢弃最旧）"""
        with cls._lock:
            buf = cls._metrics.setdefault(metric, [])
            buf.append(value)
            if len(buf) > cls._max_samples:
                buf.pop(0)

    @classmethod
    def record_duration(cls, metric: str):
        """上下文管理器：记录一段代码的耗时（毫秒）"""
        class _Timer:
            def __enter__(self_inner):
                self_inner.start = time.time()
                return self_inner
            def __exit__(self_inner, *exc):
                elapsed_ms = (time.time() - self_inner.start) * 1000
                cls.record(metric, elapsed_ms)
        return _Timer()

    @classmethod
    def report(cls) -> dict:
        """生成健康报告：p50/p95/p99 耗时、错误率、吞吐量"""
        import statistics
        with cls._lock:
            report = {}
            for metric, values in cls._metrics.items():
                if not values:
                    report[metric] = {"count": 0}
                    continue
                sorted_v = sorted(values)
                n = len(sorted_v)
                report[metric] = {
                    "count": n,
                    "p50": sorted_v[n // 2],
                    "p95": sorted_v[int(n * 0.95)] if n > 1 else sorted_v[0],
                    "p99": sorted_v[int(n * 0.99)] if n > 1 else sorted_v[0],
                    "avg": sum(sorted_v) / n,
                }
            return report

    @classmethod
    def alert_if_needed(cls) -> Optional[str]:
        """检查告警阈值。返回告警消息或 None（每小时同一告警最多一次）。"""
        now = time.time()
        alerts = []

        # AI 回复耗时 p95
        ai_times = cls._metrics.get("ai_response_time_ms", [])
        if len(ai_times) >= 10:
            sorted_t = sorted(ai_times)
            p95 = sorted_t[int(len(sorted_t) * 0.95)]
            threshold = cls.THRESHOLDS["ai_response_time_ms"]["p95"]
            if p95 > threshold:
                key = "ai_p95"
                if now - cls._last_alert.get(key, 0) > 3600:
                    cls._last_alert[key] = now
                    alerts.append(f"AI 回复 p95={p95:.0f}ms > {threshold}ms")

        if alerts:
            return "⚠️ 监控告警: " + "; ".join(alerts)
        return None
