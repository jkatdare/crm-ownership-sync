"""The review app end to end against the fake CRM: nothing writes without an approval from the UI."""
import re

import pytest

from conftest import PARENT_NAME
from ownership_sync.app import create_app
from ownership_sync.matcher import build_plan
from ownership_sync.store import Store

BELLHAVEN, LIMA = "0015QAPLGS3FVYEEEM", "001LGFPBJY4N9MB6KL"


@pytest.fixture
def client(tmp_path, locations, fake_crm):
    db = tmp_path / "state.db"
    store = Store(db)
    plan = build_plan(locations, fake_crm.list_accounts(), fake_crm.list_contacts(), PARENT_NAME)
    store.sync_proposals(store.start_run(), plan["proposals"])
    store.close()
    app = create_app(db_path=db, crm_factory=lambda: fake_crm)
    app.config["TESTING"] = True
    client = app.test_client()
    page = client.get("/").get_data(as_text=True)
    client.csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    client.db = db
    return client


def proposal_id(client, account_id):
    store = Store(client.db)
    try:
        return next(p["id"] for p in store.list(("pending", "applied", "rejected")) if p["account_id"] == account_id)
    finally:
        store.close()


def status_of(client, pid):
    store = Store(client.db)
    try:
        return store.get(pid)["status"]
    finally:
        store.close()


def test_queue_shows_every_pending_proposal_with_evidence(client):
    page = client.get("/").get_data(as_text=True)
    assert "29 proposal(s) awaiting a decision" in page
    assert "CHOW SOP check" in page and "Exact API call(s) on approval" in page
    assert client.get("/matches").status_code == 200 and client.get("/history").status_code == 200


def test_viewing_pages_writes_nothing(client, fake_crm):
    for url in ("/", "/?status=applied", "/matches", "/history"):
        client.get(url)
    assert fake_crm.writes == []


def test_post_without_the_form_token_is_refused(client, fake_crm):
    pid = proposal_id(client, LIMA)
    assert client.post(f"/proposals/{pid}/approve", data={}).status_code == 400
    assert fake_crm.writes == [] and status_of(client, pid) == "pending"


def test_approve_writes_to_the_crm_and_records_the_decision(client, fake_crm):
    pid = proposal_id(client, LIMA)
    response = client.post(f"/proposals/{pid}/approve", data={"csrf": client.csrf, "note": "confirmed on site"}, follow_redirects=True)
    assert "Approved" in response.get_data(as_text=True)
    assert fake_crm.get_account(LIMA)["parent_id"] == BELLHAVEN
    assert status_of(client, pid) == "applied"
    assert "confirmed on site" in client.get("/history").get_data(as_text=True)


def test_reject_writes_nothing_and_sticks(client, fake_crm):
    pid = proposal_id(client, LIMA)
    client.post(f"/proposals/{pid}/reject", data={"csrf": client.csrf, "note": "leave it"})
    assert fake_crm.writes == [] and status_of(client, pid) == "rejected"
    # approving a rejected proposal is not possible
    client.post(f"/proposals/{pid}/approve", data={"csrf": client.csrf})
    assert fake_crm.writes == [] and status_of(client, pid) == "rejected"


def test_approving_twice_writes_once(client, fake_crm):
    pid = proposal_id(client, LIMA)
    for _ in range(2):
        client.post(f"/proposals/{pid}/approve", data={"csrf": client.csrf})
    assert len(fake_crm.writes) == 1


def test_stale_proposal_is_closed_instead_of_written(client, fake_crm):
    pid = proposal_id(client, LIMA)
    fake_crm.update_account(LIMA, {"phone": "(555) 000-0000"})
    fake_crm.writes.clear()
    response = client.post(f"/proposals/{pid}/approve", data={"csrf": client.csrf}, follow_redirects=True)
    assert "Nothing written" in response.get_data(as_text=True)
    assert fake_crm.writes == [] and status_of(client, pid) == "obsolete"
