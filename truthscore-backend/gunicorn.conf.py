"""
TruthScore — Gunicorn production config
Usage: gunicorn -c gunicorn.conf.py main:app
"""
import os
import multiprocessing

# 2 workers per CPU core — good for async (IO-bound) FastAPI
workers = int(os.getenv("WEB_CONCURRENCY", max(2, multiprocessing.cpu_count() * 2)))
worker_class = "uvicorn.workers.UvicornWorker"

# Share ML models across workers via fork (avoids 500MB × N worker reload)
preload_app = True

# Network
bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"
timeout = 120          # LLM calls can take up to 60s
keepalive = 5
graceful_timeout = 30

# Logging
loglevel = "info"
accesslog = "-"
errorlog = "-"
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sµs'
