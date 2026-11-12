"""Shared fixtures: the untouched CRM seed, the scraped locations, and an in-memory fake CRM.

The fake CRM lets the tests exercise approvals end to end (including retries and failures) without
ever touching the real sandbox.
"""
import copy
import json
from pathlib import Path

import pytest

from ownership_sync.crm import CrmError
from ownership_sync.scraper import Location

FIXTURES = Path(__file__).parent / "fixtures"
PARENT_NAME = "Bellhaven Senior Living (Parent Account)"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def seed_accounts():
    return load("seed_accounts.json")


@pytest.fixture
def seed_contacts():
    return load("seed_contacts.json")


@pytest.fixture
def locations():
    return [Location(**row) for row in load("locations.json")]


class FakeCrm:
    """Behaves like the sandbox API for the calls the system makes."""

    def __init__(self, accounts, contacts=()):
        self.accounts = {a["account_id"]: copy.deepcopy(a) for a in accounts}
        self.contacts = [dict(c) for c in contacts]
        self.writes = []
        self.clock = 0
        self.fail_next_patch = False

    def _touch(self, account):
        self.clock += 1
        account["updated_at"] = f"2030-01-01 00:00:{self.clock:04d}Z"

    def _log(self, method, path, body):
        self.writes.append({"method": method, "path": path, "body": body, "status": 200})

    def list_accounts(self, **filters):
        rows = [copy.deepcopy(a) for a in self.accounts.values()]
        if filters.get("parent_id"):
            rows = [a for a in rows if a["parent_id"] == filters["parent_id"]]
        if filters.get("zip"):
            rows = [a for a in rows if a["billing_zip"] == filters["zip"]]
        return rows

    def list_contacts(self, **filters):
        return [dict(c) for c in self.contacts if filters.get("account_id") in (None, c["account_id"])]

    def update_contact(self, contact_id, fields):
        contact = next(c for c in self.contacts if c["contact_id"] == contact_id)
        contact.update(fields)
        self._log("PATCH", f"/contacts/{contact_id}", fields)
        return dict(contact)

    def get_account(self, account_id):
        if account_id not in self.accounts:
            raise CrmError(f"GET /accounts/{account_id} -> HTTP 404")
        return copy.deepcopy(self.accounts[account_id])

    def update_account(self, account_id, fields):
        if self.fail_next_patch:
            self.fail_next_patch = False
            raise CrmError("PATCH failed (simulated outage)")
        account = self.accounts[account_id]
        account.update(fields)
        if "parent_id" in fields:
            account["parent_name"] = self.accounts[fields["parent_id"]]["name"] if fields["parent_id"] else ""
        self._touch(account)
        self._log("PATCH", f"/accounts/{account_id}", fields)
        return copy.deepcopy(account)

    def create_account(self, fields):
        account_id = f"001NEW{len(self.accounts):012d}"
        account = {
            "account_id": account_id, "name": "", "parent_id": "", "parent_name": "", "billing_street": "",
            "billing_city": "", "billing_state": "", "billing_zip": "", "care_type": "", "status": "Active", "phone": "",
            "lifetime_revenue": 0, "outstanding_ar": 0, "chow_current_account": "", "duplicate_of_account": "",
            "note": "", "created_by_candidate": True, **fields,
        }
        account["parent_name"] = self.accounts[account["parent_id"]]["name"] if account["parent_id"] else ""
        self.accounts[account_id] = account
        self._touch(account)
        self._log("POST", "/accounts", fields)
        return copy.deepcopy(account)


@pytest.fixture
def fake_crm(seed_accounts, seed_contacts):
    return FakeCrm(seed_accounts, seed_contacts)


def account(account_id, name, parent_id="", **overrides):
    """A minimal CRM account for hand-built scenarios."""
    return {
        "account_id": account_id, "name": name, "parent_id": parent_id, "parent_name": "", "billing_street": "",
        "billing_city": "", "billing_state": "", "billing_zip": "", "care_type": "Assisted Living", "status": "Active",
        "phone": "", "lifetime_revenue": 0, "outstanding_ar": 0, "chow_current_account": "",
        "duplicate_of_account": "", "note": "", "updated_at": "2026-01-01 00:00:00Z", **overrides,
    }
