"""Portable process supervisor for the Windows Server (RDP) deployment.

Runs the whole stack without systemd/nginx:

    python scripts/run_stack.py                # everything available
    python scripts/run_stack.py --no-tunnel    # no cloudflared on this host

Components (each in its own child process, restarted on crash with backoff):
  * Redis          (only when REDIS_SERVER points at redis-server.exe)
  * web            waitress serving 127.0.0.1 (the tunnel is the only ingress)
  * worker         Celery worker; --pool=solo is required on Windows
  * beat           Celery beat; its schedule file lives in the data dir, never
                   in the repository
  * tunnel         cloudflared, when a config file exists

Press Ctrl+C to stop everything. Logs are written to ``<data dir>/logs``.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

BACKOFF_START = 5.0
BACKOFF_MAX = 60.0
POLL_SECONDS = 2.0


def data_dir() -> Path:
    configured = os.environ.get("WEBMAILER_DATA_DIR")
    if configured:
        base = Path(configured)
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "webmailer"
    else:
        base = Path.home() / ".local" / "share" / "webmailer"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _python(module: str, *args: str) -> list[str]:
    return [sys.executable, "-m", module, *args]


def component_commands(args, base: Path) -> dict[str, list[str]]:
    listen = args.listen
    commands: dict[str, list[str]] = {}

    redis_server = os.environ.get("REDIS_SERVER")
    if redis_server and not args.no_redis:
        commands["redis"] = [
            redis_server,
            "--port", os.environ.get("REDIS_PORT", "6379"),
            "--dir", str(base / "redis"),
            "--appendonly", "yes",
        ]

    # waitress uses getopt: the application must be the last argument.
    commands["web"] = _python(
        "waitress", "--call", f"--listen={listen}", "app:create_app"
    )

    queue = os.environ.get("CELERY_QUEUES", "celery,monitor")
    if not args.no_worker:
        commands["worker"] = _python(
            "celery",
            "-A", "app.workers.celery_app:celery",
            "worker",
            "--pool=solo",
            "--loglevel=info",
            "-Q", queue,
        )
    if not args.no_beat:
        commands["beat"] = _python(
            "celery",
            "-A", "app.workers.celery_app:celery",
            "beat",
            "--loglevel=info",
            "--schedule", str(base / "celerybeat-schedule"),
        )

    if not args.no_tunnel:
        cloudflared = os.environ.get("CLOUDFLARED") or "cloudflared"
        config = Path(
            os.environ.get("CLOUDFLARED_CONFIG", str(base / "cloudflared.yml"))
        )
        if config.exists():
            commands["tunnel"] = [
                cloudflared, "--config", str(config), "tunnel", "run"
            ]

    return commands


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default=os.environ.get(
        "WAITRESS_LISTEN", "127.0.0.1:5000"))
    parser.add_argument("--no-redis", action="store_true")
    parser.add_argument("--no-worker", action="store_true")
    parser.add_argument("--no-beat", action="store_true")
    parser.add_argument("--no-tunnel", action="store_true")
    args = parser.parse_args()

    base = data_dir()
    log_dir = base / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (base / "redis").mkdir(parents=True, exist_ok=True)

    commands = component_commands(args, base)
    if not commands:
        print("nothing to run", file=sys.stderr)
        return 1

    running: dict[str, subprocess.Popen] = {}
    handles: dict[str, object] = {}
    backoff: dict[str, float] = {name: BACKOFF_START for name in commands}
    stopping = False

    child_env = os.environ.copy()
    # Windows consoles default to cp1252, which cannot encode alert/emoji text
    # in logs; force UTF-8 for every child.
    child_env.setdefault("PYTHONUTF8", "1")
    child_env.setdefault("PYTHONIOENCODING", "utf-8")

    def start(name: str) -> None:
        log_path = log_dir / f"{name}.log"
        handle = open(log_path, "a", encoding="utf-8", buffering=1)
        handles[name] = handle
        handle.write(f"\n--- starting {name} at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        running[name] = subprocess.Popen(
            commands[name],
            cwd=Path(__file__).resolve().parent.parent,
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            env=child_env,
        )
        print(f"[stack] started {name} (pid {running[name].pid})", flush=True)

    def stop_all() -> None:
        for name, proc in list(running.items()):
            if proc.poll() is not None:
                continue
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        for handle in handles.values():
            try:
                handle.close()
            except OSError:
                pass

    def handle_signal(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    for name in commands:
        start(name)

    try:
        while not stopping:
            time.sleep(POLL_SECONDS)
            for name, proc in list(running.items()):
                code = proc.poll()
                if code is None:
                    continue
                print(f"[stack] {name} exited with {code}; restarting in "
                      f"{backoff[name]:.0f}s", flush=True)
                try:
                    handles[name].close()
                except OSError:
                    pass
                time.sleep(backoff[name])
                backoff[name] = min(BACKOFF_MAX, backoff[name] * 2)
                if not stopping:
                    start(name)
            for name, proc in running.items():
                if proc.poll() is None:
                    backoff[name] = BACKOFF_START
    finally:
        stop_all()
        print("[stack] stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
