"""SMS settings API: defaults, updates, plan guard, live credits."""
from __future__ import annotations


def test_get_sms_settings_returns_defaults(client, supabase):
    resp = client.get("/api/sms/settings")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["settings"]["sms_plan"] == "trial"
    assert body["settings"]["default_sms_type"] == "transactional"
    assert body["plan"]["plan"] == "trial"


def test_patch_sms_settings_persists(client, supabase):
    resp = client.patch("/api/sms/settings", json={
        "sms_sender": "MyBrand",
        "send_rate_per_minute": 45,
        "max_recipients_per_hour": 100,
        "max_recipients_per_day": 1000,
    })
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["settings"]["sms_sender"] == "MyBrand"
    assert supabase.sms_settings_row["sms_sender"] == "MyBrand"


def test_patch_rejects_value_above_plan_limit(client, supabase):
    resp = client.patch("/api/sms/settings", json={"send_rate_per_minute": 100})
    assert resp.status_code == 422
    assert resp.get_json()["error"] == "settings rejected"


def test_patch_rejects_unknown_plan(client, supabase):
    resp = client.patch("/api/sms/settings", json={"sms_plan": "mega"})
    assert resp.status_code == 422


def test_patch_warns_above_recommended(client, supabase):
    resp = client.patch("/api/sms/settings", json={"send_rate_per_minute": 45})
    assert resp.status_code == 200
    assert resp.get_json()["warnings"]


def test_patch_switching_to_bulk_allows_higher_rate(client, supabase):
    resp = client.patch("/api/sms/settings", json={"sms_plan": "bulk"})
    assert resp.status_code == 200
    assert resp.get_json()["plan"]["plan"] == "bulk"


def test_list_sms_plans(client):
    resp = client.get("/api/sms/settings/plans")
    assert resp.status_code == 200
    plans = resp.get_json()["plans"]
    assert set(plans) == {"trial", "standard", "bulk"}


def test_sms_defaults_endpoint(client):
    resp = client.get("/api/sms/settings/defaults")
    assert resp.status_code == 200
    assert resp.get_json()["defaults"]["sms_plan"] == "trial"


def test_account_endpoint_reports_credits(client, monkeypatch):
    class FakeSmsClient:
        def get_account_sms_credits(self):
            return {"type": "sms", "credits": 12, "credits_type": "sms_credits"}

    monkeypatch.setattr("app.core.sms.get_sms_client", lambda: FakeSmsClient())
    resp = client.get("/api/sms/account")
    assert resp.status_code == 200
    account = resp.get_json()["account"]
    assert account["credits"] == 12
    assert account["low"] is True
