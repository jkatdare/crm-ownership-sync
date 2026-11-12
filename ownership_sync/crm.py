"""Thin client for the CRM sandbox API.

Two safety properties live here:
  * read_only=True (what the daily pipeline uses) makes every write raise, so the scheduled job
    physically cannot change the CRM. Only the review app's approve action builds a writable client.
  * every write is appended to self.writes, which becomes the audit trail of an approval.
"""
import time

import requests


class CrmError(RuntimeError):
    pass


class CrmClient:
    def __init__(self, api_url, token, read_only=False, session=None):
        if not token:
            raise CrmError("CRM_API_TOKEN is not set - copy .env.example to .env and add your token")
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
