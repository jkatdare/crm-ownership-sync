import pytest

from ownership_sync.normalize import is_po_box, name_similarity, norm_street


@pytest.mark.parametrize("website, crm", [
    ("4850 NW Sylvania Ave", "4850 Northwest Sylvania Avenue"),
    ("199 Barks Rd W", "199 Barks Road West"),
    ("3313 Wilmington Pike", "3313 Wilmington Pk"),
    ("1250 NW Franklin Street", "1250 Northwest Franklin St"),
    ("210 Orchard Lane", "210 Orchard Ln"),
    ("1125 Logan Blvd", "1125 Logan Boulevard"),
    ("45 St Lawrence Dr", "45 St. Lawrence Drive"),
])
def test_same_street_written_differently_is_equal(website, crm):
    assert norm_street(website) == norm_street(crm)


def test_different_streets_stay_different():
    assert norm_street("118 Union Square Dr") != norm_street("240 Market St")
    assert norm_street("210 Orchard Lane") != norm_street("431 Main St")


@pytest.mark.parametrize("street, expected", [
    ("PO Box 517", True), ("P.O. Box 12", True), ("p o box 9", True), ("517 Box Elder Rd", False), ("", False),
])
def test_po_box(street, expected):
    assert is_po_box(street) is expected


@pytest.mark.parametrize("website, crm", [
    ("Bellhaven Rehabilitation & Nursing of Grove City", "Bellhaven Rehab and Nursing of Grove City"),
    ("Bellhaven Healthcare Centre of Ashland", "Bellhaven Health Care Center of Ashland"),
    ("The Arbors at Bellhaven - Dayton", "Arbors at Bellhaven Dayton"),
    ("Bellhaven at Sycamore Ridge", "Bellhaven of Sycamore Ridge"),
])
def test_cosmetic_name_variants_are_identical(website, crm):
    assert name_similarity(website, crm) == 1.0


@pytest.mark.parametrize("website, crm", [
    ("Bellhaven of Chesterton", "Chesterton Senior Commons"),
    ("Bellhaven Willow Creek", "Sunny Acres Retirement Home"),
    ("Bellhaven of Carlisle", "Bellhaven of New Carlisle"),
    ("Bellhaven at Union Square", "Union Square Senior Living"),
])
def test_genuinely_different_names_score_low(website, crm):
    assert name_similarity(website, crm) < 0.8
