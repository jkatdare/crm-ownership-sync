"""The matcher against the real (untouched) sandbox seed, plus hand-built cases for branches the seed lacks."""
from collections import Counter

from conftest import PARENT_NAME, account
from ownership_sync.matcher import build_plan
from ownership_sync.scraper import Location
from ownership_sync.sop import must_preserve

BELLHAVEN = "0015QAPLGS3FVYEEEM"


def plan_for(locations, accounts, contacts, pins=None):
    return build_plan(locations, accounts, contacts, PARENT_NAME, pins)


def by_account(plan):
    return {p["account_id"]: p for p in plan["proposals"] if p["account_id"]}


def match(plan, slug):
    return next(m for m in plan["matches"] if m["slug"] == slug)


def changes(proposal):
    """The fields a proposal sets, leaving aside the explanatory note."""
    return {k: v for k, v in proposal["payload"]["fields"].items() if k != "note"}


# ----------------------------------------------------------------- the real seed
def test_seed_overview(locations, seed_accounts, seed_contacts):
    plan = plan_for(locations, seed_accounts, seed_contacts)
    assert Counter(m["classification"] for m in plan["matches"]) == {
        "confident_match": 17, "match_needs_fix": 14, "no_crm_account": 4}
    assert Counter(p["category"] for p in plan["proposals"]) == {
        "duplicate": 7, "new_account": 4, "name_variation": 4, "wrong_parent": 4,
        "outdated_name": 3, "chow": 3, "not_on_website": 2, "data_fix": 1, "address_note": 1}
    assert len({p["fingerprint"] for p in plan["proposals"]}) == len(plan["proposals"])


def test_sop_accounts_with_revenue_and_open_ar_are_never_reparented(locations, seed_accounts, seed_contacts):
    plan = plan_for(locations, seed_accounts, seed_contacts)
    seed = {a["account_id"]: a for a in seed_accounts}
    for p in plan["proposals"]:
        if "parent_id" in p["payload"].get("fields", {}):
            assert not must_preserve(seed[p["account_id"]]), p["title"]


def test_wrong_parent_with_revenue_and_ar_goes_the_chow_route(locations, seed_accounts, seed_contacts):
    proposals = by_account(plan_for(locations, seed_accounts, seed_contacts))
    for old_id, name in (("001U6RW32TY0WSXZZB", "Bellhaven of Tiffin"), ("001A34WFSUYHCRBLFT", "Bellhaven of Marietta")):
        p = proposals[old_id]
        assert p["kind"] == "chow" and p["payload"]["link_to"] is None
        assert p["payload"]["new_account"]["name"] == name
        assert p["payload"]["new_account"]["parent_id"] == BELLHAVEN
        assert "fields" not in p["payload"]  # nothing else is written to the old account


def test_wrong_parent_without_open_ar_is_reparented_directly(locations, seed_accounts, seed_contacts):
    proposals = by_account(plan_for(locations, seed_accounts, seed_contacts))
    assert changes(proposals["001LGFPBJY4N9MB6KL"]) == {"parent_id": BELLHAVEN}  # Lima: revenue, AR 0
    assert changes(proposals["001UKEFGADQ8YCZ4YM"]) == {"parent_id": BELLHAVEN}  # Findlay: no parent at all
    assert changes(proposals["001H1JMVZWP46D5VUF"]) == {  # Zanesville: wrong parent and old name
        "parent_id": BELLHAVEN, "name": "Bellhaven of Zanesville"}
    # what the change replaces is kept in the note, so the ownership trail is not erased
    note = proposals["001H1JMVZWP46D5VUF"]["payload"]["fields"]["note"]
    assert "parent was Cedar Trail Communities" in note and "name was “Cedar Trail of Zanesville”" in note


def test_sold_facility_links_to_the_existing_account_under_the_new_owner(locations, seed_accounts, seed_contacts):
    p = by_account(plan_for(locations, seed_accounts, seed_contacts))["001SXSF4ELF0Z2LGDM"]  # Bellhaven of Sandusky
    assert p["kind"] == "chow"
    assert p["payload"]["link_to"] == "0017JP8Z1UQ763BVK3"  # Millstone Care of Sandusky
    assert p["payload"]["new_account"] is None  # the account under the right parent already exists


