"""SMS lists API: creation, phone members, removal."""
from __future__ import annotations


def test_create_list_with_phones(client, supabase):
    resp = client.post("/api/sms/lists", json={
        "name": "VIP",
        "phones": ["+15551234567", "+15559999999"],
    })
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    assert body["list"]["member_count"] == 2
    assert len(supabase.sms_list_memberships) == 2
    assert len(supabase.sms_contacts) == 2


def test_create_list_reports_invalid_phones(client, supabase):
    resp = client.post("/api/sms/lists", json={
        "name": "Mixed", "phones": ["+15551234567", "junk"],
    })
    assert resp.status_code == 201
    assert resp.get_json()["invalid"] == ["junk"]


def test_list_detail_and_members(client, supabase):
    created = client.post("/api/sms/lists", json={"name": "VIP"}).get_json()["list"]
    list_id = created["id"]
    client.post(f"/api/sms/lists/{list_id}/members", json={"phones": ["+15551234567"]})

    detail = client.get(f"/api/sms/lists/{list_id}").get_json()
    assert detail["name"] == "VIP"
    assert detail["members"][0]["phone"] == "+15551234567"


def test_remove_member(client, supabase):
    list_id = client.post("/api/sms/lists", json={"name": "VIP"}).get_json()["list"]["id"]
    added = client.post(
        f"/api/sms/lists/{list_id}/members", json={"phones": ["+15551234567"]}
    ).get_json()
    contact_id = added["members"][0]["id"]

    resp = client.delete(f"/api/sms/lists/{list_id}/members/{contact_id}")
    assert resp.status_code == 200
    assert supabase.sms_list_memberships == []


def test_update_and_delete_list(client, supabase):
    list_id = client.post("/api/sms/lists", json={"name": "Old"}).get_json()["list"]["id"]

    resp = client.patch(f"/api/sms/lists/{list_id}", json={"name": "New"})
    assert resp.status_code == 200
    assert resp.get_json()["list"]["name"] == "New"

    assert client.delete(f"/api/sms/lists/{list_id}").status_code == 200
    assert supabase.sms_lists == []


def test_missing_list_returns_404(client, supabase):
    assert client.get("/api/sms/lists/nope").status_code == 404
    assert client.post(
        "/api/sms/lists/nope/members", json={"phones": ["+15551234567"]}
    ).status_code == 404
