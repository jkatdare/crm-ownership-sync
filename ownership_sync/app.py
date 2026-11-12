"""Local review app.

Every proposal is shown with its evidence. Approve executes it against the CRM; Reject records the
decision. This module is the ONLY place a writable CRM client is created, and only inside approve().
"""
import getpass
import os
import secrets

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for

from . import config
from .apply import StaleProposal, apply_proposal, preview_calls
from .crm import CrmClient
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


def create_app(db_path=None, crm_factory=None):
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(16)
    form_token = secrets.token_hex(16)  # every POST must carry it, so only this UI can approve anything
    reviewer = os.environ.get("REVIEWER") or getpass.getuser()
    crm_factory = crm_factory or (lambda: CrmClient(config.API_URL, config.API_TOKEN))

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
    def require_form_token():
        if request.method == "POST" and request.form.get("csrf") != form_token:
            abort(400, "missing or wrong form token")

    @app.context_processor
    def template_helpers():
        return {"csrf": form_token, "categories": CATEGORIES, "crm_url": crm_url, "preview_calls": preview_calls,
                "counts": store().counts(), "last_run": store().last_run(), "reviewer": reviewer}

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
        if not s.claim(pid, retry=proposal["status"] in ("failed", "applying")):
            flash(f"Proposal #{pid} was already decided ({proposal['status']}); nothing was written.", "warn")
            return back()
        note = request.form.get("note", "").strip()
        crm = crm_factory()  # writable client: built only here, only after an explicit approval
        try:
            result = apply_proposal(crm, proposal)
        except StaleProposal as exc:
            s.decide(pid, "obsolete", reviewer, note, {"error": str(exc), "writes": crm.writes})
            flash(f"Nothing written for #{pid}: {exc}. Run the pipeline again for a fresh proposal.", "warn")
        except Exception as exc:  # CRM error, SOP guard, failed read-back: keep it visible for a retry
            s.decide(pid, "failed", reviewer, note, {"error": f"{type(exc).__name__}: {exc}", "writes": crm.writes})
            flash(f"Approval of #{pid} failed: {exc}", "error")
        else:
            s.decide(pid, "applied", reviewer, note, {**result, "writes": crm.writes})
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
            c = run_pipeline(store())["proposals"]
        except Exception as exc:
            flash(f"Pipeline run failed, nothing proposed: {exc}", "error")
        else:
            flash(f"Pipeline run finished: {c['new']} new proposal(s), {c['still_pending']} still pending, "
                  f"{c['already_decided']} already decided and not re-proposed, {c['auto_closed']} auto-closed.", "ok")
        return redirect(url_for("queue"))

    @app.get("/matches")
    def matches():
        s = store()
        by_fingerprint = {p["fingerprint"]: p for statuses in TABS.values() for p in s.list(statuses)}
        return render_template("matches.html", run=s.last_run("ok"), by_fingerprint=by_fingerprint)

    @app.get("/history")
    def history():
        return render_template("history.html", events=store().audit_log())

    return app
