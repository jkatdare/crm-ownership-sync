"""Match website locations to CRM accounts and turn every difference into a proposal.

build_plan() is a pure function of its inputs (no network, no database). The same website and CRM
snapshot always produce the same proposals, which is what makes daily re-runs safe.

How a location is matched (see compare()):
  1. ADDRESS  - same normalised street + state + (city or zip). Same building = same facility,
                whatever the account is called and whoever its parent is.
  2. NAME     - only if the street can't decide (PO box, typo): near-identical name + same city,
                state and zip, AND a corroborating signal (PO box, phone, or administrator contact).
  A similar name on its own is never a match - that is how the decoys are kept out.

How a group of matching accounts is resolved (see Planner.plan_location()):
  one record survives and is fixed up (parent, name, zip ...); every other record for the same
  facility is marked as its duplicate - unless the SOP says the record must be preserved.
"""
import hashlib
import json
from collections import defaultdict
from dataclasses import asdict

from .config import CARE_TYPE_MAP
from .normalize import is_po_box, name_similarity, norm_street, norm_text, phone_digits
from .sop import must_preserve, sop_evidence

NAME_MATCH = 0.8  # name similarity needed for a name-based match, and to call a rename "minor"
NEAR_MISS = 0.5  # an unmatched account this similar is shown to the reviewer as "considered, rejected"

SNAPSHOT_FIELDS = (
    "account_id", "name", "parent_id", "parent_name", "billing_street", "billing_city", "billing_state",
    "billing_zip", "care_type", "status", "phone", "lifetime_revenue", "outstanding_ar",
    "chow_current_account", "duplicate_of_account", "note", "updated_at",
)


def snapshot(account):
    return {k: account.get(k) for k in SNAPSHOT_FIELDS}


def compare(loc, account, contacts):
    """Evidence that a CRM account is (or is not) the facility at a website location."""
    s = {
        "street": bool(norm_street(loc.street)) and norm_street(loc.street) == norm_street(account["billing_street"]),
        "city": norm_text(loc.city) == norm_text(account["billing_city"]),
        "state": loc.state.upper() == (account["billing_state"] or "").upper(),
        "zip": loc.zip == (account["billing_zip"] or "")[:5],
        "po_box": is_po_box(account["billing_street"]),
        "name_similarity": round(name_similarity(loc.name, account["name"]), 2),
        "phone": bool(phone_digits(loc.phone)) and phone_digits(loc.phone) == phone_digits(account["phone"]),
        "administrator": bool(loc.administrator)
        and any(norm_text(c["name"]) == norm_text(loc.administrator) for c in contacts),
    }
    if s["street"] and s["state"] and (s["city"] or s["zip"]):
        basis = "address"
    elif (s["city"] and s["state"] and s["zip"] and s["name_similarity"] >= NAME_MATCH
          and (s["po_box"] or s["phone"] or s["administrator"])):
        basis = "name + city + zip"
    else:
        basis = None
    return {
        "account": account,
        "signals": s,
        "basis": basis,
        "contacts": [f"{c['name']} ({c['title']})" for c in contacts],
    }


def owner_of(account):
    """Short name of an account's parent company, for wording."""
    return (account.get("parent_name") or "").replace(" (Parent Account)", "") or "no parent"


def same_site(a, b):
    """Two CRM accounts at the same physical address."""
    street = norm_street(a["billing_street"])
    return (bool(street) and street == norm_street(b["billing_street"])
            and a["billing_state"] == b["billing_state"]
            and (a["billing_zip"] == b["billing_zip"] or norm_text(a["billing_city"]) == norm_text(b["billing_city"])))


