"""SQLite ledger: every proposal ever made, and what was decided about it.

This is what makes re-runs safe. A proposal is identified by its fingerprint (what it would change).
Once a fingerprint has been approved or rejected it is never proposed again, however often the
pipeline runs. No secrets are stored here.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',   -- running | ok | error
    summary_json TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,                       -- patch | create | chow
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    confidence TEXT NOT NULL,
    account_id TEXT,
    location_slug TEXT,
    payload_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | applying | applied | rejected | failed | obsolete
    first_run_id INTEGER,
    last_run_id INTEGER,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    decision_note TEXT,
    result_json TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    proposal_id INTEGER,
    event TEXT NOT NULL,
    detail_json TEXT
);
"""

# A human has dealt with these; the pipeline must never put them back in the queue.
DECIDED = ("applied", "rejected", "failed", "applying")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _row(row):
    """sqlite Row -> dict with the *_json columns decoded."""
    if row is None:
        return None
    d = dict(row)
    for key in [k for k in d if k.endswith("_json")]:
        d[key[:-5]] = json.loads(d.pop(key)) if d[key] else None
    return d


class Store:
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    # ---------------------------------------------------------------- runs
    def start_run(self):
        with self.db:
            return self.db.execute("INSERT INTO runs (started_at) VALUES (?)", (now(),)).lastrowid

    def finish_run(self, run_id, status, summary=None, error=None):
        with self.db:
            self.db.execute("UPDATE runs SET finished_at = ?, status = ?, summary_json = ?, error = ? WHERE id = ?",
                            (now(), status, json.dumps(summary) if summary else None, error, run_id))

    def last_run(self, status=None):
        where = "WHERE status = ?" if status else ""
        return _row(self.db.execute(f"SELECT * FROM runs {where} ORDER BY id DESC LIMIT 1", (status,) if status else ()).fetchone())

    # ---------------------------------------------------------------- proposals
    def sync_proposals(self, run_id, proposals):
        """Record this run's proposals without ever re-proposing something already decided."""
        counts = {"new": 0, "still_pending": 0, "already_decided": 0, "reopened": 0, "auto_closed": 0}
        reappeared, seen = [], set()
        with self.db:
            for p in proposals:
                seen.add(p["fingerprint"])
                row = self.db.execute("SELECT id, status FROM proposals WHERE fingerprint = ?", (p["fingerprint"],)).fetchone()
                content = (p["kind"], p["category"], p["title"], p["confidence"], p["account_id"], p["location_slug"],
                           json.dumps(p["payload"]), json.dumps(p["evidence"]))
                if row is None:
                    self.db.execute(
                        "INSERT INTO proposals (kind, category, title, confidence, account_id, location_slug, payload_json,"
                        " evidence_json, fingerprint, first_run_id, last_run_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        content + (p["fingerprint"], run_id, run_id, now()))
                    counts["new"] += 1
                elif row["status"] in DECIDED:
                    # Approved or rejected before: remember we saw it, but do NOT put it back in the queue.
                    self.db.execute("UPDATE proposals SET last_run_id = ? WHERE id = ?", (run_id, row["id"]))
                    counts["already_decided"] += 1
                    if row["status"] == "applied":
                        reappeared.append(p["title"])  # applied, yet the CRM still differs: worth a human look
                else:
                    # Still undecided (or auto-closed earlier and now relevant again): refresh its evidence.
                    self.db.execute(
                        "UPDATE proposals SET kind=?, category=?, title=?, confidence=?, account_id=?, location_slug=?,"
                        " payload_json=?, evidence_json=?, status='pending', result_json=NULL, last_run_id=? WHERE id=?",
                        content + (run_id, row["id"]))
                    counts["still_pending" if row["status"] == "pending" else "reopened"] += 1
            # Pending proposals this run no longer produces are closed: the CRM or the website moved on.
            for row in self.db.execute("SELECT id, fingerprint FROM proposals WHERE status = 'pending'").fetchall():
                if row["fingerprint"] not in seen:
                    self.db.execute("UPDATE proposals SET status = 'obsolete', result_json = ? WHERE id = ?",
                                    (json.dumps({"closed": f"no longer proposed as of run {run_id}"}), row["id"]))
                    counts["auto_closed"] += 1
        counts["pending_total"] = self.counts().get("pending", 0)
        if reappeared:
            counts["applied_but_still_differs"] = reappeared
        return counts

    def get(self, proposal_id):
        return _row(self.db.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone())

    def list(self, statuses):
        marks = ",".join("?" * len(statuses))
        rows = self.db.execute(f"SELECT * FROM proposals WHERE status IN ({marks}) ORDER BY id", tuple(statuses)).fetchall()
        return [_row(r) for r in rows]

    def counts(self):
        return dict(self.db.execute("SELECT status, COUNT(*) FROM proposals GROUP BY status").fetchall())

    def claim(self, proposal_id, retry=False):
        """Atomically take a proposal for applying, so a double click cannot write twice."""
        allowed = ("pending", "failed", "applying") if retry else ("pending",)
        marks = ",".join("?" * len(allowed))
        with self.db:
            cur = self.db.execute(f"UPDATE proposals SET status = 'applying' WHERE id = ? AND status IN ({marks})",
                                  (proposal_id, *allowed))
        return cur.rowcount == 1

    def decide(self, proposal_id, status, by, note="", result=None, only_if=None):
        """Record a decision. With only_if, it applies only while the proposal is still in that status."""
        guard = " AND status = ?" if only_if else ""
        with self.db:
            cur = self.db.execute(
                f"UPDATE proposals SET status = ?, decided_at = ?, decided_by = ?, decision_note = ?, result_json = ? WHERE id = ?{guard}",
                (status, now(), by, note, json.dumps(result) if result is not None else None, proposal_id) + ((only_if,) if only_if else ()))
            if cur.rowcount:
                self.db.execute("INSERT INTO audit (at, proposal_id, event, detail_json) VALUES (?,?,?,?)",
                                (now(), proposal_id, status, json.dumps({"by": by, "note": note, "result": result})))
        return cur.rowcount == 1

    def audit_log(self, limit=200):
        rows = self.db.execute(
            "SELECT audit.*, proposals.title FROM audit LEFT JOIN proposals ON proposals.id = audit.proposal_id"
            " ORDER BY audit.id DESC LIMIT ?", (limit,)).fetchall()
        return [_row(r) for r in rows]
