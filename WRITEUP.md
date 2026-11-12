# Writeup

> Draft prepared with Claude Code from what was actually built and run. Items marked **TODO** are mine
> to fill in after the review session (decisions taken, time spent, video link).

## Matching approach

**Address first, names second, and a name alone is never enough.**

1. A CRM account is the same facility as a website location when the **normalised street + state +
   (city or zip)** agree. Same building, same facility - regardless of what the account is called
   or who its parent is. That one rule is what finds renamed facilities ("Sunny Acres Retirement
   Home" is Bellhaven Willow Creek), facilities still filed under a previous owner ("Cedar Trail of
   Zanesville"), and every duplicate.
2. If the street cannot decide (the CRM holds a PO box, or a typo), a **near-identical name + same
   city, state and zip** counts only with corroboration: the PO box itself, the phone, or the
   website's administrator being a contact on the account. That is how Bellhaven of Ashtabula
   (billing street "PO Box 517") is matched.
3. Everything else is not a match, however similar the name. Near-misses are shown to the reviewer
   as "considered and rejected" so the reasoning is visible: "Amberly Manor" in Colorado Springs is
   not Amberly Manor in Hudson, OH; "Union Square Senior Living" at 240 Market St is not Bellhaven
   at Union Square at 118 Union Square Dr; "Bellhaven of New Carlisle" (OH) is not "Bellhaven of
   Carlisle" (PA).

When several records match one location, one survives and the rest are resolved against it. The
survivor is picked by one ranking (already under Bellhaven > movable under the SOP > Active > has
billing history > website administrator is a contact > phone matches > more contacts > no parent).
If the best two are separated by nothing stronger than the last criterion, the proposal is labelled
ambiguous instead of pretending to be sure.

Result on the sandbox: 35 website locations (34 in the directory + 1 linked only from the home
page), 17 confident matches, 14 matches needing a fix, 4 with no CRM account, and 3 accounts under
the Bellhaven parent that are no longer on the website - 28 proposals in total.

## Choices for outcomes that are not simple field updates

**Change of ownership with billing exposure (the SOP).** Bellhaven of Tiffin and Bellhaven of
Marietta sit under Cedar Trail and have both revenue history and outstanding AR, so their parent is
not touched. A new account is created under Bellhaven from the website data and the old account
gets `chow_current_account` = the new id - and nothing else. I read "leave the existing account
exactly as it is" literally: no rename, no status change, no note on the old record; the
explanatory note goes on the new account. Findlay (no parent, revenue but no AR), Lima (Harborview,
revenue but no AR) and Zanesville (Cedar Trail, no revenue) are re-parented directly.
The rule is one function (`sop.must_preserve`) used both when planning and again on the live record
at write time, so a proposal that went stale cannot break it.

**A facility that left Bellhaven and already has an account under the new owner.** Bellhaven of
Sandusky is not on the website; "Millstone Care of Sandusky" under Millstone Health Partners has the
same address. Sandusky has revenue and open AR, so the SOP applies in the outbound direction: the
old account is preserved untouched and `chow_current_account` points at the Millstone account.
No new account is created because the account under the correct parent already exists.

**A facility that left Bellhaven with no trace of where it went.** Bellhaven Care Center of Alliance
and Bellhaven of Coldwater are under the Bellhaven parent, not on the website, and nothing else in
the CRM shares their address. I set `status = Needs Review` with a note, and leave the parent
alone. Not Inactive: the building has most likely been sold, not closed, so it is still a prospect -
just not one to route through Bellhaven's corporate office. Not un-parented: that would assert
"independent", which nobody has confirmed, and would erase the only clue about where it came from.

**Duplicates.** The losing copy gets `duplicate_of_account` = survivor, `status = Inactive` and a
note, per the brief. Its parent is left as it was - it is a dead record, and the old parent is part
of its history. Records under a previous owner for a facility that already has a Bellhaven account
(Port Clinton, Erie, Monroe x2) are treated as duplicates rather than CHOW cases because none has
revenue or AR: the SOP would allow re-parenting them, but that would only produce a second Bellhaven
record for the same building. If such a record did have revenue and open AR, the code preserves it
and links it with `chow_current_account` instead of deactivating it.

**Three records, none under Bellhaven, nothing to tell them apart (Kettering).** One has no parent,
one is under Cedar Trail, one under Harborview; none has revenue, AR, contacts or a matching phone.
The tool picks the unparented record as the survivor (re-parent + rename), marks the other two as
duplicates, flags all three proposals as ambiguous, and lets the reviewer pin a different survivor
in `overrides.json`. **TODO: state which one I kept and why.**

**PO box (Ashtabula).** Matched with high confidence on name, city, zip, phone and administrator.
I did not overwrite the PO box with the website's street: the field is a *billing* street, and a PO
box is a legitimate billing address.

**Zip typo (Portsmouth).** Same street, city, state, name, phone and administrator, zip 45626 vs
45662 - a transposition. Corrected, because here the billing address *is* the physical address.

**Names.** Three accounts carry a pre-rebrand name and are renamed. Four differ only in spelling
("Rehab and Nursing" vs "Rehabilitation & Nursing", "of" vs "at", a missing "The"). I surface those
as a separate low-stakes category rather than silently ignoring or silently changing them.
**TODO: say which I approved / rejected.**

**Left alone on purpose.** Phone numbers and contacts are used as matching evidence only; the brief
scopes the scrape to name, address and care offerings, and a website front-desk number is not
obviously better than the number billing has on file. Accounts under other parents that merely
resemble a Bellhaven community are never touched.

## How I used AI tools

I built this with Claude Code and directed it rather than accepting its first answer. **TODO: adjust
this section so it reflects my own direction and checks.**

- **Exploration before code.** Claude read the website and the API read-only, and mapped all 35
  locations against all 121 accounts by hand before writing a matcher. That is where the site's
  traps surfaced (the page clamp, the community missing from the directory) and where the scenario
  list came from.
- **API contract without side effects.** The docs do not describe request bodies, so the write
  contract was learned from requests designed to be rejected (empty bodies, an invalid status) and
  the account was re-read afterwards to confirm nothing had changed.
- **Independent cross-check.** Three AI reviewers were given only the raw data and the brief (not
  my plan) and each derived the expected end state; a fourth reconciled them. I compared that with
  the tool's 28 proposals. **TODO: note where they disagreed and what I decided.**
- **Adversarial code review.** Five reviewers each attacked one risk area (SOP and matching,
  re-run safety, write safety, scraper, app and CI); a second agent then tried to refute each
  finding before I acted on it.
- **Tests as the check on the AI.** 64 tests, including one that approves the whole queue against
  a fake CRM built from the real sandbox seed and asserts the next run proposes nothing.
- **Me.** Every write to the CRM was a proposal I read and approved in the review app.

## What I would build next

1. **Contacts.** The website names each administrator; the CRM's administrator contacts are stale
   for several communities. Propose contact updates the same way accounts are handled.
2. **Find the new owner.** For facilities that leave an operator, look at other operators' sites
   and public facility registries so "Needs Review" becomes "moved to X".
3. **A grace period for removals.** Track first-seen / last-seen per location and only flag an
   account after it has been missing for N consecutive runs.
4. **Survivor picker in the UI** for ambiguous duplicate groups, instead of `overrides.json`.
5. **Hosted review app** with sign-in and a shared database, and a notification (Slack / email)
   when a run produces new proposals or fails.
6. **More operators.** The operator name, parent account, brand words and care-type map are already
   configuration; the scraper would become one adapter per site.
7. **Proper address normalisation** (USPS / libpostal) once the address formats get messier than
   this dataset.

## Time spent

**TODO: actual time.**

## Demo video

**TODO: link.**