def test_unlisted_accounts_with_unknown_new_owner_are_flagged_not_deactivated(locations, seed_accounts, seed_contacts):
    proposals = by_account(plan_for(locations, seed_accounts, seed_contacts))
    for account_id in ("00116ETS45BL7DTQP7", "0016PVXH4B25HWR7QE"):  # Alliance, Coldwater
        fields = proposals[account_id]["payload"]["fields"]
        assert fields["status"] == "Needs Review" and "parent_id" not in fields and fields["note"]


def test_duplicates_point_at_the_survivor_and_go_inactive(locations, seed_accounts, seed_contacts):
    proposals = by_account(plan_for(locations, seed_accounts, seed_contacts))
    expected = {
        "001QU150PM4Z15UA71": "001EGU7BMJ942ZTRE6",  # second Owosso -> the one with the website's phone + administrator
        "001JD2MWRA74LTSN24": "001UELXDAKFRKB8932",  # Harborview Port Clinton -> Bellhaven Port Clinton
        "001BLYF02K97SZLZHH": "001CVBBCSDM7YHN220",  # Harborview Erie -> Bellhaven Erie
        "0011AB44D05WLA9HTX": "001U1750VLVJAGG1S5",  # Cedar Trail Monroe -> Bellhaven Monroe
        "00159PL81N38KM4FHM": "001U1750VLVJAGG1S5",  # Harborview Monroe -> Bellhaven Monroe
    }
    for loser, survivor in expected.items():
        fields = proposals[loser]["payload"]["fields"]
        assert (fields["duplicate_of_account"], fields["status"]) == (survivor, "Inactive")
        assert "parent_id" not in fields
    for survivor in set(expected.values()):
        assert survivor not in proposals  # the surviving records need no change


def test_three_records_none_under_bellhaven_is_flagged_ambiguous(locations, seed_accounts, seed_contacts):
    plan = plan_for(locations, seed_accounts, seed_contacts)
    proposals = by_account(plan)
    survivor = "0016KTS1UAWBRXS09J"
    assert match(plan, "bellhaven-of-kettering")["confidence"] == "medium"
    assert changes(proposals[survivor]) == {"parent_id": BELLHAVEN, "name": "Bellhaven of Kettering"}
    assert proposals[survivor]["confidence"] == "medium"
    assert "tie-break" in proposals[survivor]["payload"]["fields"]["note"]
    for loser in ("001B7XZAA3AFALS9GP", "001WR41PYNWXCAE2X4"):
        assert proposals[loser]["payload"]["fields"]["duplicate_of_account"] == survivor


def test_reviewer_can_pin_a_different_survivor(locations, seed_accounts, seed_contacts):
    pinned = "001B7XZAA3AFALS9GP"
    proposals = by_account(plan_for(locations, seed_accounts, seed_contacts, pins={"bellhaven-of-kettering": pinned}))
    assert proposals[pinned]["payload"]["fields"]["parent_id"] == BELLHAVEN
    assert proposals["0016KTS1UAWBRXS09J"]["payload"]["fields"]["duplicate_of_account"] == pinned


def test_decoys_are_never_touched(locations, seed_accounts, seed_contacts):
    plan = plan_for(locations, seed_accounts, seed_contacts)
    touched = set(by_account(plan))
    decoys = {
        "0015D74ZLRY810RGY5",  # "Amberly Manor" - same name, but Colorado Springs
        "001GNU41AVXZRLLJ9P",  # "Union Square Senior Living" - same town and zip, different street
        "0019M1Z45PGV2DJ971",  # "Maplewood Senior Care Center" - same town and zip, different street
    }
    assert not touched & decoys
    for slug in ("amberly-manor", "bellhaven-at-union-square", "bellhaven-of-carlisle", "bellhaven-of-batavia"):
        assert match(plan, slug)["classification"] == "no_crm_account"
    # ...and the decoys are shown to the reviewer as considered-and-rejected evidence
    creates = {p["location_slug"]: p for p in plan["proposals"] if p["kind"] == "create"}
    assert creates["amberly-manor"]["evidence"]["others"][0]["account"]["account_id"] == "0015D74ZLRY810RGY5"
    assert creates["bellhaven-at-union-square"]["confidence"] == "medium"


