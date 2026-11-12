"""Thin client for the CRM sandbox API.

Two safety properties live here:
  * read_only=True (what the daily pipeline uses) makes every write raise, so the scheduled job
    physically cannot change the CRM. Only the review app's approve action builds a writable client.
  * every write is appended to self.writes, which becomes the audit trail of an approval.
"""
import copy
import time

import requests


class CrmError(RuntimeError):
    pass


class CrmClient:
    def __init__(self, api_url, token, read_only=False, session=None):
        if not token:
            raise CrmError("CRM_API_TOKEN is not set - create a .env file containing CRM_API_TOKEN=<your token> (see the README)")
        self.api_url = api_url.rstrip("/")
        self.read_only = read_only
        self.writes = []
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})

    def _request(self, method, path, params=None, body=None):
        if method != "GET" and self.read_only:
            raise CrmError(f"read-only client refused {method} {path}")
        # GET and PATCH are idempotent, so a blip is retried. POST is never retried here: a second
        # POST could create a second account, so the executor re-checks the CRM before any retry.
        attempts = 1 if method == "POST" else 3
        for attempt in range(attempts):
            try:
                resp = self.session.request(method, self.api_url + path, params=params, json=body, timeout=30)
            except requests.RequestException as exc:
                if attempt == attempts - 1:
                    if method != "GET":
                        self.writes.append({"method": method, "path": path, "body": body, "status": "no response"})
                    raise CrmError(f"{method} {path}: {exc}") from exc
                time.sleep(1 + attempt)
                continue
            if resp.status_code >= 500 and attempt < attempts - 1:
                time.sleep(1 + attempt)
                continue
            if method != "GET":
                self.writes.append({"method": method, "path": path, "body": body, "status": resp.status_code})
            if resp.status_code >= 400:
                raise CrmError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json()

    def _all_pages(self, path, **filters):
        items, page = [], 1
        while True:
            data = self._request("GET", path, params={**filters, "page": page, "page_size": 100})
            items += data["data"]
            if not data["data"] or len(items) >= data["total"]:
                return items
            page += 1

    # reads
    def list_accounts(self, **filters):
        return self._all_pages("/accounts", **filters)

    def list_contacts(self, **filters):
        return self._all_pages("/contacts", **filters)

    def get_account(self, account_id):
        return self._request("GET", f"/accounts/{account_id}")

    # writes (only ever reached through an approved proposal)
    def update_account(self, account_id, fields):
        return self._request("PATCH", f"/accounts/{account_id}", body=fields)

    def create_account(self, fields):
        return self._request("POST", "/accounts", body=fields)

    def update_contact(self, contact_id, fields):
        return self._request("PATCH", f"/contacts/{contact_id}", body=fields)


class InMemoryCrm:
    """A private, in-memory copy of CRM records that answers the same calls as CrmClient.

    Seeded from the LIVE CRM it lets every pending approval be rehearsed against real records:
    all the guards run on real data, and the writes land in this copy instead of in the CRM
    (`python -m ownership_sync rehearse`). The test suite seeds one from fixtures for the same reason.
    """

    def __init__(self, accounts, contacts=()):
        self.accounts = {a["account_id"]: copy.deepcopy(a) for a in accounts}
        self.contacts = [dict(c) for c in contacts]
        self.writes = []
        self.clock = 0

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

    def get_account(self, account_id):
        if account_id not in self.accounts:
            raise CrmError(f"GET /accounts/{account_id} -> HTTP 404")
        return copy.deepcopy(self.accounts[account_id])

    def update_account(self, account_id, fields):
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

    def update_contact(self, contact_id, fields):
        contact = next(c for c in self.contacts if c["contact_id"] == contact_id)
        contact.update(fields)
        self._log("PATCH", f"/contacts/{contact_id}", fields)
        return dict(contact)
