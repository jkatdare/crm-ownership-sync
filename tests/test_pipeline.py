"""The scheduled run: it must refuse to act on an incomplete scrape, and it must not write."""
import pytest

from ownership_sync import config
from ownership_sync.pipeline import run_pipeline
from ownership_sync.scraper import ScrapeError
from ownership_sync.store import Store


def scrape_returning(locations, **stats):
    full = {"pages_crawled": 6, "communities_found": len(locations), "in_directory": len(locations) - 1,
            "outside_directory": [], "directory_claims": len(locations) - 1, "home_page_claims": len(locations),
            "warnings": [], **stats}
    return lambda _base_url: (locations, full)


def test_a_normal_run_records_proposals_and_never_writes(locations, fake_crm):
    store = Store(":memory:")
    summary = run_pipeline(store, crm=fake_crm, scrape_fn=scrape_returning(locations))
    assert summary["proposals"]["new"] == 29 and summary["classification"]["confident_match"] == 17
    assert store.last_run()["status"] == "ok" and fake_crm.writes == []


def test_run_aborts_when_the_directory_count_does_not_match_what_was_scraped(locations, fake_crm):
    store = Store(":memory:")
    partial = locations[:32]  # three cards went missing from a directory page
    with pytest.raises(ScrapeError, match="incomplete scrape"):
        run_pipeline(store, crm=fake_crm, scrape_fn=scrape_returning(partial, directory_claims=34, in_directory=31))
    assert store.last_run()["status"] == "error" and store.counts() == {}  # nothing proposed, even on a first run


def test_sharp_drop_needs_a_human_to_acknowledge_it(locations, fake_crm, monkeypatch):
    store = Store(":memory:")
    run_pipeline(store, crm=fake_crm, scrape_fn=scrape_returning(locations))
    shrunk = scrape_returning(locations[:20])  # self-consistent, but 15 communities fewer than yesterday
    with pytest.raises(ScrapeError, match="MIN_SCRAPE_RATIO=0"):
        run_pipeline(store, crm=fake_crm, scrape_fn=shrunk)
    assert store.counts() == {"pending": 29}  # the queue is untouched by the refused run

    monkeypatch.setattr(config, "MIN_SCRAPE_RATIO", 0.0)  # the operator confirms the drop is real
    assert run_pipeline(store, crm=fake_crm, scrape_fn=shrunk)["scrape"]["communities_found"] == 20


def test_rehearsal_applies_everything_to_a_copy_and_changes_nothing_real(locations, fake_crm):
    from ownership_sync.pipeline import rehearse

    store = Store(":memory:")
    run_pipeline(store, crm=fake_crm, scrape_fn=scrape_returning(locations))
    before = {a["account_id"]: a for a in fake_crm.list_accounts()}

    report = rehearse(store, live_crm=fake_crm, scrape_fn=scrape_returning(locations))

    assert len(report["results"]) == 29 and all(r["ok"] for r in report["results"])
    assert report["remaining"] == [] and report["classification"] == {"confident_match": 35}
    # the "live" CRM and the ledger are exactly as they were
    assert fake_crm.writes == [] and {a["account_id"]: a for a in fake_crm.list_accounts()} == before
    assert store.counts() == {"pending": 29}


def test_the_pipeline_tells_the_planner_what_the_reviewer_has_already_declined(locations, fake_crm):
    dayton = "0010KJFP601YNTZVDA"
    store = Store(":memory:")
    run_pipeline(store, crm=fake_crm, scrape_fn=scrape_returning(locations))
    rename = next(p for p in store.list(("pending",)) if p["account_id"] == dayton)
    store.decide(rename["id"], "rejected", "tester", only_if="pending")
    fake_crm.accounts[dayton]["care_type"] = "Assisted Living"  # a second difference appears on the same account

    summary = run_pipeline(store, crm=fake_crm, scrape_fn=scrape_returning(locations))
    assert summary["proposals"]["new"] == 1
    newest = store.list(("pending",))[-1]
    assert newest["account_id"] == dayton and "name" not in newest["payload"]["fields"]  # only the care type
