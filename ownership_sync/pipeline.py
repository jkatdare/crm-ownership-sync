"""One scheduled run: scrape -> read the CRM -> match -> record proposals. Never writes to the CRM."""
from collections import Counter

from . import config
from .apply import apply_proposal
from .crm import CrmClient, InMemoryCrm
from .matcher import build_plan
from .scraper import ScrapeError, scrape


def run_pipeline(store, crm=None, scrape_fn=scrape):
    run_id = store.start_run()
    try:
        locations, scrape_stats = scrape_fn(config.BASE_URL)
        # Never act on a partial scrape: a half-loaded site would look like closed or sold facilities.
        # 1. The directory states how many communities it lists; scraping a different number from it
        #    proves the crawl was incomplete (or double counted). This also protects the very first run.
        claimed = scrape_stats.get("directory_claims")
        if claimed is not None and claimed != scrape_stats["in_directory"]:
            raise ScrapeError(f"the directory says it lists {claimed} communities but {scrape_stats['in_directory']} "
                              "were scraped from it; refusing to propose anything from an incomplete scrape")
        # 2. A sharp drop against the last good run needs a human to acknowledge it.
        previous = store.last_run("ok")
        baseline = previous["summary"]["scrape"]["communities_found"] if previous else 0
        if not locations or len(locations) < config.MIN_SCRAPE_RATIO * baseline:
            raise ScrapeError(f"scraped {len(locations)} communities but the last good run had {baseline}; refusing to "
                              "propose anything. If the drop is real, acknowledge it by running once with MIN_SCRAPE_RATIO=0")

        crm = crm or CrmClient(config.API_URL, config.API_TOKEN, read_only=True)  # read-only by construction
        accounts, contacts = crm.list_accounts(), crm.list_contacts()
        pins = config.load_overrides().get("survivor_pins")
        plan = build_plan(locations, accounts, contacts, config.TARGET_PARENT_NAME, pins)

        summary = {
            "scrape": scrape_stats,
            "crm": {"accounts": len(accounts), "contacts": len(contacts), "target_parent": plan["target"]},
            "classification": dict(Counter(m["classification"] for m in plan["matches"])),
            "proposals": store.sync_proposals(run_id, plan["proposals"]),
            "matches": plan["matches"],
            "unlisted": plan["unlisted"],
        }
        store.finish_run(run_id, "ok", summary)
        return summary
    except Exception as exc:
        store.finish_run(run_id, "error", error=f"{type(exc).__name__}: {exc}")
        raise


def rehearse(store, live_crm=None, scrape_fn=scrape):
    """Dry-run every pending approval against a private copy of the LIVE CRM. Writes nothing anywhere.

    Each proposal goes through the real executor and all of its guards, on the records exactly as
    they are in the CRM right now; the writes land in the copy. The matcher is then run on the copy
    to show what the next daily run would propose once everything has been approved.
    """
    live_crm = live_crm or CrmClient(config.API_URL, config.API_TOKEN, read_only=True)
    rehearsal = InMemoryCrm(live_crm.list_accounts(), live_crm.list_contacts())
    live_accounts = len(rehearsal.accounts)
    results = []
    for proposal in store.list(("pending", "failed")):
        first_call = len(rehearsal.writes)
        try:
            apply_proposal(rehearsal, proposal)
            results.append({"id": proposal["id"], "title": proposal["title"], "ok": True,
                            "calls": rehearsal.writes[first_call:]})
        except Exception as exc:
            results.append({"id": proposal["id"], "title": proposal["title"], "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
    locations, _stats = scrape_fn(config.BASE_URL)
    after = build_plan(locations, rehearsal.list_accounts(), rehearsal.list_contacts(), config.TARGET_PARENT_NAME,
                       config.load_overrides().get("survivor_pins"))
    return {"accounts": live_accounts,
            "results": results,
            "remaining": [p["title"] for p in after["proposals"]],
            "classification": dict(Counter(m["classification"] for m in after["matches"]))}
