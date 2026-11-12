"""Local review app.

Every proposal is shown with its evidence. Approve executes it against the CRM; Reject records the
decision. This module is the ONLY place a writable CRM client is created, and only inside approve().
"""
import getpass
import json
import os
import secrets

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for

from . import config
from .apply import StaleProposal, apply_proposal, preview_calls
from .crm import CrmClient, InMemoryCrm
from .pipeline import run_pipeline
from .store import Store

# category -> (label shown to the reviewer, position in the queue)
CATEGORIES = {
    "chow": ("Change of ownership (SOP)", 1),
    "wrong_parent": ("Wrong parent", 2),
    "duplicate": ("Duplicate record", 3),
    "new_account": ("No CRM account yet", 4),
    "outdated_name": ("Outdated name", 5),
    "not_on_website": ("No longer on website", 6),
    "data_fix": ("Data fix", 7),
    "name_variation": ("Minor name variation", 8),
    "address_note": ("Address note (optional)", 9),
}
TABS = {"pending": ("pending",), "applied": ("applied",), "rejected": ("rejected",),
        "failed": ("failed", "applying"), "obsolete": ("obsolete",)}


def crm_url(account_id):
    """Deep link into the read-only CRM browser (its URL carries the token, so it is built at render time only)."""
    return f"{config.BASE_URL}/crm/{config.API_TOKEN}/accounts/{account_id}"


