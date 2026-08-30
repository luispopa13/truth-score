"""
TruthScore — Gunicorn production config
Usage: gunicorn -c gunicorn.conf.py main:app
"""
import os
import multiprocessing

# ── Worker count ────────────────────────────────────────────────
# Each worker loads the full ML stack (~640MB of torch/sentence-transformer
# models), so a naive "2 × CPU" (previously the default, e.g. 16 on an 8-core
# box) OOM-kills the container. Default to a SANE small number and let the
# operator scale up explicitly via env once they've sized memory.
#
# HEAVY ML NOTE: for real horizontal scale, do NOT load models per worker —
# route embedding/reranking to the shared ranking_service.py sidecar (wired
# separately) so N web workers share ONE model process instead of N copies.
_cpu = multiprocessing.cpu_count() or 1
_default_workers = min(4, _cpu)
workers = int(
    os.getenv("WEB_CONCURRENCY", os.getenv("GUNICORN_WORKERS", str(_default_workers)))
)
worker_class = "uvicorn.workers.UvicornWorker"

# ── Memory: share models via copy-on-write ──────────────────────
# preload_app imports the app ONCE in the master, then forks workers. The torch
# model weights loaded at import are read-only after load, so Linux copy-on-write
# keeps them in shared physical pages across all workers instead of duplicating
# ~640MB per worker. This is the single biggest lever against OOM here.
# (If a model were (re)loaded lazily per-request inside a worker, COW wouldn't
# help — hence the sidecar note above for the real fix.)
preload_app = True

# ── Network ─────────────────────────────────────────────────────
# Host/port from env, never hardcoded. Default 0.0.0.0:8000.
_host = os.getenv("HOST", "0.0.0.0")
_port = os.getenv("PORT", "8000")
bind = f"{_host}:{_port}"
timeout = 120          # LLM calls can take up to 60s
keepalive = 5
graceful_timeout = 30

# ── Scheduler leader election (see utils/redis_client.should_run_scheduler) ──
# Background schedulers (digests, news scanner) must run in exactly ONE worker,
# not once per worker. When Redis is unavailable, should_run_scheduler() falls
# back to "am I worker 0?" — so stamp each forked worker with a stable id here.
def post_fork(server, worker):
    # worker.age is 1 for the first worker spawned, 2 for the next, ... — map it
    # to a 0-based id so exactly one worker sees GUNICORN_WORKER_ID == "0".
    os.environ["GUNICORN_WORKER_ID"] = str(max(0, worker.age - 1))

# ── Logging ─────────────────────────────────────────────────────
loglevel = "info"
accesslog = "-"
errorlog = "-"
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sµs'
