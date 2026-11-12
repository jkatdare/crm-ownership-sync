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


def test_failed_approval_expires_when_a_later_run_no_longer_needs_it(locations, fake_crm):
    store = Store(":memory:")
    proposals = plan(locations, fake_crm)["proposals"]
    store.sync_proposals(store.start_run(), proposals)
    first = store.list(("pending",))[0]
    store.claim(first["id"])
    store.decide(first["id"], "failed", "tester", result={"error": "CRM outage"})  # approved, but the write failed

    still_needed = store.sync_proposals(store.start_run(), proposals)
    assert store.get(first["id"])["status"] == "failed" and still_needed["auto_closed"] == 0  # Retry stays available

    gone = store.sync_proposals(store.start_run(), [p for p in proposals if p["fingerprint"] != first["fingerprint"]])
    assert gone["auto_closed"] == 1 and store.get(first["id"])["status"] == "obsolete"  # no stale Retry left behind


# ----------------------------------------------------------------- the ledger over several runs
def good_run(store, proposals):
    """One pipeline run that finished successfully, as run_pipeline records it."""
    run_id = store.start_run()
    counts = store.sync_proposals(run_id, proposals)
    store.finish_run(run_id, "ok", {"proposals": counts})
    return counts


def test_an_applied_fix_that_is_later_undone_is_queued_again(locations, fake_crm):
    store = Store(":memory:")
    proposals = plan(locations, fake_crm)["proposals"]
    good_run(store, proposals)
    first = store.list(("pending",))[0]
    store.claim(first["id"])
    store.decide(first["id"], "applied", "tester", result={"updated": "x"})
    rest = [p for p in proposals if p["fingerprint"] != first["fingerprint"]]

    assert good_run(store, rest)["new"] == 0  # the fix is in place: the proposal is gone
    back = good_run(store, proposals)  # ...and then someone undid it in the CRM

    assert back["reopened"] == 1 and back["applied_then_undone"] == [first["title"]]
    requeued = store.get(first["id"])
    assert requeued["status"] == "pending" and requeued["decided_by"] is None  # not attributed to the old approver
    history = [(e["event"], e["detail"]["by"]) for e in store.audit_log() if e["proposal_id"] == first["id"]]
    assert history == [("pending", "system"), ("applied", "tester")]  # newest first: who re-queued it, and the approval


def test_an_applied_fix_the_crm_never_took_is_reported_but_not_requeued(locations, fake_crm):
    store = Store(":memory:")
    proposals = plan(locations, fake_crm)["proposals"]
    good_run(store, proposals)
    first = store.list(("pending",))[0]
    store.claim(first["id"])
    store.decide(first["id"], "applied", "tester", result={"updated": "x"})  # ...but the fake CRM was not changed

    for _ in range(2):  # run after run it is still proposed: never queued, always reported
        counts = good_run(store, proposals)
        assert counts["applied_but_still_differs"] == [first["title"]] and counts["reopened"] == 0
        assert store.get(first["id"])["status"] == "applied"


def test_a_rejection_survives_the_proposal_going_away_and_coming_back(locations, fake_crm):
    store = Store(":memory:")
    proposals = plan(locations, fake_crm)["proposals"]
    good_run(store, proposals)
    first = store.list(("pending",))[0]
    store.decide(first["id"], "rejected", "tester", "no", only_if="pending")
    rest = [p for p in proposals if p["fingerprint"] != first["fingerprint"]]

    assert "rejected_and_back" not in good_run(store, proposals)  # still there: quietly stays rejected
    good_run(store, rest)  # the listing drops off the website for a day
    back = good_run(store, proposals)  # and returns

    assert store.get(first["id"])["status"] == "rejected" and back["new"] == 0 and back["reopened"] == 0
    assert back["rejected_and_back"] == [first["title"]]  # not silent: it may be a new event


