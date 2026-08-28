"""
TruthScore -- Observability: structured logging, correlation IDs, metrics.

- setup_logging(): swaps the root handler for a JSON-line formatter so logs are
  machine-parseable in prod (Cloud Logging / Loki / Datadog) while staying
  readable in dev. Every record carries the current request's correlation id.
- request_id_ctx: a ContextVar the HTTP middleware sets per request; the log
  formatter reads it so any log line emitted while handling a request is
  automatically tagged with that request's id — no threading it through calls.
- METRICS: a tiny in-process counter set (requests, errors, in-flight, latency,
  per-status) exposed at /metrics. No external deps; resets on restart.
"""
import os, sys, json, time, logging
from contextvars import ContextVar

# Correlation id for the in-flight request ("-" when outside a request).
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


class _JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg, request_id (+exc)."""
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_ctx.get(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging() -> None:
    """Install the JSON formatter on the root logger. LOG_FORMAT=plain keeps
    the human formatter (default in dev); LOG_LEVEL sets the threshold."""
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    if os.getenv("LOG_FORMAT", "json").lower() == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    # Replace any pre-existing handlers so we don't double-log.
    root.handlers = [handler]


class _Metrics:
    """In-process request metrics. Cheap, lock-free (single event loop), and
    good enough for a /metrics scrape or a health dashboard. Not persisted."""
    def __init__(self):
        self.started_at = time.time()
        self.requests = 0
        self.errors = 0            # 5xx responses + unhandled exceptions
        self.in_flight = 0
        self.latency_sum_ms = 0.0
        self.by_status: dict = {}

    def start(self):
        self.requests += 1
        self.in_flight += 1

    def finish(self, status: int, elapsed_ms: float):
        self.in_flight = max(0, self.in_flight - 1)
        self.latency_sum_ms += elapsed_ms
        bucket = f"{status // 100}xx"
        self.by_status[bucket] = self.by_status.get(bucket, 0) + 1
        if status >= 500:
            self.errors += 1

    def snapshot(self) -> dict:
        n = max(self.requests, 1)
        return {
            "uptime_s": round(time.time() - self.started_at, 1),
            "requests": self.requests,
            "errors": self.errors,
            "in_flight": self.in_flight,
            "avg_latency_ms": round(self.latency_sum_ms / n, 1),
            "by_status": dict(self.by_status),
        }


METRICS = _Metrics()
