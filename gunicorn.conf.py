"""Gunicorn production server configuration (Linux / Heroku)."""
import os

# Heroku injects $PORT and requires binding to it. GUNICORN_BIND wins when set.
port = os.environ.get("PORT")
bind = os.environ.get("GUNICORN_BIND") or (
    f"0.0.0.0:{port}" if port else "0.0.0.0:5000"
)
workers = int(os.environ.get("GUNICORN_WORKERS", "4"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
# gthread lets long-ish tracking/webhook requests overlap without more workers.
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "gthread")
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "60"))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "60"))
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", "1000"))
max_requests_jitter = int(os.environ.get("GUNICORN_MAX_REQUESTS_JITTER", "100"))
accesslog = os.environ.get("GUNICORN_ACCESSLOG", "-")
errorlog = os.environ.get("GUNICORN_ERRORLOG", "-")
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")