def test_rejected_and_failed_proposals_keep_current_content_for_a_reopen_or_retry(locations, fake_crm):
    store = Store(":memory:")
    proposals = plan(locations, fake_crm)["proposals"]
    good_run(store, proposals)
    rejected, failed = store.list(("pending",))[:2]
    store.decide(rejected["id"], "rejected", "tester", only_if="pending")
    store.claim(failed["id"])
    store.decide(failed["id"], "failed", "tester", result={"error": "HTTP 422"})

    def reworded(p):  # same change (same fingerprint), new wording and evidence - e.g. the website's phone changed
        changed = p["fingerprint"] in (rejected["fingerprint"], failed["fingerprint"])
        return {**p, "title": p["title"] + " (v2)", "evidence": {**p["evidence"], "reason": "fresh"}} if changed else p

    good_run(store, [reworded(p) for p in proposals])
    for row, status in ((rejected, "rejected"), (failed, "failed")):
        now_row = store.get(row["id"])
        assert (now_row["status"], now_row["evidence"]["reason"]) == (status, "fresh") and now_row["title"].endswith("(v2)")
    assert store.get(failed["id"])["result"] == {"error": "HTTP 422"}  # what happened stays on the record


def test_a_failed_approval_keeps_its_trace_when_a_later_run_closes_it(locations, fake_crm):
    store = Store(":memory:")
    proposals = plan(locations, fake_crm)["proposals"]
    good_run(store, proposals)
    first = store.list(("pending",))[0]
    store.claim(first["id"])
    attempt = {"error": "read-back failed", "writes": [{"method": "PATCH", "path": "/accounts/x", "status": 200}]}
    store.decide(first["id"], "failed", "tester", result=attempt)

    good_run(store, [p for p in proposals if p["fingerprint"] != first["fingerprint"]])  # the write had landed after all
    closed = store.get(first["id"])
    assert closed["status"] == "obsolete" and closed["result"]["failed_attempt"] == attempt


def test_an_interrupted_approval_is_written_to_the_audit_log(locations, fake_crm):
    store = Store(":memory:")
    good_run(store, plan(locations, fake_crm)["proposals"])
    first = store.list(("pending",))[0]
    store.claim(first["id"])  # ...and the process dies before the outcome is recorded
    store.recover_interrupted()
    assert store.get(first["id"])["status"] == "failed"
    (event,) = [e for e in store.audit_log() if e["proposal_id"] == first["id"]]
    assert event["event"] == "failed" and event["detail"]["by"] == "system"


def test_a_decision_made_while_a_run_is_syncing_is_not_overwritten(tmp_path, locations, fake_crm):
    """The sync must hold the write lock before it reads; otherwise its stale read undoes the decision."""
    import threading
    import time

    db = tmp_path / "ledger.db"
    setup = Store(db)
    proposals = plan(locations, fake_crm)["proposals"]
    good_run(setup, proposals)
    target = setup.db.execute("SELECT id FROM proposals WHERE fingerprint = ?", (proposals[0]["fingerprint"],)).fetchone()["id"]
    setup.close()

    class PauseAfterFirstRead:
        """Wraps the run's connection: after its first look at a proposal it stalls, as a slow run would."""

        def __init__(self, real):
            self.real, self.paused = real, False

        def execute(self, sql, *args):
            result = self.real.execute(sql, *args)
            if sql.startswith("SELECT id, status") and not self.paused:
                self.paused = True
                reviewer_can_go.set()
                time.sleep(0.5)
            return result

        def __enter__(self):
            return self.real.__enter__()

        def __exit__(self, *exc):
            return self.real.__exit__(*exc)

    reviewer_can_go = threading.Event()

    def reviewer():
        reviewer_can_go.wait(5)
        other = Store(db)
        other.decide(target, "rejected", "reviewer", "decided mid-run", only_if="pending")
        other.close()

    thread = threading.Thread(target=reviewer)
    thread.start()
    run = Store(db)
    run.db = PauseAfterFirstRead(run.db)
    run.sync_proposals(run.start_run(), proposals)
    thread.join()

    check = Store(db)
    assert check.get(target)["status"] == "rejected"  # the reviewer's decision stands
    assert check.counts() == {"pending": 28, "rejected": 1}


