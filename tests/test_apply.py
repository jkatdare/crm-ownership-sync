"""Approval execution: SOP guard, drift guard, idempotent retries, and the read-only pipeline client."""
import pytest

from conftest import PARENT_NAME
from ownership_sync.apply import SopViolation, StaleProposal, apply_proposal, preview_calls
from ownership_sync.crm import CrmClient, CrmError
from ownership_sync.matcher import build_plan

BELLHAVEN = "0015QAPLGS3FVYEEEM"
TIFFIN, LIMA, SANDUSKY, MILLSTONE_SANDUSKY = "001U6RW32TY0WSXZZB", "001LGFPBJY4N9MB6KL", "001SXSF4ELF0Z2LGDM", "0017JP8Z1UQ763BVK3"


@pytest.fixture
def proposals(locations, fake_crm):
    plan = build_plan(locations, fake_crm.list_accounts(), fake_crm.list_contacts(), PARENT_NAME)
    return {p["account_id"] or p["location_slug"]: p for p in plan["proposals"]}


def test_chow_creates_one_account_and_touches_only_the_link_on_the_old_one(fake_crm, proposals):
    before = fake_crm.get_account(TIFFIN)
    result = apply_proposal(fake_crm, proposals[TIFFIN])
    after, new = fake_crm.get_account(TIFFIN), fake_crm.get_account(result["created"])

    assert [w["method"] for w in fake_crm.writes] == ["POST", "PATCH"]
    assert fake_crm.writes[1]["body"] == {"chow_current_account": new["account_id"]}
    changed = {k for k in before if before[k] != after[k]} - {"updated_at"}
    assert changed == {"chow_current_account"}  # parent, name, status, note ... exactly as they were
    assert (new["parent_id"], new["name"], new["billing_street"]) == (BELLHAVEN, "Bellhaven of Tiffin", "45 St Lawrence Dr")
    assert (new["lifetime_revenue"], new["outstanding_ar"]) == (0, 0)


def test_chow_retry_after_a_failed_link_does_not_create_a_second_account(fake_crm, proposals):
    fake_crm.fail_next_patch = True
    with pytest.raises(CrmError):
        apply_proposal(fake_crm, proposals[TIFFIN])  # POST succeeded, PATCH failed
    result = apply_proposal(fake_crm, proposals[TIFFIN])  # reviewer clicks Retry
    assert "reused" in result and "created" not in result
    tiffins = [a for a in fake_crm.list_accounts(parent_id=BELLHAVEN) if a["name"] == "Bellhaven of Tiffin"]
    assert len(tiffins) == 1
    assert fake_crm.get_account(TIFFIN)["chow_current_account"] == tiffins[0]["account_id"]


def test_chow_to_an_existing_account_creates_nothing(fake_crm, proposals):
    apply_proposal(fake_crm, proposals[SANDUSKY])
    assert [w["method"] for w in fake_crm.writes] == ["PATCH"]
    old = fake_crm.get_account(SANDUSKY)
    assert (old["chow_current_account"], old["parent_id"], old["status"]) == (MILLSTONE_SANDUSKY, BELLHAVEN, "Active")


def test_reparent_is_refused_if_the_account_gained_open_ar_since_it_was_proposed(fake_crm, proposals):
    lima = fake_crm.accounts[LIMA]
    lima["outstanding_ar"] = 2500  # billing posted an invoice; updated_at deliberately left alone
    with pytest.raises(SopViolation):
        apply_proposal(fake_crm, proposals[LIMA])
    assert fake_crm.writes == [] and fake_crm.get_account(LIMA)["parent_id"] != BELLHAVEN


def test_stale_proposal_writes_nothing(fake_crm, proposals):
    fake_crm.update_account(LIMA, {"phone": "(555) 000-0000"})  # someone edited the account after the proposal
    fake_crm.writes.clear()
    with pytest.raises(StaleProposal):
        apply_proposal(fake_crm, proposals[LIMA])
    assert fake_crm.writes == []


def test_applying_twice_is_a_no_op(fake_crm, proposals):
    apply_proposal(fake_crm, proposals[LIMA])
    assert "noop" in apply_proposal(fake_crm, proposals[LIMA])
    created = apply_proposal(fake_crm, proposals["bellhaven-of-batavia"])["created"]
    again = apply_proposal(fake_crm, proposals["bellhaven-of-batavia"])
    assert again["account_id"] == created and "noop" in again
    assert len([w for w in fake_crm.writes if w["method"] == "POST"]) == 1


def test_duplicate_mark_keeps_parent_and_stamps_a_note(fake_crm, proposals):
    loser = "001JD2MWRA74LTSN24"  # Harborview Nursing & Rehab of Port Clinton
    before = fake_crm.get_account(loser)
    apply_proposal(fake_crm, proposals[loser])
    after = fake_crm.get_account(loser)
    assert (after["status"], after["duplicate_of_account"], after["parent_id"]) == ("Inactive", "001UELXDAKFRKB8932", before["parent_id"])
    assert after["note"].startswith("[ownership-sync ")