def create_app(db_path=None, crm_factory=None, demo_crm=None):
    """The review app. With demo_crm (an InMemoryCrm) every read and write goes to that copy instead of the CRM."""
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(16)
    form_token = secrets.token_hex(16)  # every POST must carry it, so only this UI can approve anything
    reviewer = os.environ.get("REVIEWER") or getpass.getuser()
    if demo_crm is not None:
        crm_factory = lambda: demo_crm
    crm_factory = crm_factory or (lambda: CrmClient(config.API_URL, config.API_TOKEN))

    # Nothing can be mid-approval while the app is starting, so anything still marked as such was
    # interrupted by a crash; make it retryable.
    startup = Store(db_path or config.DB_PATH)
    startup.recover_interrupted()
    startup.close()

    def store():
        if "store" not in g:
            g.store = Store(db_path or config.DB_PATH)
        return g.store

    @app.teardown_appcontext
    def close_store(_exc):
        opened = g.pop("store", None)
        if opened:
            opened.close()

    @app.before_request
    def local_requests_only():
        # This page can write to the CRM and its links carry the API token, so it answers only for
        # this machine's own hostnames (a hostile web page cannot reach it under another name)...
        if request.host.partition(":")[0] not in ("127.0.0.1", "localhost"):
            abort(400, "this app only answers on localhost")
        # ...and every POST must carry the per-process form token, so only this UI can approve.
        if request.method == "POST" and request.form.get("csrf") != form_token:
            abort(400, "missing or wrong form token")

    @app.context_processor
    def template_helpers():
        demo = demo_crm is not None  # in a demo, account links open the in-memory copy, not the real CRM
        return {"csrf": form_token, "categories": CATEGORIES, "preview_calls": preview_calls, "demo": demo,
                "crm_url": (lambda account_id: url_for("demo_account", account_id=account_id)) if demo else crm_url,
                "counts": store().counts(), "last_run": store().last_run(), "reviewer": reviewer,
                "as_json": lambda value: json.dumps(value, indent=2, ensure_ascii=False)}

    def back(done=None):
        """Return to the queue; `done` makes it show that proposal's outcome in a banner."""
        tab, anchor = request.form.get("tab", "pending"), request.form.get("anchor", "")
        tab = tab if tab in TABS else "pending"
        anchor = anchor if anchor.startswith("p") and anchor[1:].isdigit() else None
        return redirect(url_for("queue", status=tab, done=done, _anchor=anchor))

    @app.get("/")
    def queue():
        tab = request.args.get("status", "pending")
        if tab not in TABS:
            abort(404)
        proposals = store().list(TABS[tab])
        proposals.sort(key=lambda p: (CATEGORIES.get(p["category"], ("", 99))[1], p["title"]))
        done = store().get(request.args.get("done", type=int) or 0)
        return render_template("queue.html", proposals=proposals, tab=tab, done=done)

    @app.post("/proposals/<int:pid>/approve")
    def approve(pid):
        s = store()
        proposal = s.get(pid) or abort(404)
        # The page says which run's version of the proposal the reviewer was looking at. If a run has
        # refreshed it since, what would be written is no longer exactly what they saw.
        seen = request.form.get("seen")
        if seen is not None and seen != str(proposal["last_run_id"]):
            flash(f"Proposal #{pid} was refreshed by a pipeline run after this page loaded. Nothing was written - "
                  "please review it again.", "warn")
            return back()
        try:
            crm = crm_factory()  # writable client: built only here, only for an explicit approval
        except Exception as exc:  # e.g. no token configured: say so instead of half-starting
            flash(f"Cannot approve #{pid}: {exc}", "error")
            return back()
        if not s.claim(pid):  # atomic: exactly one request can win, so a double click writes once
            flash(f"Proposal #{pid} is already {s.get(pid)['status']}; nothing was written by this click.", "warn")
            return back()
        note = request.form.get("note", "").strip()
        first = len(crm.writes)  # the audit trail of THIS approval starts here
        try:
            result = apply_proposal(crm, proposal)
        except StaleProposal as exc:
            s.decide(pid, "obsolete", reviewer, note, {"error": str(exc), "writes": crm.writes[first:]})
            flash(f"Nothing written for #{pid}: {exc}. Run the pipeline again for a fresh proposal.", "warn")
        except Exception as exc:  # CRM error, SOP guard, failed read-back: keep it visible for a retry
            s.decide(pid, "failed", reviewer, note, {"error": f"{type(exc).__name__}: {exc}", "writes": crm.writes[first:]})
            flash(f"Approval of #{pid} failed: {exc}", "error")
        else:
            s.decide(pid, "applied", reviewer, note, {**result, "writes": crm.writes[first:]})
            return back(done=pid)
        return back()

    @app.post("/proposals/<int:pid>/reject")
    def reject(pid):
        if store().decide(pid, "rejected", reviewer, request.form.get("note", "").strip(), only_if="pending"):
            flash(f"Rejected #{pid}. It will not be proposed again.", "ok")
        else:
            flash(f"Proposal #{pid} is no longer pending; nothing changed.", "warn")
        return back()

    @app.post("/proposals/<int:pid>/reopen")
    def reopen(pid):
        if store().decide(pid, "pending", reviewer, "reopened by reviewer", only_if="rejected"):
            flash(f"Reopened #{pid}.", "ok")
        return back()

    @app.post("/run")
    def run_now():
        try:
            summary = run_pipeline(store(), crm=demo_crm)  # None = the real CRM, through a read-only client
        except Exception as exc:
            flash(f"Pipeline run failed, nothing proposed: {exc}", "error")
        else:
            c = summary["proposals"]
            flash(f"Pipeline run finished: {c['new']} new proposal(s), {c['still_pending']} still pending, "
                  f"{c['already_decided']} already decided and not re-proposed, {c['auto_closed']} auto-closed.", "ok")
            for warning in summary["scrape"]["warnings"]:
                flash(f"Check the scrape: {warning}", "warn")
            for title in c.get("applied_but_still_differs", []):
                flash(f"Applied earlier, but the CRM still differs: {title}", "warn")
            for title in c.get("applied_then_undone", []):
                flash(f"Applied earlier and undone in the CRM since, so it is queued again: {title}", "warn")
            for title in c.get("rejected_and_back", []):
                flash(f"Rejected earlier, went away and is back (still rejected - reopen it if this is new): {title}", "warn")
        return redirect(url_for("queue"))

    @app.get("/matches")
    def matches():
        s = store()
        by_fingerprint = {p["fingerprint"]: p for statuses in TABS.values() for p in s.list(statuses)}
        return render_template("matches.html", run=s.last_run("ok"), by_fingerprint=by_fingerprint)

    @app.get("/history")
    def history():
        return render_template("history.html", events=store().audit_log())

    @app.get("/demo/accounts/<account_id>")
    def demo_account(account_id):
        """Stand-in for the CRM browser during a demo: one record of the in-memory copy."""
        if demo_crm is None or account_id not in demo_crm.accounts:
            abort(404)
        return render_template("demo_account.html", account=demo_crm.get_account(account_id), tab="demo",
                               contacts=demo_crm.list_contacts(account_id=account_id))

    return app


def create_demo_app():
    """The review app replayed on an in-memory copy of the ORIGINAL sandbox data, with a throwaway ledger.

    The real CRM is clean once the queue has been approved, so there is nothing left to show on it.
    This starts again from the data as first read (kept in tests/fixtures) - for a walkthrough, or
    to try a code change against messy data - without touching the CRM or the real ledger.
    """
    fixtures = config.ROOT / "tests" / "fixtures"
    read = lambda name: json.loads((fixtures / name).read_text(encoding="utf-8"))
    demo_crm = InMemoryCrm(read("seed_accounts.json"), read("seed_contacts.json"))
    db_path = config.ROOT / "data" / "demo.db"
    db_path.unlink(missing_ok=True)
    store = Store(db_path)
    run_pipeline(store, crm=demo_crm)
    store.close()
    return create_app(db_path=db_path, demo_crm=demo_crm)
