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


def set_field(client, pid, column, value):
    store = Store(client.db)
    with store.db:
        store.db.execute(f"UPDATE proposals SET {column} = ? WHERE id = ?", (value, pid))
    store.close()


def test_overlapping_double_click_on_a_create_makes_one_account(client, fake_crm):
    import threading
    import time

    slow_create = fake_crm.create_account

    def create_slowly(fields):  # the first click is still talking to the CRM when the second arrives
        time.sleep(0.3)
        return slow_create(fields)

    fake_crm.create_account = create_slowly
    store = Store(client.db)
    pid = next(p["id"] for p in store.list(("pending",)) if p["location_slug"] == "amberly-manor")
    store.close()
    app = client.application

    def click():
        app.test_client().post(f"/proposals/{pid}/approve", data={"csrf": client.csrf})

    threads = [threading.Thread(target=click) for _ in range(2)]
    threads[0].start()
    time.sleep(0.1)
    threads[1].start()
    for t in threads:
        t.join()
    assert [w["method"] for w in fake_crm.writes] == ["POST"]
    assert len([a for a in fake_crm.list_accounts() if a["name"] == "Amberly Manor" and a["billing_state"] == "OH"]) == 1
    assert status_of(client, pid) == "applied"


def test_approval_interrupted_by_a_crash_becomes_retryable_when_the_app_restarts(client, fake_crm):
    pid = proposal_id(client, LIMA)
    set_field(client, pid, "status", "applying")  # the process died mid-approval
    restarted = create_app(db_path=client.db, crm_factory=lambda: fake_crm).test_client()
    assert status_of(client, pid) == "failed"
    page = restarted.get("/?status=failed").get_data(as_text=True)
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    restarted.post(f"/proposals/{pid}/approve", data={"csrf": token})
    assert status_of(client, pid) == "applied" and fake_crm.get_account(LIMA)["parent_id"] == BELLHAVEN


def test_missing_token_is_reported_and_the_proposal_stays_pending(tmp_path, client):
    from ownership_sync.crm import CrmError

    def no_client():
        raise CrmError("CRM_API_TOKEN is not set")

    broken = create_app(db_path=client.db, crm_factory=no_client).test_client()
    page = broken.get("/").get_data(as_text=True)
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    pid = proposal_id(client, LIMA)
    response = broken.post(f"/proposals/{pid}/approve", data={"csrf": token}, follow_redirects=True)
    assert response.status_code == 200 and "CRM_API_TOKEN is not set" in response.get_data(as_text=True)
    assert status_of(client, pid) == "pending"


def test_requests_under_a_foreign_hostname_are_refused(client, fake_crm):
    assert client.get("/", headers={"Host": "attacker.example:5000"}).status_code == 400
    pid = proposal_id(client, LIMA)
    response = client.post(f"/proposals/{pid}/approve", data={"csrf": client.csrf}, headers={"Host": "attacker.example:5000"})
    assert response.status_code == 400 and fake_crm.writes == []


def test_queue_survives_an_unknown_category_and_shows_request_bodies_verbatim(client):
    set_field(client, proposal_id(client, LIMA), "category", "something_new")
    response = client.get("/")
    page = response.get_data(as_text=True)
    assert response.status_code == 200 and "something_new" in page
    # request bodies are shown as they are sent, not as JSON unicode escapes (u0026 for the ampersand, u003c for <)
    assert "Rehabilitation &amp; Nursing of Grove City" in page and "u0026" not in page and "u003c" not in page


def test_approving_from_a_page_that_a_later_run_has_refreshed_writes_nothing(client, fake_crm, locations):
    pid = proposal_id(client, LIMA)
    page_version = Store(client.db).get(pid)["last_run_id"]  # what the open page carries in its form
    store = Store(client.db)  # a scheduled run refreshes the pending proposal in the background
    store.sync_proposals(store.start_run(), build_plan(locations, fake_crm.list_accounts(), fake_crm.list_contacts(), PARENT_NAME)["proposals"])
    store.close()

    stale = client.post(f"/proposals/{pid}/approve", data={"csrf": client.csrf, "seen": page_version}, follow_redirects=True)
    assert "review it again" in stale.get_data(as_text=True)
    assert fake_crm.writes == [] and status_of(client, pid) == "pending"

    fresh = Store(client.db).get(pid)["last_run_id"]
    client.post(f"/proposals/{pid}/approve", data={"csrf": client.csrf, "seen": fresh})
    assert status_of(client, pid) == "applied" and len(fake_crm.writes) == 1


def test_demo_mode_reads_and_writes_only_the_in_memory_copy(tmp_path, locations, fake_crm):
    db = tmp_path / "demo.db"
    store = Store(db)
    store.sync_proposals(store.start_run(), build_plan(locations, fake_crm.list_accounts(), fake_crm.list_contacts(), PARENT_NAME)["proposals"])
    store.close()
    demo = create_app(db_path=db, demo_crm=fake_crm).test_client()
    page = demo.get("/").get_data(as_text=True)
    assert "DEMO MODE" in page and "/demo/accounts/" in page and "/crm/" not in page  # no link to the real CRM
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    pid = next(p["id"] for p in Store(db).list(("pending",)) if p["account_id"] == LIMA)
    demo.post(f"/proposals/{pid}/approve", data={"csrf": token})
    assert fake_crm.get_account(LIMA)["parent_id"] == BELLHAVEN  # landed in the copy
    record = demo.get(f"/demo/accounts/{LIMA}").get_data(as_text=True)
    assert "Bellhaven Senior Living (Parent Account)" in record and "parent was Harborview Care Group" in record


def test_the_demo_account_page_does_not_exist_outside_demo_mode(client):
    assert client.get(f"/demo/accounts/{LIMA}").status_code == 404
    assert "DEMO MODE" not in client.get("/").get_data(as_text=True)
