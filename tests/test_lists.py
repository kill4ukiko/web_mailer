"""Contact-list (reusable groups, many-to-many) tests."""
from __future__ import annotations


def _contact(supabase, email):
    return supabase.create_contact({"email": email})[0]


def _make_campaign(supabase, status="draft"):
    camp = supabase.create_campaign({
        "name": "C", "subject": "S", "from_email": "n@yourdomain.com",
        "from_name": "N", "html_content": "<p>hi</p>",
    })
    camp["status"] = status
    return camp


def test_create_list_with_emails(client, supabase):
    _contact(supabase, "a@example.com")
    resp = client.post("/api/lists", json={"name": "Leads", "emails": ["a@example.com"]})
    assert resp.status_code == 201
    entry = resp.get_json()["list"]
    assert entry["member_count"] == 1

    detail = client.get(f"/api/lists/{entry['id']}").get_json()
    assert detail["members"][0]["email"] == "a@example.com"


def test_create_list_creates_unknown_emails(client, supabase):
    resp = client.post("/api/lists", json={"name": "Fresh", "emails": ["ghost@example.com"]})
    assert resp.status_code == 201
    assert resp.get_json()["list"]["member_count"] == 1
    assert supabase._contact_by_email("ghost@example.com") is not None


def test_add_and_remove_member(client, supabase):
    contact = _contact(supabase, "b@example.com")
    lst = client.post("/api/lists", json={"name": "L"}).get_json()["list"]

    add = client.post(f"/api/lists/{lst['id']}/members", json={"contact_ids": [contact["id"]]})
    assert add.status_code == 200
    assert add.get_json()["added"] == 1
    assert len(add.get_json()["members"]) == 1

    rm = client.delete(f"/api/lists/{lst['id']}/members/{contact['id']}")
    assert rm.status_code == 200
    assert client.get(f"/api/lists/{lst['id']}").get_json()["members"] == []


def test_delete_list_cascades_memberships(client, supabase):
    contact = _contact(supabase, "c@example.com")
    lst = client.post("/api/lists", json={"name": "Temp"}).get_json()["list"]
    client.post(f"/api/lists/{lst['id']}/members", json={"contact_ids": [contact["id"]]})

    resp = client.delete(f"/api/lists/{lst['id']}")
    assert resp.status_code == 200
    assert supabase.list_members(lst["id"]) == []
    assert client.get("/api/lists").get_json()["items"] == []


def test_campaign_create_attaches_list_recipients(client, supabase):
    _contact(supabase, "a@example.com")
    _contact(supabase, "b@example.com")
    supabase.create_contact({"email": "skip@example.com"})
    lst = client.post("/api/lists", json={
        "name": "Leads", "emails": ["a@example.com", "b@example.com"],
    }).get_json()["list"]

    resp = client.post("/api/campaigns", json={
        "name": "C", "subject": "S", "from_email": "n@yourdomain.com",
        "from_name": "N", "html_content": "<p>hi</p>", "list_ids": [lst["id"]],
    })
    assert resp.status_code == 201
    cid = resp.get_json()["campaign"]["id"]
    assert supabase.count_recipients(cid) == 2
    assert [entry["id"] for entry in supabase.list_campaign_lists(cid)] == [lst["id"]]


def test_set_campaign_lists_replaces_and_attaches(client, supabase):
    campaign = _make_campaign(supabase)
    ca = _contact(supabase, "a@example.com")
    cb = _contact(supabase, "b@example.com")
    l1 = client.post("/api/lists", json={"name": "L1"}).get_json()["list"]
    l2 = client.post("/api/lists", json={"name": "L2"}).get_json()["list"]
    client.post(f"/api/lists/{l1['id']}/members", json={"contact_ids": [ca["id"]]})
    client.post(f"/api/lists/{l2['id']}/members", json={"contact_ids": [cb["id"]]})

    resp = client.post(f"/api/campaigns/{campaign['id']}/lists",
                       json={"list_ids": [l1["id"], l2["id"]]})
    assert resp.status_code == 200
    assert resp.get_json()["attached"] == 2
    assert supabase.count_recipients(campaign["id"]) == 2

    # Replacing with a single list drops the other from the association.
    resp = client.post(f"/api/campaigns/{campaign['id']}/lists",
                       json={"list_ids": [l1["id"]], "attach": False})
    assert resp.status_code == 200
    assert [entry["id"] for entry in supabase.list_campaign_lists(campaign["id"])] == [l1["id"]]


def test_campaign_deduplicates_overlapping_lists(client, supabase):
    _contact(supabase, "shared@example.com")
    l1 = client.post("/api/lists", json={"name": "L1", "emails": ["shared@example.com"]}).get_json()["list"]
    l2 = client.post("/api/lists", json={"name": "L2", "emails": ["shared@example.com"]}).get_json()["list"]

    resp = client.post("/api/campaigns", json={
        "name": "C", "subject": "S", "from_email": "n@yourdomain.com",
        "from_name": "N", "html_content": "<p>hi</p>", "list_ids": [l1["id"], l2["id"]],
    })
    cid = resp.get_json()["campaign"]["id"]
    assert supabase.count_recipients(cid) == 1
