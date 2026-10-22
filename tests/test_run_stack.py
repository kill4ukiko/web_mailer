"""Windows supervisor command construction (no processes are started)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts import run_stack


def _args(**overrides):
    base = dict(
        listen="127.0.0.1:5000",
        no_redis=True,
        no_worker=True,
        no_beat=True,
        no_tunnel=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_web_command_puts_application_last(tmp_path):
    commands = run_stack.component_commands(_args(), tmp_path)

    web = commands["web"]
    assert "--call" in web
    assert "--listen=127.0.0.1:5000" in web
    assert web[-1] == "app:create_app"


def test_worker_uses_solo_pool(tmp_path):
    commands = run_stack.component_commands(_args(no_worker=False), tmp_path)

    worker = commands["worker"]
    assert "--pool=solo" in worker
    assert "celery,monitor" in worker


def test_tunnel_only_when_config_exists(tmp_path):
    assert "tunnel" not in run_stack.component_commands(
        _args(no_tunnel=False), tmp_path
    )

    (tmp_path / "cloudflared.yml").write_text("tunnel: x\n", encoding="utf-8")
    commands = run_stack.component_commands(_args(no_tunnel=False), tmp_path)
    assert commands["tunnel"][-1] == "run"
