"""Command line entry point.

    python -m ownership_sync run      # the daily job: scrape + match + record proposals (CRM read-only)
    python -m ownership_sync serve    # the local review app (approve / reject)
    python -m ownership_sync scrape   # just print the scraped locations as JSON
    python -m ownership_sync report   # every proposal and its decision, as a Markdown table
"""
import argparse
import json
import sys
from dataclasses import asdict

from . import config
from .pipeline import run_pipeline
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
    serve.add_argument("--port", type=int, default=5000)
    commands.add_parser("scrape", help="print the scraped locations as JSON")
    commands.add_parser("report", help="print every proposal and its decision as Markdown")
    args = parser.parse_args(argv)

    if args.command == "scrape":
        locations, stats = scrape(config.BASE_URL)
        print(json.dumps({"stats": stats, "locations": [asdict(loc) for loc in locations]}, indent=2))
    elif args.command == "report":
        store = Store(config.DB_PATH)
        print("| # | Category | Proposal | Status | Decided by | Decided at (UTC) | Note |")
        print("|---|---|---|---|---|---|---|")
        for p in store.list(("pending", "applying", "applied", "rejected", "failed", "obsolete")):
            print(f"| {p['id']} | {p['category']} | {p['title']} | {p['status']} | {p['decided_by'] or ''} | "
                  f"{p['decided_at'] or ''} | {p['decision_note'] or ''} |")
    elif args.command == "serve":
        from .app import create_app
        print(f"Review app: http://127.0.0.1:{args.port}  (state: {config.DB_PATH})")
        create_app().run(host="127.0.0.1", port=args.port, debug=False)
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
