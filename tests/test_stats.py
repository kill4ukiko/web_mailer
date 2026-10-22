"""Atomic campaign stat increments (RPC with a safe legacy fallback)."""
from __future__ import annotations

import pytest

from app.core.supabase_client import SupabaseWrapper


class _RpcStub:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        fail = self.fail

        class _Resp:
            def execute(self_inner):
                if fail:
                    raise RuntimeError("function does not exist")
                return type("R", (), {"data": 1})()

        return _Resp()


def _wrapper(client):
    wrapper = SupabaseWrapper.__new__(SupabaseWrapper)
    wrapper.client = client
    return wrapper


def test_increment_stats_uses_atomic_rpc():
    stub = _RpcStub()
    _wrapper(stub).increment_stats("c1", "opens", delta=2)
    assert stub.calls == [
        (
            "increment_campaign_stat",
            {"p_campaign_id": "c1", "p_field": "opens", "p_delta": 2},
        )
    ]


def test_increment_stats_rejects_unknown_field():
    with pytest.raises(ValueError):
        _wrapper(_RpcStub()).increment_stats("c1", "drop table")


def test_increment_stats_falls_back_when_rpc_missing(monkeypatch):
    stub = _RpcStub(fail=True)
    wrapper = _wrapper(stub)
    monkeypatch.setattr(
        wrapper, "get_campaign", lambda _cid: {"stats": {"bounces": 2}}
    )
    written: list[dict] = []

    class _Table:
        def update(self, payload):
            written.append(payload)
            return self

        def eq(self, *_a):
            return self

        def execute(self):
            return type("R", (), {"data": []})()

    client = type("C", (), {})()
    client.rpc = stub.rpc
    client.table = lambda _name: _Table()
    monkeypatch.setattr(wrapper, "client", client)

    wrapper.increment_stats("c1", "bounces")

    assert written == [{"stats": {"bounces": 3}}]