def test_only_bellhaven_related_accounts_are_touched(locations, seed_accounts, seed_contacts):
    plan = plan_for(locations, seed_accounts, seed_contacts)
    assert len(by_account(plan)) == 25  # 29 proposals minus the 4 brand-new accounts
    seed = {a["account_id"]: a for a in seed_accounts}
    for account_id in by_account(plan):
        a = seed[account_id]
        on_site = any(m["account_id"] == account_id for m in plan["matches"])
        assert on_site or a["parent_id"] == BELLHAVEN or any(
            p["payload"].get("fields", {}).get("duplicate_of_account") for p in plan["proposals"] if p["account_id"] == account_id)


def test_po_box_account_is_matched_and_its_billing_street_is_never_overwritten(locations, seed_accounts, seed_contacts):
    plan = plan_for(locations, seed_accounts, seed_contacts)
    m = match(plan, "bellhaven-of-ashtabula")
    assert (m["account_id"], m["classification"], m["basis"]) == ("001NXP9X46CWEPSLSV", "confident_match", "name + city + zip")
    p = by_account(plan)["001NXP9X46CWEPSLSV"]  # only an optional note recording the physical address
    assert (p["category"], set(p["payload"]["fields"])) == ("address_note", {"note"})
    assert "3156 W Prospect Rd" in p["payload"]["fields"]["note"] and "PO Box 517" in p["payload"]["fields"]["note"]


def test_zip_typo_is_corrected_and_names_are_aligned(locations, seed_accounts, seed_contacts):
    proposals = by_account(plan_for(locations, seed_accounts, seed_contacts))
    assert changes(proposals["001CF3LDWVRGL09P4F"]) == {"billing_zip": "45662"}
    assert "zip was 45626" in proposals["001CF3LDWVRGL09P4F"]["payload"]["fields"]["note"]
    assert proposals["0013NUZQHQUEZ8DXEG"]["category"] == "outdated_name"  # Chesterton Senior Commons
    assert proposals["001TZCSTBZFM6K5BGN"]["category"] == "name_variation"  # Rehab and Nursing of Grove City


# ----------------------------------------------------------------- hand-built cases the seed does not contain
PARENT = account("P1", PARENT_NAME)
OTHER = account("P2", "Other Operator (Parent Account)")
LOC = Location(slug="bellhaven-of-testville", url="https://x/communities/bellhaven-of-testville", name="Bellhaven of Testville",
               street="10 Oak Street", city="Testville", state="OH", zip="44000", care_offerings=["Assisted Living"],
               administrator="Ann Example", phone="(555) 010-0001")
AT = dict(billing_street="10 Oak St", billing_city="Testville", billing_state="OH", billing_zip="44000")


def test_previous_owner_record_with_open_ar_is_linked_not_deactivated():
    current = account("A1", "Bellhaven of Testville", "P1", parent_name=PARENT_NAME, **AT)
    old = account("A2", "Testville Care", "P2", parent_name=OTHER["name"], lifetime_revenue=5000, outstanding_ar=100, **AT)
    plan = plan_for([LOC], [PARENT, OTHER, current, old], [])
    (p,) = plan["proposals"]
    assert (p["kind"], p["account_id"], p["payload"]["link_to"], p["payload"]["new_account"]) == ("chow", "A2", "A1", None)


def test_unlisted_account_without_billing_is_marked_superseded_by_the_new_owners_record():
    left = account("A1", "Bellhaven of Elsewhere", "P1", parent_name=PARENT_NAME, **AT)
    new_owner = account("A2", "Other of Elsewhere", "P2", parent_name=OTHER["name"], **AT)
    plan = plan_for([], [PARENT, OTHER, left, new_owner], [])
    (p,) = plan["proposals"]
    assert p["payload"]["fields"]["duplicate_of_account"] == "A2" and p["payload"]["fields"]["status"] == "Inactive"


def test_resolved_records_are_ignored_on_later_runs():
    current = account("A1", "Bellhaven of Testville", "P1", parent_name=PARENT_NAME, **AT)
    chow_old = account("A2", "Bellhaven of Testville", "P2", parent_name=OTHER["name"], lifetime_revenue=5000,
                       outstanding_ar=100, chow_current_account="A1", **AT)
    dup = account("A3", "Testville Care", "P2", parent_name=OTHER["name"], status="Inactive", duplicate_of_account="A1", **AT)
    flagged = account("A4", "Bellhaven of Gone", "P1", parent_name=PARENT_NAME, status="Needs Review",
                      billing_street="1 Far Rd", billing_city="Gone", billing_state="OH", billing_zip="44999")
    plan = plan_for([LOC], [PARENT, OTHER, current, chow_old, dup, flagged], [])
    assert plan["proposals"] == []
    assert match(plan, LOC.slug)["classification"] == "confident_match"


