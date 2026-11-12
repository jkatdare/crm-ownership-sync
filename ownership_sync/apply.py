"""Execute ONE approved proposal against the CRM.

Reached only from the review app's Approve button. Guards, in order:
  1. Idempotency - if the CRM already shows the proposed end state (a retry after a crash, or
     someone fixed it by hand) nothing is written. A CHOW never creates a second new account.
  2. Drift       - the account must be unchanged since the reviewer saw it (updated_at); otherwise
     the proposal is stale and nothing is written.
  3. SOP         - re-checked on the LIVE record: a parent change is refused if the account now has
     revenue history and open AR; a CHOW is refused if the account no longer needs preserving.
  4. Read-back   - after writing, the account is fetched again and compared with what was intended.
"""
from datetime import date

from .normalize import norm_street
from .sop import must_preserve


class StaleProposal(RuntimeError):
    """The CRM no longer looks like it did when this was proposed. Re-run the pipeline."""


class SopViolation(RuntimeError):
    """Applying this would break the change-of-ownership SOP."""


class ApplyError(RuntimeError):
    """The CRM did not end up in the intended state."""


def preview_calls(proposal):
    """The exact API calls an approval makes - shown to the reviewer before they click."""
    payload, calls = proposal["payload"], []
    if payload.get("new_account"):
        calls.append({"method": "POST", "path": "/accounts", "body": payload["new_account"]})
    if proposal["kind"] == "patch":
        for contact in payload.get("move_contacts") or []:
            calls.append({"method": "PATCH", "path": f"/contacts/{contact['contact_id']}",
                          "body": {"account_id": payload["fields"]["duplicate_of_account"]}})
        calls.append({"method": "PATCH", "path": f"/accounts/{payload['account_id']}", "body": payload["fields"]})
    if proposal["kind"] == "chow":
        new_id = payload.get("link_to") or "<account_id returned by the POST above>"
        calls.append({"method": "PATCH", "path": f"/accounts/{payload['account_id']}", "body": {"chow_current_account": new_id}})
    return calls


def _stamp(note):
    return f"[ownership-sync {date.today().isoformat()}] {note}"


def _check_drift(account, payload):
    if account["updated_at"] != payload["expect_updated_at"]:
        raise StaleProposal(f"{account['name']} ({account['account_id']}) changed in the CRM at {account['updated_at']}, "
                            "after this proposal was made")


def _verify(crm, account_id, fields):
    after = crm.get_account(account_id)
    wrong = {k: after.get(k) for k, v in fields.items() if after.get(k) != v}
    if wrong:
        raise ApplyError(f"read-back of {account_id} does not match what was written: {wrong}")
    return after


def _find_at_address(crm, new):
    """An existing live account under the same parent at the same address (so we never create a second)."""
    for a in crm.list_accounts(parent_id=new["parent_id"], zip=new["billing_zip"]):
        if (a["parent_id"] == new["parent_id"] and not a["duplicate_of_account"]
                and norm_street(a["billing_street"]) == norm_street(new["billing_street"])):
            return a
    return None


def _create(crm, new):
    intended = {**new, "note": _stamp(new["note"])}
    created = crm.create_account(intended)
    new_id = created.get("account_id") or (created.get("data") or {}).get("account_id")
    if not new_id:
        raise ApplyError(f"the CRM did not return an account_id for the new account: {created}")
    # Read back. If the POST ignored any field, set it explicitly rather than leave a half-filled
    # account behind (accounts cannot be deleted).
    after = crm.get_account(new_id)
    missing = {k: v for k, v in intended.items() if after.get(k) != v}
    if missing:
        crm.update_account(new_id, missing)
        _verify(crm, new_id, missing)
    return new_id


def _move_contacts(crm, payload):
    """Carry a duplicate's contacts over to the surviving account (the API has no merge).

    Only the contacts the reviewer saw, and only those still attached to the duplicate, so a retry
    moves nothing twice.
    """
    wanted = {c["contact_id"] for c in payload.get("move_contacts") or []}
    survivor_id = payload["fields"].get("duplicate_of_account")
    moved = []
    for contact in crm.list_contacts(account_id=payload["account_id"]) if wanted and survivor_id else []:
        if contact["contact_id"] in wanted and contact["account_id"] == payload["account_id"]:
            crm.update_contact(contact["contact_id"], {"account_id": survivor_id})
            moved.append(contact["contact_id"])
    return moved


def _apply_patch(crm, payload):
    account = crm.get_account(payload["account_id"])
    fields = dict(payload["fields"])
    core = {k: v for k, v in fields.items() if k != "note"}
    # Already in place? For a note-only proposal that means the note text is already on the account.
    already = all(account.get(k) == v for k, v in core.items()) if core else fields["note"] in (account.get("note") or "")
    if not already:
        _check_drift(account, payload)
        if "parent_id" in fields and must_preserve(account):
            raise SopViolation(f"{account['name']} has revenue history and outstanding AR - its parent must not change (CHOW SOP)")
        if fields.get("duplicate_of_account"):
            survivor = crm.get_account(fields["duplicate_of_account"])
            if survivor["duplicate_of_account"] or survivor["account_id"] == account["account_id"]:
                raise StaleProposal("the surviving record is itself marked as a duplicate")
    moved = _move_contacts(crm, payload)
    if already:
        return {"noop": "the CRM already shows these values", "account_id": account["account_id"], "moved_contacts": moved}
    if "note" in fields:
        fields["note"] = " | ".join(filter(None, [account.get("note"), _stamp(fields["note"])]))
    crm.update_account(account["account_id"], fields)
    _verify(crm, account["account_id"], fields)
    return {"updated": account["account_id"], "moved_contacts": moved}


def _apply_create(crm, payload):
    existing = _find_at_address(crm, payload["new_account"])
    if existing:
        return {"noop": "an account already exists at this address under this parent", "account_id": existing["account_id"]}
    return {"created": _create(crm, payload["new_account"])}


def _apply_chow(crm, payload):
    old = crm.get_account(payload["account_id"])
    if old["chow_current_account"]:
        return {"noop": f"already linked to {old['chow_current_account']}", "account_id": old["account_id"]}
    _check_drift(old, payload)
    if not must_preserve(old):
        raise StaleProposal(f"{old['name']} no longer has both revenue history and outstanding AR, so the SOP now "
                            "allows a direct re-parent")
    result = {"preserved": old["account_id"]}
    new_id = payload.get("link_to")
    if new_id:
        crm.get_account(new_id)  # must exist
    else:
        existing = _find_at_address(crm, payload["new_account"])  # left behind by an interrupted earlier attempt?
        new_id = existing["account_id"] if existing else _create(crm, payload["new_account"])
        result["reused" if existing else "created"] = new_id
    # The ONLY change to the old account - everything else on it stays exactly as it is.
    crm.update_account(old["account_id"], {"chow_current_account": new_id})
    after = _verify(crm, old["account_id"], {"chow_current_account": new_id})
    if after["parent_id"] != old["parent_id"]:
        raise ApplyError("the old account's parent changed - it must not")
    return {**result, "linked_to": new_id}


def apply_proposal(crm, proposal):
    handler = {"patch": _apply_patch, "create": _apply_create, "chow": _apply_chow}[proposal["kind"]]
    return handler(crm, proposal["payload"])