def test_a_run_overtaken_by_a_newer_run_records_nothing(locations, fake_crm):
    import pytest

    from ownership_sync.store import RunOvertaken

    store = Store(":memory:")
    proposals = plan(locations, fake_crm)["proposals"]
    good_run(store, proposals)  # run 1
    rejected, applied = store.list(("pending",))[:2]
    store.decide(rejected["id"], "rejected", "tester", only_if="pending")
    store.claim(applied["id"])
    store.decide(applied["id"], "applied", "tester", result={"updated": "x"})  # recorded, but the CRM never took it
    good_run(store, proposals)  # run 2

    slow = store.start_run()  # run 3 starts (the scheduled job, stuck in its scrape)...
    good_run(store, proposals)  # ...run 4 starts later, and finishes first
    with pytest.raises(RunOvertaken):
        store.sync_proposals(slow, proposals)  # run 3 arrives with older data
    store.finish_run(slow, "error", error="overtaken")
    assert {store.get(r["id"])["last_run_id"] for r in (rejected, applied)} == {4}  # not dragged backwards

    after = good_run(store, proposals)  # run 5, nothing has changed anywhere
    assert after["reopened"] == 0 and "rejected_and_back" not in after and "applied_then_undone" not in after
    assert (store.get(rejected["id"])["status"], store.get(applied["id"])["status"]) == ("rejected", "applied")


def test_claim_fails_if_a_run_refreshed_the_proposal_since_it_was_read(locations, fake_crm):
    store = Store(":memory:")
    proposals = plan(locations, fake_crm)["proposals"]
    good_run(store, proposals)
    first = store.list(("pending",))[0]
    good_run(store, proposals)  # a run refreshes it after the reviewer's request read version 1
    assert store.claim(first["id"], expect_run=first["last_run_id"]) is False
    assert store.get(first["id"])["status"] == "pending"
    assert store.claim(first["id"], expect_run=store.get(first["id"])["last_run_id"]) is True


def test_a_rejected_change_does_not_ride_back_in_on_a_different_bundle(locations, fake_crm):
    """Reject a rename; later another field on the same account also needs fixing. Only that field is proposed."""
    from conftest import PARENT_NAME as parent
    from ownership_sync.matcher import build_plan as plan_with

    dayton = "0010KJFP601YNTZVDA"  # "Arbors at Bellhaven Dayton" - the website calls it "The Arbors at Bellhaven - Dayton"
    store = Store(":memory:")
    first_plan = plan(locations, fake_crm)["proposals"]
    good_run(store, first_plan)
    rename = next(p for p in store.list(("pending",)) if p["account_id"] == dayton)
    assert set(rename["payload"]["fields"]) == {"name", "note"}
    store.decide(rename["id"], "rejected", "tester", "keeping the CRM spelling", only_if="pending")
    assert store.declined_changes() == {dayton: [{"name": "The Arbors at Bellhaven - Dayton"}]}

    def replan():
        return plan_with(locations, fake_crm.list_accounts(), fake_crm.list_contacts(), parent, None, store.declined_changes())

    # Nothing else changed: the identical proposal is produced, recognised and left rejected.
    same = good_run(store, replan()["proposals"])
    assert same["new"] == 0 and store.get(rename["id"])["status"] == "rejected"

    fake_crm.accounts[dayton]["care_type"] = "Assisted Living"  # the website says Memory Support -> Memory Care
    bundle = [p for p in replan()["proposals"] if p["account_id"] == dayton]
    assert [set(p["payload"]["fields"]) - {"note"} for p in bundle] == [{"care_type"}]  # the rename is NOT in it
    counts = good_run(store, replan()["proposals"])
    assert counts["new"] == 1 and store.get(rename["id"])["status"] == "rejected"


def test_part_of_a_rejected_bundle_is_not_proposed_on_its_own_later(locations, fake_crm):
    from conftest import PARENT_NAME as parent
    from ownership_sync.matcher import build_plan as plan_with

    zanesville = "001H1JMVZWP46D5VUF"  # wrong parent AND old name: one proposal with both changes
    store = Store(":memory:")
    good_run(store, plan(locations, fake_crm)["proposals"])
    both = next(p for p in store.list(("pending",)) if p["account_id"] == zanesville)
    assert set(both["payload"]["fields"]) == {"parent_id", "name", "note"}
    store.decide(both["id"], "rejected", "tester", only_if="pending")

    fake_crm.accounts[zanesville]["name"] = "Bellhaven of Zanesville"  # someone fixes the name by hand
    again = plan_with(locations, fake_crm.list_accounts(), fake_crm.list_contacts(), parent, None, store.declined_changes())
    assert [p for p in again["proposals"] if p["account_id"] == zanesville] == []  # the re-parent was declined too
