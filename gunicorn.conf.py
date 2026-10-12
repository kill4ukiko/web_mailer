"""Gunicorn production server configuration."""
import os

bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:5000")
workers = int(os.environ.get("GUNICORN_WORKERS", "4"))
threads = int(os.environ.get("GUNICORN_THREADS", "2"))
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "sync")
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "60"))
graceful_timeout = os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "60")
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", "1000"))
max_requests_jitter = int(os.environ.get("GUNICORN_MAX_REQUESTS_JITTER", "100"))
accesslog = os.environ.get("GUNICORN_ACCESSLOG", "-")
errorlog = os.environ.get("GUNICORN_ERRORLOG", "-")
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")