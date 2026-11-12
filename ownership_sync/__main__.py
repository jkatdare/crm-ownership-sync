"""Command line entry point.

    python -m ownership_sync run      # the daily job: scrape + match + record proposals (CRM read-only)
    python -m ownership_sync rehearse # dry-run every pending approval on a copy of the live CRM; writes nothing
    python -m ownership_sync serve    # the local review app (approve / reject)
    python -m ownership_sync serve --demo   # the same app replayed on a copy of the original data; never touches the CRM
    python -m ownership_sync scrape   # just the scraper: every location as JSON (--table for a readable listing)
    python -m ownership_sync report   # every proposal and its decision, as a Markdown table
"""
import argparse
import json
import sys
from dataclasses import asdict

from . import config
from .pipeline import rehearse, run_pipeline
from .scraper import scrape
from .store import Store


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy code page
    parser = argparse.ArgumentParser(prog="ownership_sync", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="scrape, match and record proposals; never writes to the CRM")
    serve = commands.add_parser("serve", help="start the review app")
    serve.add_argument("--port", type=int)
    serve.add_argument("--demo", action="store_true",
                       help="replay on an in-memory copy of the original sandbox data; the CRM is never read or written")
    scrape_cmd = commands.add_parser("scrape", help="print the scraped locations as JSON")
    scrape_cmd.add_argument("--table", action="store_true", help="print a readable table instead of JSON")
    commands.add_parser("report", help="print every proposal and its decision as Markdown")
    commands.add_parser("rehearse", help="dry-run every pending approval against a copy of the live CRM; writes nothing")
    args = parser.parse_args(argv)

    if args.command == "scrape":
        locations, stats = scrape(config.BASE_URL)
        if not args.table:
            print(json.dumps({"stats": stats, "locations": [asdict(loc) for loc in locations]}, indent=2))
            return 0
        elsewhere = stats["outside_directory"]
        print(f"Scraped {stats['communities_found']} communities from {config.BASE_URL} ({stats['pages_crawled']} pages crawled):")
        print(f"  {stats['in_directory']} from the directory"
              + (f" + {len(elsewhere)} linked only elsewhere on the site ({', '.join(elsewhere)})" if elsewhere else ""))
        print(f"  the site itself says: {stats.get('home_page_claims', '?')} on the home page, "
              f"{stats.get('directory_claims', '?')} in the directory")
        print("  warnings: " + ("; ".join(stats["warnings"]) or "none"))
        print()
        row = "{:<50} {:<26} {:<15} {:<3} {:<6} {}"
        print(row.format("NAME", "STREET", "CITY", "ST", "ZIP", "CARE OFFERINGS"))
        for loc in locations:
            print(row.format(loc.name, loc.street, loc.city, loc.state, loc.zip, ", ".join(loc.care_offerings)))
    elif args.command == "report":
        store = Store(config.DB_PATH)
        print("| # | Category | Proposal | Status | Decided by | Decided at (UTC) | Note |")
        print("|---|---|---|---|---|---|---|")
        for p in store.list(("pending", "applying", "applied", "rejected", "failed", "obsolete")):
            print(f"| {p['id']} | {p['category']} | {p['title']} | {p['status']} | {p['decided_by'] or ''} | "
                  f"{p['decided_at'] or ''} | {p['decision_note'] or ''} |")
    elif args.command == "rehearse":
        report = rehearse(Store(config.DB_PATH))
        results = report["results"]
        print(f"Rehearsing {len(results)} pending approval(s) on a private copy of the live CRM "
              f"({report['accounts']} accounts read). Nothing is written to the CRM or to the ledger.")
        print()
        for r in results:
            print(f"  {'OK  ' if r['ok'] else 'FAIL'}  #{r['id']:<3} {r['title']}")
            if r["ok"]:
                calls = ", ".join(f"{c['method']} {c['path']}" for c in r["calls"]) or "nothing to write (already in place)"
                print(f"              would send: {calls}")
            else:
                print(f"              {r['error']}")
        failed = [r for r in results if not r["ok"]]
        calls = [c for r in results for c in r.get("calls", [])]
        print()
        print(f"{len(results) - len(failed)} of {len(results)} would apply cleanly "
              f"({sum(c['method'] == 'POST' for c in calls)} POST, {sum(c['method'] == 'PATCH' for c in calls)} PATCH); "
              f"{len(failed)} would be refused.")
        print(f"After approving all of them, the next run would propose {len(report['remaining'])} change(s); locations: "
              + ", ".join(f"{n} {k}" for k, n in sorted(report["classification"].items())) + ".")
        for title in report["remaining"]:
            print(f"  still proposed: {title}")
        return 1 if failed else 0
    elif args.command == "serve":
        from .app import create_app, create_demo_app
        port = args.port or (5001 if args.demo else 5000)  # different default, so both can run side by side
        if args.demo:
            print(f"DEMO review app: http://127.0.0.1:{port}  (in-memory copy of the original data; the CRM is not touched)")
            create_demo_app().run(host="127.0.0.1", port=port, debug=False)
        else:
            print(f"Review app: http://127.0.0.1:{port}  (state: {config.DB_PATH})")
            create_app().run(host="127.0.0.1", port=port, debug=False)
    else:
        store = Store(config.DB_PATH)
        try:
            summary = run_pipeline(store)
        except Exception as exc:
            print(f"RUN FAILED - nothing proposed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        scrape_stats, proposals = summary["scrape"], summary["proposals"]
        print(f"Scraped {scrape_stats['communities_found']} communities "
              f"({scrape_stats['in_directory']} in the directory, {len(scrape_stats['outside_directory'])} linked only elsewhere); "
              f"read {summary['crm']['accounts']} CRM accounts.")
        for warning in scrape_stats["warnings"]:
            print(f"  WARNING: {warning}")
        print("Locations: " + ", ".join(f"{n} {k}" for k, n in sorted(summary["classification"].items())))
        print(f"Proposals: {proposals['new']} new, {proposals['still_pending']} still pending, "
              f"{proposals['already_decided']} already decided (not re-proposed), {proposals['reopened']} reopened, "
              f"{proposals['auto_closed']} auto-closed. Awaiting review: {proposals['pending_total']}.")
        for title in proposals.get("applied_but_still_differs", []):
            print(f"  WARNING: applied earlier but the CRM still differs: {title}")
        for title in proposals.get("applied_then_undone", []):
            print(f"  NOTE: applied earlier, undone in the CRM since, so it is queued again: {title}")
        for title in proposals.get("rejected_and_back", []):
            print(f"  NOTE: rejected earlier, went away and is back (still rejected; reopen it if this is new): {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
