"""Re-run safety: decided items are never proposed again, and an approved queue converges to nothing."""
from collections import Counter

from conftest import PARENT_NAME
from ownership_sync.apply import apply_proposal
from ownership_sync.matcher import build_plan
from ownership_sync.store import Store

BELLHAVEN = "0015QAPLGS3FVYEEEM"


def plan(locations, crm):
    return build_plan(locations, crm.list_accounts(), crm.list_contacts(), PARENT_NAME)


def test_approving_everything_converges_to_an_empty_queue(locations, fake_crm):
    for p in plan(locations, fake_crm)["proposals"]:
        apply_proposal(fake_crm, p)
    after = plan(locations, fake_crm)

    assert after["proposals"] == []
    assert Counter(m["classification"] for m in after["matches"]) == {"confident_match": 35}
    # every website location now has exactly one live account under Bellhaven
    live = [a for a in fake_crm.list_accounts(parent_id=BELLHAVEN)
            if a["status"] == "Active" and not a["duplicate_of_account"] and not a["chow_current_account"]]
    assert len(live) == 35 and {m["account_id"] for m in after["matches"]} == {a["account_id"] for a in live}
    # the three accounts that left the website are each resolved, none silently dropped
    assert sorted(u["state"].split(" ")[0] for u in after["unlisted"] if u["status"] != "Inactive") == ["already", "already", "resolved"]


def test_second_run_proposes_nothing_new(locations, fake_crm):
    store = Store(":memory:")
    first = store.sync_proposals(store.start_run(), plan(locations, fake_crm)["proposals"])
    second = store.sync_proposals(store.start_run(), plan(locations, fake_crm)["proposals"])
    assert (first["new"], second["new"], second["still_pending"]) == (29, 0, 29)
    assert store.counts() == {"pending": 29}


def test_rejected_and_approved_items_are_never_proposed_again(locations, fake_crm):
    store = Store(":memory:")
    store.sync_proposals(store.start_run(), plan(locations, fake_crm)["proposals"])
    rejected, approved = store.list(("pending",))[:2]
    store.decide(rejected["id"], "rejected", "tester", "not convinced", only_if="pending")
    assert store.claim(approved["id"])
    store.decide(approved["id"], "applied", "tester", result=apply_proposal(fake_crm, approved))

    counts = store.sync_proposals(store.start_run(), plan(locations, fake_crm)["proposals"])  # CRM still shows the rejected diff
    assert counts["new"] == 0 and counts["already_decided"] == 1
    assert store.get(rejected["id"])["status"] == "rejected"
    assert store.get(approved["id"])["status"] == "applied"
    assert store.counts() == {"pending": 27, "rejected": 1, "applied": 1}


def test_pending_item_closes_itself_when_no_longer_needed_and_can_come_back(locations, fake_crm):
    store = Store(":memory:")
    proposals = plan(locations, fake_crm)["proposals"]
    store.sync_proposals(store.start_run(), proposals)
    counts = store.sync_proposals(store.start_run(), proposals[1:])  # someone fixed the first one by hand
    assert counts["auto_closed"] == 1 and store.counts() == {"pending": 28, "obsolete": 1}
    counts = store.sync_proposals(store.start_run(), proposals)  # ...and it regressed
    assert counts["reopened"] == 1 and store.counts() == {"pending": 29}


def test_a_proposal_can_only_be_claimed_once(locations, fake_crm):
    store = Store(":memory:")
    store.sync_proposals(store.start_run(), plan(locations, fake_crm)["proposals"])
    first = store.list(("pending",))[0]["id"]
    assert store.claim(first) is True
    assert store.claim(first) is False  # the double click
    assert store.decide(first, "rejected", "tester", only_if="pending") is False  # too late to reject
