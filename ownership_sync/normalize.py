"""Text normalisation used for matching. Pure functions, no I/O."""
import re

from .config import BRAND_WORDS

# Street-type and direction words collapse to one canonical abbreviation, so that
# "4850 Northwest Sylvania Avenue" and "4850 NW Sylvania Ave" compare equal.
_STREET_WORDS = {
    "street": "st", "avenue": "ave", "av": "ave", "road": "rd", "drive": "dr", "lane": "ln",
    "boulevard": "blvd", "pike": "pk", "court": "ct", "place": "pl", "parkway": "pkwy",
    "highway": "hwy", "circle": "cir", "terrace": "ter", "trail": "trl", "saint": "st",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
}

# Spelling variants that should not count as a different facility name.
_NAME_SYNONYMS = {"rehab": "rehabilitation", "centre": "center", "ctr": "center", "healthcare": "health care"}
_NAME_NOISE = {"the", "of", "at", "and"} | BRAND_WORDS


def _tokens(text):
    return re.sub(r"[^a-z0-9 ]", " ", (text or "").lower().replace("&", " and ")).split()


def norm_text(text):
    """Case/punctuation/whitespace-insensitive form, for cities and people's names."""
    return " ".join(_tokens(text))


def norm_street(street):
    return " ".join(_STREET_WORDS.get(t, t) for t in _tokens(street))


def is_po_box(street):
    return re.sub(r"[^a-z0-9]", "", (street or "").lower()).startswith("pobox")


def phone_digits(phone):
    return re.sub(r"\D", "", phone or "")[-10:]


def name_tokens(name):
    """Distinctive words of a facility name: synonyms unified, brand and filler words dropped."""
    words = " ".join(_NAME_SYNONYMS.get(t, t) for t in _tokens(name)).split()
    return {w for w in words if w not in _NAME_NOISE}


def name_similarity(a, b):
    """Jaccard overlap of distinctive name words, 0.0 - 1.0.

    1.0 = same words ("Bellhaven Rehab and Nursing of Grove City" vs
    "Bellhaven Rehabilitation & Nursing of Grove City"); low = a genuinely different name.
    """
    ta, tb = name_tokens(a), name_tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0