def test_inactive_account_for_a_live_community_is_reactivated():
    sleeping = account("A1", "Bellhaven of Testville", "P1", parent_name=PARENT_NAME, status="Inactive", **AT)
    (p,) = plan_for([LOC], [PARENT, sleeping], [])["proposals"]
    assert changes(p) == {"status": "Active"}


def test_a_facility_with_child_accounts_is_still_a_facility_and_company_records_are_not():
    campus = account("A1", "Bellhaven of Testville", "P1", parent_name=PARENT_NAME, **AT)
    wing = account("A2", "Testville Memory Wing", "A1", billing_street="12 Oak St", billing_city="Testville",
                   billing_state="OH", billing_zip="44000")  # hangs off the campus account
    subsidiary = account("P3", "Acquired Operator (Parent Account)", "P1", parent_name=PARENT_NAME)  # no address
    plan = plan_for([LOC], [PARENT, OTHER, campus, wing, subsidiary], [])
    assert match(plan, LOC.slug)["account_id"] == "A1"  # matched, not re-created
    assert plan["proposals"] == [] and plan["unlisted"] == []  # the company record is not flagged as a missing facility


# ----------------------------------------------------------------- edge cases raised in review
OTHER_AT = dict(parent_id="P2", parent_name=OTHER["name"], **AT)


def titles(plan):
    return [p["title"] for p in plan["proposals"]]


def test_a_survivor_that_approved_duplicates_point_at_is_never_demoted(locations, seed_accounts, seed_contacts):
    # One Kettering duplicate has already been approved against the default survivor...
    survivor, approved_loser, other = "0016KTS1UAWBRXS09J", "001WR41PYNWXCAE2X4", "001B7XZAA3AFALS9GP"
    accounts = [dict(a) for a in seed_accounts]
    next(a for a in accounts if a["account_id"] == approved_loser).update(duplicate_of_account=survivor, status="Inactive")
    # ...and only then does someone pin a different survivor. Following the pin would leave a duplicate chain.
    proposals = by_account(plan_for(locations, accounts, seed_contacts, pins={"bellhaven-of-kettering": other}))
    assert changes(proposals[survivor])["parent_id"] == BELLHAVEN
    assert changes(proposals[other])["duplicate_of_account"] == survivor


def test_facility_with_a_blank_street_is_matched_not_recreated_and_company_hq_is_never_a_candidate():
    blank = account("A1", "Bellhaven of Testville", "P1", parent_name=PARENT_NAME, billing_city="Testville",
                    billing_state="OH", billing_zip="44000", phone="(555) 010-0001", lifetime_revenue=90000, outstanding_ar=4000)
    hq = account("P2", "Other Operator (Parent Account)", **AT)  # a company record whose HQ shares the address
    child = account("A9", "Some Other Facility", "P2", billing_street="9 Far Rd", billing_city="Elsewhere",
                    billing_state="OH", billing_zip="44999")
    plan = plan_for([LOC], [PARENT, hq, child, blank], [{"account_id": "A1", "name": "Ann Example", "title": "Administrator"}])
    assert match(plan, LOC.slug)["account_id"] == "A1"
    assert not any(p["kind"] == "create" or p["account_id"] == "P2" for p in plan["proposals"])


def test_matched_account_left_in_needs_review_is_proposed_back_to_active():
    relisted = account("A1", "Bellhaven of Testville", "P1", parent_name=PARENT_NAME, status="Needs Review", **AT)
    (p,) = plan_for([LOC], [PARENT, relisted], [])["proposals"]
    assert changes(p) == {"status": "Active"} and "status was Needs Review" in p["payload"]["fields"]["note"]


def test_frozen_record_is_linked_only_once_the_survivor_is_really_under_bellhaven():
    frozen = account("A1", "Testville Care", lifetime_revenue=5000, outstanding_ar=100, **OTHER_AT)
    twin = account("A2", "Testville Nursing", **AT)  # unparented, movable
    first = plan_for([LOC], [PARENT, OTHER, frozen, twin], [])
    assert [p["account_id"] for p in first["proposals"]] == ["A2"]  # re-parent the twin; no link to a non-Bellhaven record
    twin.update(parent_id="P1", parent_name=PARENT_NAME, name=LOC.name)  # the re-parent was approved
    (link,) = plan_for([LOC], [PARENT, OTHER, frozen, twin], [])["proposals"]
    assert (link["kind"], link["account_id"], link["payload"]["link_to"]) == ("chow", "A1", "A2")


