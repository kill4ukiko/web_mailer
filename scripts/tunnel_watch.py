"""Supervise the Cloudflare tunnel: keep cloudflared alive + Telegram alerts.

``run-local.bat`` launches this script instead of ``cloudflared`` directly. It:

  * (re)starts ``cloudflared tunnel ... run`` whenever the process exits, with
    exponential backoff so a crash-loop can't hammer Cloudflare;
  * probes the public health URL and alerts on up / down transitions;
  * appends cloudflared's output to ``.tools/cloudflared/tunnel.log``;
  * sends Telegram alerts on start / down / recovery / stop.

Run it directly (the launcher does)::

    python scripts/tunnel_watch.py
    python scripts/tunnel_watch.py --health https://track.example.com/api/health
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Running `python scripts\tunnel_watch.py` puts scripts\ on sys.path, so the
# repo root must be added explicitly for `import app...` to resolve.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_HEALTH_URL = os.environ.get(
    "TUNNEL_HEALTH_URL", "https://track.goprilad.com/api/health"
)

logger = logging.getLogger("tunnel_watch")


def _cloudflared() -> Path:
    return ROOT / ".tools" / "cloudflared" / "cloudflared.exe"


def _config() -> Path:
    return ROOT / ".tools" / "cloudflared" / "config.yml"


def _log_path() -> Path:
    return ROOT / ".tools" / "cloudflared" / "tunnel.log"


# Substrings in cloudflared's output that map to an actionable fix.
_HINTS = (
    ("credentials file", "run setup-local.bat to (re)create the tunnel credentials"),
    ("couldn't find a valid tunnel", "run setup-local.bat to set up the tunnel"),
    ("no ingress rules", "run setup-local.bat to (re)write config.yml"),
)


def _tail(max_lines: int = 12) -> list[str]:
    """Return the last non-empty lines of cloudflared's log (best effort)."""
    try:
        text = _log_path().read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return []
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-max_lines:]


def _diagnose(lines: list[str]) -> str | None:
    """Map a known cloudflared failure to a one-line fix hint."""
    blob = "\n".join(lines).lower()
    for marker, hint in _HINTS:
        if marker in blob:
            return hint
    return None


def _notify(title: str, lines: list[str] | None = None, level: str = "info") -> bool:
    """Best-effort Telegram alert (never raises, never blocks the tunnel)."""
    try:
        from app.core.notifications import notify

        return notify(title, lines, level)
    except Exception as exc:  # noqa: BLE001
        logger.warning("notify failed: %s", exc)
        return False


def _probe(url: str, timeout: float = 10.0) -> bool:
    """Return True when ``url`` answers with a non-error HTTP status."""
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= int(getattr(resp, "status", 0)) < 400
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False


def _transition(previous: bool | None, current: bool) -> str | None:
    """Classify a health transition.

    Returns ``"up"`` on the first successful probe, ``"recovered"`` after a
    failure, ``"down"`` on a failure, or ``None`` when nothing changed.
    """
    if previous is None:
        return "up" if current else None
    if current == previous:
        return None
    return "recovered" if current else "down"


def _start_cloudflared(log_file) -> subprocess.Popen:
    # NOTE: --config/--no-autoupdate/--loglevel are *tunnel* options, so they
    # must come before the `run` subcommand (cloudflared rejects them after it).
    cmd = [
        str(_cloudflared()),
        "tunnel",
        "--config",
        str(_config()),
        "--no-autoupdate",
        "--loglevel",
        "info",
        "run",
    ]
    return subprocess.Popen(
        cmd, cwd=str(ROOT), stdout=log_file, stderr=subprocess.STDOUT
    )


def watch(
    health_url: str = DEFAULT_HEALTH_URL,
    *,
    probe_interval: float = 30.0,
    poll_interval: float = 5.0,
    max_backoff: float = 60.0,
) -> int:
    """Run the supervision loop until interrupted. Returns a process exit code."""
    exe = _cloudflared()
    if not exe.exists():
        logger.error("cloudflared not found at %s (run setup-local.bat)", exe)
        _notify("Tunnel cannot start", [f"cloudflared missing: {exe}"], "error")
        return 1

    log_path = _log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _notify("Tunnel starting", [f"Health probe: {health_url}"], "info")

    backoff = 1.0
    healthy: bool | None = None
    last_probe = 0.0
    proc: subprocess.Popen | None = None
    try:
        while True:
            with log_path.open("a", encoding="utf-8") as log_file:
                proc = _start_cloudflared(log_file)
                logger.info("cloudflared started (pid=%s)", proc.pid)
                started = time.monotonic()
                last_probe = 0.0
                while proc.poll() is None:
                    time.sleep(poll_interval)
                    now = time.monotonic()
                    if now - last_probe < probe_interval and last_probe:
                        continue
                    last_probe = now
                    up = _probe(health_url)
                    event = _transition(healthy, up)
                    healthy = up
                    if event == "up":
                        _notify("Tunnel up", [health_url], "success")
                    elif event == "recovered":
                        _notify("Tunnel recovered", [health_url], "success")
                    elif event == "down":
                        _notify("Tunnel down", [f"Probe failed: {health_url}"], "error")

                code = proc.returncode
                uptime = time.monotonic() - started
                tail = _tail()
                hint = _diagnose(tail)
                logger.warning("cloudflared exited (code=%s) after %.0fs", code, uptime)
                if tail:
                    logger.warning("last cloudflared output:\n%s", "\n".join(tail))
                if hint:
                    logger.warning("hint: %s", hint)

            if healthy is not False:
                alert = [f"cloudflared exited (code {code}); restarting in ~{backoff:.0f}s"]
                if hint:
                    alert.append(f"hint: {hint}")
                if tail:
                    alert.extend(tail[-4:])
                _notify("Tunnel down", alert, "error")
                healthy = False

            # Reset the backoff once the process managed to stay up a while.
            backoff = 1.0 if uptime >= 60 else min(max_backoff, backoff * 2)
            time.sleep(backoff)
    except KeyboardInterrupt:
        if proc is not None and proc.poll() is None:
            logger.info("stopping cloudflared (pid=%s)", proc.pid)
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        _notify("Tunnel stopped", ["Supervisor interrupted; stopping cloudflared."], "warning")
        return 0


def main() -> int:
    os.chdir(ROOT)  # so .env and relative tool paths resolve
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Cloudflare tunnel supervisor")
    parser.add_argument("--health", default=DEFAULT_HEALTH_URL)
    parser.add_argument("--probe-interval", type=float, default=30.0)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    args = parser.parse_args()
    return watch(
        health_url=args.health,
        probe_interval=args.probe_interval,
        poll_interval=args.poll_interval,
    )


if __name__ == "__main__":
    raise SystemExit(main())