def test_preview_shows_exactly_the_calls_that_are_made(fake_crm, proposals):
    for key in (LIMA, SANDUSKY, "bellhaven-of-carlisle"):
        fake_crm.writes.clear()
        preview = preview_calls(proposals[key])
        apply_proposal(fake_crm, proposals[key])
        assert [(c["method"], c["path"]) for c in preview] == [(w["method"], w["path"]) for w in fake_crm.writes]


def test_pipeline_client_cannot_write():
    crm = CrmClient("https://example.invalid/api/v1", "token", read_only=True)
    with pytest.raises(CrmError, match="read-only"):
        crm.update_account("001X", {"name": "nope"})
    with pytest.raises(CrmError, match="read-only"):
        crm.create_account({"name": "nope"})


def test_marking_a_duplicate_carries_its_contacts_to_the_survivor(fake_crm, proposals):
    loser, survivor, contact = "001QU150PM4Z15UA71", "001EGU7BMJ942ZTRE6", "003BT8QV7PS7S0XM0P"  # the two Owosso records
    p = proposals[loser]
    assert [c["contact_id"] for c in p["payload"]["move_contacts"]] == [contact]

    preview = preview_calls(p)
    apply_proposal(fake_crm, p)
    assert [(c["method"], c["path"]) for c in preview] == [(w["method"], w["path"]) for w in fake_crm.writes]
    assert [w["path"] for w in fake_crm.writes] == [f"/contacts/{contact}", f"/accounts/{loser}"]
    assert fake_crm.list_contacts(account_id=loser) == []
    assert {c["name"] for c in fake_crm.list_contacts(account_id=survivor)} == {"Gloria Lambert", "Tricia Lindqvist"}


def test_duplicate_retry_after_a_failed_account_update_moves_no_contact_twice(fake_crm, proposals):
    loser = "001QU150PM4Z15UA71"
    fake_crm.fail_next_patch = True
    with pytest.raises(CrmError):
        apply_proposal(fake_crm, proposals[loser])  # contact moved, account update failed
    result = apply_proposal(fake_crm, proposals[loser])  # Retry
    assert result["moved_contacts"] == [] and fake_crm.get_account(loser)["status"] == "Inactive"
    assert len([w for w in fake_crm.writes if w["path"].startswith("/contacts/")]) == 1


def test_create_repairs_fields_the_post_ignored(fake_crm, proposals):
    real_create = fake_crm.create_account
    fake_crm.create_account = lambda fields: real_create({"name": fields["name"]})  # an API that keeps only the name
    new_id = apply_proposal(fake_crm, proposals["bellhaven-of-batavia"])["created"]
    created = fake_crm.get_account(new_id)
    assert (created["parent_id"], created["billing_zip"], created["care_type"]) == (BELLHAVEN, "45103", "Skilled Nursing")
    assert [w["method"] for w in fake_crm.writes] == ["POST", "PATCH"]


def test_note_only_proposal_is_written_once_and_the_po_box_survives(fake_crm, proposals):
    ashtabula = "001NXP9X46CWEPSLSV"
    apply_proposal(fake_crm, proposals[ashtabula])
    after = fake_crm.get_account(ashtabula)
    assert after["billing_street"] == "PO Box 517" and "3156 W Prospect Rd" in after["note"]
    assert "noop" in apply_proposal(fake_crm, proposals[ashtabula])  # a retry does not append the note again
    assert len(fake_crm.writes) == 1


def test_duplicate_mark_is_refused_if_the_record_gained_revenue_and_open_ar(fake_crm, proposals):
    loser = "001JD2MWRA74LTSN24"  # Harborview Nursing & Rehab of Port Clinton, 0 / 0 when it was proposed
    fake_crm.accounts[loser].update(lifetime_revenue=40000, outstanding_ar=2500)  # billing posted since
    with pytest.raises(StaleProposal, match="preserved"):
        apply_proposal(fake_crm, proposals[loser])
    assert fake_crm.writes == [] and fake_crm.get_account(loser)["status"] == "Active"


def test_a_retired_record_at_the_address_does_not_block_creating_the_current_account(fake_crm, proposals):
    # A CHOW predecessor that stays under the parent must not be mistaken for the facility's current account.
    new = proposals["bellhaven-of-batavia"]["payload"]["new_account"]
    fake_crm.create_account({**new, "name": "Old Batavia record", "chow_current_account": "001SOMEWHEREELSE"})
    fake_crm.writes.clear()
    result = apply_proposal(fake_crm, proposals["bellhaven-of-batavia"])
    assert "created" in result and [w["method"] for w in fake_crm.writes] == ["POST"]