def test_flagged_account_is_linked_when_the_new_owners_record_appears_later():
    gone = account("A1", "Bellhaven of Gone", "P1", parent_name=PARENT_NAME, status="Needs Review",
                   lifetime_revenue=130000, outstanding_ar=5200, **AT)
    assert plan_for([], [PARENT, OTHER, gone], [])["proposals"] == []  # flagged, nothing new to say
    new_owner = account("A2", "Other Care of Gone", **OTHER_AT)
    (p,) = plan_for([], [PARENT, OTHER, gone, new_owner], [])["proposals"]
    assert (p["kind"], p["payload"]["link_to"]) == ("chow", "A2")


def test_several_possible_successors_is_a_medium_confidence_tie_break_showing_all_of_them():
    left = account("A1", "Bellhaven of Elsewhere", "P1", parent_name=PARENT_NAME, **AT)
    one, two = account("A2", "Other of Elsewhere", **OTHER_AT), account("A3", "Third of Elsewhere", **AT)
    (p,) = plan_for([], [PARENT, OTHER, left, one, two], [])["proposals"]
    assert p["confidence"] == "medium" and len(p["evidence"]["others"]) == 2
    assert p["payload"]["fields"]["duplicate_of_account"] == "A2"  # the one that has a parent


def test_same_parent_duplicate_with_open_ar_is_flagged_once_then_left_alone():
    keep = account("A1", "Bellhaven of Testville", "P1", parent_name=PARENT_NAME, lifetime_revenue=9000, outstanding_ar=50,
                   phone="(555) 010-0001", **AT)
    twin = account("A2", "Bellhaven of Testville", "P1", parent_name=PARENT_NAME, lifetime_revenue=7000, outstanding_ar=40, **AT)
    (flag,) = plan_for([LOC], [PARENT, keep, twin], [])["proposals"]
    assert (flag["account_id"], changes(flag)) == ("A2", {"status": "Needs Review"})  # never deactivated
    twin.update(status="Needs Review")  # approved
    assert plan_for([LOC], [PARENT, keep, twin], [])["proposals"] == []  # converges


def test_duplicate_with_revenue_history_is_worded_honestly_and_not_high_confidence():
    current = account("A1", "Bellhaven of Testville", "P1", parent_name=PARENT_NAME, **AT)
    old = account("A2", "Testville Care", lifetime_revenue=47000, outstanding_ar=0, **OTHER_AT)
    (p,) = plan_for([LOC], [PARENT, OTHER, current, old], [])["proposals"]
    assert p["confidence"] == "medium" and "has revenue history but no outstanding AR" in p["evidence"]["reason"]
    assert "no billing history is lost" not in p["evidence"]["reason"]


def test_blank_zips_do_not_make_two_addresses_the_same_place():
    springfield = account("A1", "Bellhaven of Springfield", "P1", parent_name=PARENT_NAME, billing_street="100 Main St",
                          billing_city="Springfield", billing_state="OH", billing_zip="")
    dayton = account("A2", "Maple Care of Dayton", "P2", parent_name=OTHER["name"], billing_street="100 Main Street",
                     billing_city="Dayton", billing_state="OH", billing_zip="")
    (p,) = plan_for([], [PARENT, OTHER, springfield, dayton], [])["proposals"]
    assert p["category"] == "not_on_website"  # flagged for review, not "superseded by" an unrelated record


def test_unparented_facility_with_child_accounts_is_matched_not_recreated():
    campus = account("A1", "Testville Care Campus", **AT)  # no parent, but another record hangs off it
    wing = account("A2", "Testville Memory Wing", "A1", billing_street="12 Oak St", billing_city="Testville",
                   billing_state="OH", billing_zip="44000")
    plan = plan_for([LOC], [PARENT, OTHER, campus, wing], [])
    assert match(plan, LOC.slug)["account_id"] == "A1"
    assert [p["kind"] for p in plan["proposals"]] == ["patch"]  # re-parent and rename it; never a second account