def fingerprint(proposal):
    """Stable identity of a proposal: what it would change, not how it is worded.

    The ledger uses this to recognise a proposal it has already seen, so an approved or rejected
    item is never proposed again. Notes are excluded so that rewording never reopens a decision
    (unless the note is the whole change, as with a physical-address annotation).
    """
    payload = proposal["payload"]
    new = payload.get("new_account")
    fields = payload.get("fields", {})
    core = {
        "kind": proposal["kind"],
        "account_id": proposal["account_id"],
        "location": proposal["location_slug"] if proposal["kind"] == "create" else None,
        "fields": {k: v for k, v in sorted(fields.items()) if k != "note"} or dict(fields),
        "link_to": payload.get("link_to"),
        "new": {k: new[k] for k in ("name", "parent_id", "billing_street", "billing_zip")} if new else None,
    }
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]


class Planner:
    def __init__(self, accounts, contacts, parent_name, pins=None):
        parents = [a for a in accounts if a["name"] == parent_name]
        if len(parents) != 1:
            raise ValueError(f"expected exactly one CRM account named {parent_name!r}, found {len(parents)}")
        self.target = parents[0]
        self.tid = self.target["account_id"]
        self.pins = pins or {}
        self.by_id = {a["account_id"]: a for a in accounts}
        self.contacts_of = defaultdict(list)
        for c in contacts:
            if c.get("is_active", True):
                self.contacts_of[c["account_id"]].append(c)
        # Facility records only: a record with a street address is a facility, one without is a
        # company (parent) record. Records already resolved by an earlier run (marked duplicate, or a
        # CHOW predecessor) never take part in matching again - this is what lets re-runs converge.
        self.facilities = [a for a in accounts if a["account_id"] != self.tid and (a["billing_street"] or "").strip()]
        self.live = [a for a in self.facilities if not a["duplicate_of_account"] and not a["chow_current_account"]]

    # ------------------------------------------------------------------ helpers
    def label(self, account_id):
        account = self.by_id.get(account_id)
        return f"{account['name']} ({account_id})" if account else (account_id or "(none)")

    def proposal(self, kind, category, title, reason, payload, *, account=None, loc=None, confidence="high", **evidence):
        p = {
            "kind": kind,  # how it is executed: patch | create | chow
            "category": category,  # what it means to the reviewer
            "title": title,
            "confidence": confidence,
            "account_id": account["account_id"] if account else None,
            "location_slug": loc.slug if loc else None,
            "payload": payload,
            "evidence": {
                "reason": reason,
                "location": asdict(loc) if loc else None,
                "account": snapshot(account) if account else None,
                "contacts": [f"{c['name']} ({c['title']})" for c in self.contacts_of[account["account_id"]]] if account else [],
                **evidence,
            },
        }
        p["fingerprint"] = fingerprint(p)
        return p

    def patch(self, category, title, reason, account, fields, **kw):
        """A proposal that PATCHes fields on one existing account."""
        diff = []
        for key, value in fields.items():
            is_link = key in ("parent_id", "duplicate_of_account")
            diff.append({
                "field": key,
                "before": self.label(account[key]) if is_link and account[key] else (account.get(key) or "(empty)"),
                "after": self.label(value) if is_link else value,
            })
        payload = {"account_id": account["account_id"], "expect_updated_at": account["updated_at"], "fields": fields}
        # The API has no merge, so marking a record as a duplicate also carries its contacts over to
        # the surviving record - otherwise they would be stranded on an inactive account.
        contacts = self.contacts_of[account["account_id"]] if fields.get("duplicate_of_account") else []
        if contacts:
            payload["move_contacts"] = [{k: c[k] for k in ("contact_id", "name", "title")} for c in contacts]
            diff.append({
                "field": "contacts",
                "before": "; ".join(f"{c['name']} ({c['title']})" for c in contacts) + " - on this record",
                "after": f"moved to {self.label(fields['duplicate_of_account'])}",
            })
        return self.proposal("patch", category, title, reason, payload, account=account, diff=diff, **kw)

    def new_account_fields(self, loc, note):
        care = [CARE_TYPE_MAP[o] for o in loc.care_offerings if o in CARE_TYPE_MAP]
        if loc.administrator:  # a new account has no contacts yet; at least record who runs the building
            note += f" Administrator per the website: {loc.administrator}."
        return {
            "name": loc.name,
            "parent_id": self.tid,
            "billing_street": loc.street,
            "billing_city": loc.city,
            "billing_state": loc.state,
            "billing_zip": loc.zip,
            "care_type": care[0] if care else "",
            "phone": loc.phone,
            "status": "Active",
            "note": note,
        }

    def signal_bullets(self, loc, cand):
        a, s = cand["account"], cand["signals"]
        out = []
        if s["street"]:
            out.append(f"Street matches after normalisation: website “{loc.street}” = CRM “{a['billing_street']}”.")
        elif s["po_box"]:
            out.append(f"CRM billing street is a PO box (“{a['billing_street']}”), so the street cannot be compared; "
                       "matched on name + city + zip plus the corroboration below.")
        else:
            out.append(f"Street differs: website “{loc.street}” vs CRM “{a['billing_street']}”.")
        place = [n for n in ("city", "state", "zip") if s[n]]
        out.append(f"Matching {', '.join(place) or 'nothing'} out of city / state / zip.")
        out.append(f"Name similarity {s['name_similarity']:.0%}: website “{loc.name}” vs CRM “{a['name']}”.")
        if s["phone"]:
            out.append("The website phone number is on this account.")
        if s["administrator"]:
            out.append(f"The website's administrator ({loc.administrator}) is a contact on this account.")
        return out

    # ------------------------------------------------------------------ locations
    def rank(self, loc, cand):
        """Which record should survive when several match one location. Higher tuple wins."""
        a, s = cand["account"], cand["signals"]
        at_target = a["parent_id"] == self.tid
        movable = at_target or not must_preserve(a)
        return (
            movable and a["account_id"] == self.pins.get(loc.slug),  # reviewer override (overrides.json)
            at_target,  # already under the right parent
            movable,  # the SOP allows it to be re-parented
            a["status"] == "Active",
            (a["lifetime_revenue"] or 0) > 0,  # keep the record that carries the billing history
            s["administrator"],  # corroborated by the website
            s["phone"],
            len(cand["contacts"]),
            not a["parent_id"],  # weak: an unparented record over one filed under another operator
        )

    def plan_location(self, loc, cands):
        """Returns (summary row, proposals) for one website location and its matching CRM accounts."""
        row = {"slug": loc.slug, "name": loc.name, "url": loc.url, "city": f"{loc.city}, {loc.state}",
               "account_id": None, "account_name": None, "basis": None, "duplicates": 0}
        if not cands:
            p = self.create(loc)
            return {**row, "classification": "no_crm_account", "confidence": p["confidence"]}, [p]

        cands.sort(key=lambda c: c["account"]["account_id"])  # deterministic final tie-break
        cands.sort(key=lambda c: self.rank(loc, c), reverse=True)
        top, rest = cands[0], cands[1:]
        a = top["account"]
        row.update(account_id=a["account_id"], account_name=a["name"], basis=top["basis"], duplicates=len(rest))

        if a["parent_id"] != self.tid and must_preserve(a):
            # Every record for this facility is frozen by the SOP, so nothing can be re-parented:
            # the facility needs a new account under the right parent, linked from the old one.
            # (Any further frozen records are picked up on the next run, once the new account exists.)
            return {**row, "classification": "match_needs_fix", "confidence": "high"}, [self.chow_new(loc, top)]

        # Ambiguous = the best two records differ only by the weak last criterion (or nothing at all).
        ambiguous = bool(rest) and self.rank(loc, top)[:-1] == self.rank(loc, rest[0])[:-1]
        why = self.why_survivor(loc, top, ambiguous)
        proposals = []
        fix = self.fix_survivor(loc, top, rest, ambiguous, why)
        if fix:
            proposals.append(fix)
        proposals += [self.loser(loc, c, top, why, ambiguous) for c in rest]
        s = top["signals"]
        solid = top["basis"] == "address" or (s["phone"] and s["administrator"])
        confidence = "high" if solid and not ambiguous else "medium"
        # A note-only proposal (e.g. recording a physical address) annotates a correct record; it is not a fix.
        needs_fix = bool(fix) and set(fix["payload"]["fields"]) != {"note"}
        classification = "match_needs_fix" if needs_fix else "confident_match"
        return {**row, "classification": classification, "confidence": confidence}, proposals

    def why_survivor(self, loc, top, ambiguous):
        a, s = top["account"], top["signals"]
        if a["account_id"] == self.pins.get(loc.slug):
            return "it is pinned as the survivor in overrides.json"
        reasons = []
        if a["parent_id"] == self.tid:
            reasons.append("it is already under the Bellhaven parent")
        if (a["lifetime_revenue"] or 0) > 0:
            reasons.append("it carries the billing history")
        if s["administrator"]:
            reasons.append(f"the website's administrator ({loc.administrator}) is a contact on it")
        if s["phone"]:
            reasons.append("its phone matches the website")
        if ambiguous:
            return ("NO EVIDENCE SEPARATES THESE RECORDS (no billing history, contacts or phone match on any of them), "
                    "so the choice is a tie-break: the record with no parent, else the lowest account id. "
                    "To keep a different record, pin it in overrides.json and re-run")
        return " and ".join(reasons) or "it ranks highest on status, contacts and parent"

    def fix_survivor(self, loc, top, rest, ambiguous, why):
        """Bring the surviving record in line with the website. None if it is already right."""
        a, s = top["account"], top["signals"]
        fields = {}
        if a["parent_id"] != self.tid:
            fields["parent_id"] = self.tid
        if a["name"].strip() != loc.name:
            fields["name"] = loc.name
        if top["basis"] == "address":  # same street, so the rest of the address must agree with the website
            if not s["zip"]:
                fields["billing_zip"] = loc.zip
            if not s["city"]:
                fields["billing_city"] = loc.city
        care = [CARE_TYPE_MAP[o] for o in loc.care_offerings if o in CARE_TYPE_MAP]
        if care and a["care_type"] not in care:
            fields["care_type"] = care[0]
        if a["status"] == "Inactive":
            fields["status"] = "Active"

        # The CRM keeps no field history, so what a change replaces is written to the note: the previous
        # owner and the previous name are exactly the ownership trail a re-parent or rename would erase.
        was = {"parent_id": f"parent was {a['parent_name'] or 'none'}", "name": f"name was “{a['name']}”",
               "billing_zip": f"zip was {a['billing_zip']}", "billing_city": f"city was {a['billing_city']}",
               "care_type": f"care type was {a['care_type'] or 'blank'}", "status": f"status was {a['status']}"}
        notes = []
        if fields:
            notes.append("Updated to match the Bellhaven website: " + "; ".join(was[f] for f in fields) + ".")
        if fields and rest and ambiguous:
            notes.append(f"Kept as the surviving record of {len(rest) + 1} for this address by tie-break (nothing distinguished them).")
        # A PO box is a legitimate billing address, so it is never overwritten; the physical address is noted instead.
        if s["po_box"] and norm_street(loc.street) not in norm_street(a["note"]):
            notes.append(f"Physical address per the Bellhaven website: {loc.street}, {loc.city}, {loc.state} {loc.zip}. "
                         f"Billing street ({a['billing_street']}) kept as the billing address.")
        if not notes:
            return None
        annotation_only = not fields
        fields["note"] = " ".join(notes)

        minor = s["name_similarity"] >= NAME_MATCH
        sop = None
        if annotation_only:
            category = "address_note"
            title = f"Note the physical address on “{a['name']}” (billing PO box kept)"
            reason = ("Confident match with nothing to correct. The CRM holds a PO box as the billing street, which is a "
                      "valid billing address and is left alone; the website's physical address is added to the note so "
                      "it is not lost. Optional - rejecting it leaves the account exactly as it is.")
        elif "parent_id" in fields:
            category, sop = "wrong_parent", sop_evidence(a)
            title = f"Re-parent “{a['name']}” to Bellhaven" + (f" and rename it “{loc.name}”" if "name" in fields else "")
            reason = (f"The website lists this facility as a Bellhaven community, but its CRM account sits under "
                      f"{owner_of(a)}. The SOP allows a direct re-parent here (see the check below).")
        elif "name" in fields and not minor:
            category, title = "outdated_name", f"Rename “{a['name']}” to “{loc.name}”"
            reason = ("Same address as the website listing and already under Bellhaven, but the CRM still carries a "
                      "different (pre-rebrand) name.")
        elif "name" in fields:
            category, title = "name_variation", f"Rename “{a['name']}” to “{loc.name}”"
            reason = ("Same facility, same words - only the spelling differs from the website's official name. "
                      "Low stakes either way; approving makes the CRM name match the website exactly.")
        else:
            category = "data_fix"
            title = f"Correct {', '.join(f.replace('billing_', '') for f in fields if f != 'note')} on “{a['name']}”"
            reason = ("Name, street and the rest of the address match the website; the differing value looks like "
                      "a data-entry error in the CRM.")
        bullets = self.signal_bullets(loc, top)
        if rest:
            bullets.append(f"{len(rest) + 1} CRM records match this location. This one is kept because {why}.")
        others = [{"role": "other record for this facility (separate proposal)", "account": snapshot(c["account"])} for c in rest]
        solid = top["basis"] == "address" or (s["phone"] and s["administrator"])
        return self.patch(category, title, reason, a, fields, loc=loc, signals=s, bullets=bullets, sop=sop,
                          others=others, confidence="high" if solid and not ambiguous else "medium")

    def loser(self, loc, cand, top, why, ambiguous):
        """A second record for a facility that already has a surviving record."""
        a, survivor = cand["account"], top["account"]
        bullets = self.signal_bullets(loc, cand) + [f"Surviving record: {survivor['name']} ({survivor['account_id']}), kept because {why}."]
        others = [{"role": "surviving record", "account": snapshot(survivor)}]
        owner = owner_of(a)
        if must_preserve(a) and a["parent_id"] != self.tid:
            # Previous owner's record with open AR: preserve it untouched, just point it at the current account.
            return self.chow_link(a, survivor, loc=loc, bullets=bullets, others=others, reason=(
                f"This is the previous owner's record ({owner}) for a facility that now has a Bellhaven account. "
                "It has revenue history AND outstanding AR, so per the SOP it is left exactly as it is and only "
                "linked to the current account."))
        if must_preserve(a):
            fields = {"status": "Needs Review", "note": (
                f"Possible duplicate of {survivor['account_id']} ({loc.name}), but it has revenue history and open AR - "
                "billing to confirm before it is deactivated.")}
            return self.patch("duplicate", f"Flag “{a['name']}” as a possible duplicate with open AR", (
                "Second record for the same facility under the same parent, but it has revenue history and outstanding AR, "
                "so it is flagged for billing instead of being deactivated."), a, fields, loc=loc, signals=cand["signals"],
                bullets=bullets, others=others, sop=sop_evidence(a), confidence="medium")
        fields = {
            "duplicate_of_account": survivor["account_id"],
            "status": "Inactive",
            "note": f"Duplicate of {survivor['account_id']} ({loc.name}): same facility at {loc.street}, {loc.city}. "
                    f"Superseded record ({owner}).",
        }
        reason = (f"{loc.name} at {loc.street} has more than one CRM record. This copy ({owner}) has "
                  f"${a['lifetime_revenue'] or 0:,.0f} lifetime revenue and ${a['outstanding_ar'] or 0:,.0f} outstanding AR, "
                  "so no billing history is lost by deactivating it. Per the CRM convention it is marked Inactive and "
                  "pointed at the surviving record. Its parent is left unchanged; any contacts on it move to the survivor.")
        which = f"the second “{a['name']}” record ({a['account_id']})" if a["parent_id"] == survivor["parent_id"] else f"“{a['name']}” ({owner})"
        return self.patch("duplicate", f"Mark {which} as a duplicate of “{loc.name}”", reason, a, fields,
                          loc=loc, signals=cand["signals"], bullets=bullets, others=others, sop=sop_evidence(a),
                          confidence="medium" if ambiguous else "high")

    def chow_new(self, loc, top):
        a = top["account"]
        sop = sop_evidence(a)
        owner = owner_of(a)
        new_account = self.new_account_fields(loc, (
            f"Change of ownership: successor of {a['account_id']} ({owner}), which keeps the billing history per the CHOW SOP."))
        payload = {"account_id": a["account_id"], "expect_updated_at": a["updated_at"], "new_account": new_account, "link_to": None}
        reason = (f"The website lists this facility as a Bellhaven community, but its CRM account sits under {owner}. "
                  "It has revenue history AND outstanding AR, so the SOP forbids changing its parent. Instead the old "
                  "account is left exactly as it is, a new account is created under Bellhaven, and the only change to "
                  "the old account is chow_current_account = the new account's id.")
        diff = [{"field": "chow_current_account", "before": "(empty)", "after": "id of the new account created below"}]
        return self.proposal("chow", "chow", f"CHOW: create “{loc.name}” under Bellhaven and link “{a['name']}” ({owner}) to it",
                             reason, payload, account=a, loc=loc, signals=top["signals"], bullets=self.signal_bullets(loc, top),
                             sop=sop, diff=diff, new_account=new_account)

    def chow_link(self, account, successor, reason, **kw):
        payload = {"account_id": account["account_id"], "expect_updated_at": account["updated_at"],
                   "new_account": None, "link_to": successor["account_id"]}
        diff = [{"field": "chow_current_account", "before": "(empty)", "after": self.label(successor["account_id"])}]
        title = (f"CHOW: link “{account['name']}” to its current account “{successor['name']}” "
                 f"({owner_of(successor)})")
        return self.proposal("chow", "chow", title, reason, payload, account=account, sop=sop_evidence(account), diff=diff, **kw)

    def create(self, loc):
        near = []
        for a in self.live:
            similarity = name_similarity(loc.name, a["name"])
            if similarity >= NEAR_MISS:
                same_town = norm_text(a["billing_city"]) == norm_text(loc.city) and a["billing_state"] == loc.state
                where = (f"same town but a different street (“{a['billing_street']}”)" if same_town
                         else f"a different place ({a['billing_city']}, {a['billing_state']})")
                near.append((similarity, same_town, {
                    "role": "considered and rejected",
                    "account": snapshot(a),
                    "why": f"Name is {similarity:.0%} similar, but it is at {where}, and neither the phone nor the "
                           "administrator matches the website. A similar name alone is not a match.",
                }))
        near.sort(key=lambda n: (-n[0], n[2]["account"]["account_id"]))
        new_account = self.new_account_fields(loc, f"Created from the Bellhaven website listing {loc.url}.")
        reason = ("Listed on the Bellhaven website, but no CRM account exists at this address and none matches on "
                  "name + city + zip. A new account under the Bellhaven parent is needed.")
        return self.proposal("create", "new_account", f"Create “{loc.name}” under Bellhaven", reason,
                             {"new_account": new_account}, loc=loc, new_account=new_account,
                             others=[n[2] for n in near[:3]],
                             confidence="medium" if any(n[1] for n in near) else "high")

    # ------------------------------------------------------------------ accounts not on the website
    def plan_unlisted(self, a, claimed):
        """An account under the Bellhaven parent that matched no website location."""
        row = {"account_id": a["account_id"], "name": a["name"], "city": f"{a['billing_city']}, {a['billing_state']}",
               "status": a["status"]}
        if a["chow_current_account"]:
            return {**row, "state": f"resolved - CHOW link to {self.label(a['chow_current_account'])}"}, None
        if a["duplicate_of_account"]:
            return {**row, "state": f"resolved - duplicate of {self.label(a['duplicate_of_account'])}"}, None
        if a["status"] != "Active":
            return {**row, "state": f"already flagged ({a['status']})"}, None

        sop = sop_evidence(a)
        successors = [b for b in self.live if b["account_id"] != a["account_id"] and b["parent_id"] != self.tid
                      and b["account_id"] not in claimed and same_site(a, b)]
        if successors:
            b = sorted(successors, key=lambda b: (b["status"] != "Active", not b["parent_id"], b["account_id"]))[0]
            owner = owner_of(b)
            others = [{"role": f"current account for this address ({owner})", "account": snapshot(b)}]
            bullets = [f"Not found on the Bellhaven website (no listing at {a['billing_street']}, {a['billing_city']}).",
                       f"Another CRM account, “{b['name']}” under {owner}, has the same address - the facility changed hands."]
            if must_preserve(a):
                p = self.chow_link(a, b, bullets=bullets, others=others, reason=(
                    f"No longer a Bellhaven community: the same address now belongs to “{b['name']}” under {owner}. "
                    "This account has revenue history AND outstanding AR, so per the SOP it is left exactly as it is. "
                    "The account under the correct parent already exists, so no new one is created - only "
                    "chow_current_account is set, pointing at it."))
            else:
                fields = {"duplicate_of_account": b["account_id"], "status": "Inactive",
                          "note": f"No longer a Bellhaven community; superseded by {b['account_id']} ({b['name']}, {owner})."}
                p = self.patch("duplicate", f"Mark “{a['name']}” as superseded by “{b['name']}” ({owner})", (
                    f"No longer on the Bellhaven website, and the same address already has an account under {owner}. "
                    f"The SOP would allow re-parenting it, but that would just create a second {owner} record for the "
                    "same building, so this one is marked Inactive and pointed at the existing account."),
                    a, fields, bullets=bullets, others=others, sop=sop)
            return {**row, "state": "proposal"}, p

        fields = {"status": "Needs Review", "note": (
            "Not listed on the Bellhaven website and no other CRM account found at this address - likely sold or closed. "
            "Confirm the current owner before treating it as a Bellhaven facility.")}
        reason = ("Under the Bellhaven parent in the CRM, but not on Bellhaven's website, and no other CRM account shares "
                  "its address, so the new owner (if any) is unknown. It is flagged Needs Review with a note rather than "
                  "marked Inactive (the facility may well still operate under someone else) and its parent is left alone "
                  "until someone confirms where it went.")
        bullets = [f"Not found on the Bellhaven website (no listing at {a['billing_street']}, {a['billing_city']}).",
                   "No other CRM account at this address.", f"Billing: {sop['branch'].split(':')[0].lower()} is what the SOP "
                   f"would allow (revenue ${sop['lifetime_revenue']:,.0f}, AR ${sop['outstanding_ar']:,.0f}), but there is no known parent to move it to."]
        p = self.patch("not_on_website", f"Flag “{a['name']}” for review: under Bellhaven but not on the website",
                       reason, a, fields, bullets=bullets, sop=sop, confidence="medium")
        return {**row, "state": "proposal"}, p

    # ------------------------------------------------------------------ everything
    def build(self, locations):
        matches, unlisted, proposals, claimed = [], [], [], set()
        for loc in sorted(locations, key=lambda l: l.slug):
            cands = [c for c in (compare(loc, a, self.contacts_of[a["account_id"]])
                                 for a in self.live if a["account_id"] not in claimed) if c["basis"]]
            claimed |= {c["account"]["account_id"] for c in cands}
            row, props = self.plan_location(loc, cands)
            row["proposals"] = [p["fingerprint"] for p in props]
            matches.append(row)
            proposals += props
        for a in sorted(self.facilities, key=lambda a: a["name"]):
            if a["parent_id"] == self.tid and a["account_id"] not in claimed:
                row, p = self.plan_unlisted(a, claimed)
                row["proposals"] = [p["fingerprint"]] if p else []
                unlisted.append(row)
                if p:
                    proposals.append(p)
        return {"target": {"account_id": self.tid, "name": self.target["name"]},
                "matches": matches, "unlisted": unlisted, "proposals": proposals}


def build_plan(locations, accounts, contacts, parent_name, pins=None):
    return Planner(accounts, contacts, parent_name, pins).build(locations)
