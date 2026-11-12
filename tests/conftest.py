"""Shared fixtures: the untouched CRM seed, the scraped locations, and an in-memory fake CRM.

The fake CRM lets the tests exercise approvals end to end (including retries and failures) without
ever touching the real sandbox.
"""
import json
from pathlib import Path

import pytest

from ownership_sync.crm import CrmError, InMemoryCrm
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


class FakeCrm(InMemoryCrm):
    """The package's in-memory CRM, plus a switch to make the next account update fail."""

    def __init__(self, accounts, contacts=()):
        super().__init__(accounts, contacts)
        self.fail_next_patch = False

    def update_account(self, account_id, fields):
        if self.fail_next_patch:
            self.fail_next_patch = False
            raise CrmError("PATCH failed (simulated outage)")
        return super().update_account(account_id, fields)


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
