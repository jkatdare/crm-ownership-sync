# Writeup

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
page): 17 confident matches, 14 matches needing a fix, 4 with no CRM account; plus 3 accounts under
the Bellhaven parent that are no longer on the website. That is 29 proposals:

| Proposals | Count | Which |
|---|---|---|
| Change of ownership (SOP) | 3 | Tiffin, Marietta (new account + link); Sandusky (link to the existing Millstone account) |
| Wrong parent, re-parented directly | 4 | Findlay, Lima, Zanesville, Kettering |
| Duplicate records | 7 | Owosso, Port Clinton, Erie, Monroe x2, Kettering x2 |
| No CRM account yet | 4 | Batavia, Carlisle, Union Square, Amberly Manor |
| Outdated name | 3 | Chesterton, Chagrin Falls, Willow Creek |
| Minor name spelling | 4 | Grove City, Sycamore Ridge, Arbors Dayton, Ashland |
| No longer on the website, new owner unknown | 2 | Alliance, Coldwater |
| Data fix | 1 | Portsmouth zip |
| Address note (optional) | 1 | Ashtabula |

## End state

I reviewed all 29 proposals in the app and approved every one; none was rejected. The full list with
timestamps is in [DECISIONS.md](DECISIONS.md) and in the app's audit log. Afterwards the CRM was
re-read and compared, field by field, with the approved plan applied to the original data:

- 127 accounts: the original 121 plus 6 created (Batavia, Carlisle, Union Square, Amberly Manor, and
  the two CHOW successors, Tiffin and Marietta).
- 25 existing accounts changed, exactly the 25 the plan names; the other 96 are untouched,
  including the three look-alike accounts under other operators.
- Each of the 35 website communities maps to exactly one live, Active account under the Bellhaven
  parent, and a fresh run of the pipeline proposes nothing (35 confident matches).
- Tiffin, Marietta and Sandusky (revenue and outstanding AR) changed in `chow_current_account` and
  nothing else.

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
of its history. Because the API has no merge, the tool also moves the losing copy's contacts to the
survivor (a contact's `account_id` is writable), so nobody is stranded on an inactive record; in
this data that is one contact, on the second Owosso record. Records under a previous owner for a
facility that already has a Bellhaven account (Port Clinton, Erie, Monroe x2) are treated as
duplicates rather than CHOW cases because none has revenue or AR: the SOP would allow re-parenting
them, but that would only produce a second Bellhaven record for the same building. If such a record
did have revenue and open AR, the code preserves it and links it with `chow_current_account`
instead of deactivating it.

**Three records, none under Bellhaven, nothing to tell them apart (Kettering).** One has no parent,
one is under Cedar Trail, one under Harborview; none has revenue, AR, contacts or a matching phone.
The tool picks the unparented record as the survivor (re-parent + rename), marks the other two as
duplicates, flags all three proposals as ambiguous, says so in the note, and lets the reviewer pin
a different survivor in `overrides.json` before approving. The three independent reviewers and the
judge all landed on the same tie-break; the judge's fallback, if I had wanted no guess at all, was
Needs Review on all three, at the cost of leaving a listed community without a Bellhaven account.
I kept the tie-break (the unparented record, `0016KTS1UAWBRXS09J`): with nothing to tell the three
apart, the record that sits under no other operator is the least disruptive one to re-parent, and
none of the three has billing history or contacts to lose.

**Notes as the ownership trail.** The CRM keeps no field history, so a re-parent or rename would
erase exactly the history this exercise is about. Every change to an existing record therefore also
writes what it replaced ("parent was Harborview Care Group", "name was Sunny Acres Retirement
Home", "zip was 45626"), date-stamped and appended to any existing note. The exception is a
CHOW-preserved account, which gets only the link.

**PO box (Ashtabula).** Matched with high confidence on name, city, zip, phone and administrator.
I did not overwrite the PO box with the website's street: the field is a *billing* street, and a PO
box is a legitimate billing address. An optional proposal adds the physical address to the note,
which I approved.

**Zip typo (Portsmouth).** Same street, city, state, name, phone and administrator, zip 45626 vs
45662 - a transposition. Corrected, because here the billing address *is* the physical address.

**Names.** Three accounts carry a pre-rebrand name and are renamed. Four differ only in spelling
("Rehab and Nursing" vs "Rehabilitation & Nursing", "of" vs "at", a missing "The"). I surface those
as a separate low-stakes category rather than silently ignoring or silently changing them. I
approved all four: the website is the only naming authority available, and the previous name is
kept in the note.

**Left alone on purpose.** Phone numbers and existing contacts are used as matching evidence only;
the brief scopes the scrape to name, address and care offerings, and a website front-desk number is
not obviously better than the number billing has on file. Accounts under other parents that merely
resemble a Bellhaven community are never touched.

## How I used AI tools

I built this with Claude Code and directed it rather than accepting its first answer.

- **Exploration before code.** Claude read the website and the API read-only, and mapped all 35
  locations against all 121 accounts by hand before writing a matcher. That is where the site's
  traps surfaced (out-of-range pages re-serving the last page, the community missing from the
  directory) and where the scenario list came from.
- **API contract without side effects.** The docs do not describe request bodies, so the write
  contract was learned from requests designed to be rejected (empty bodies, an invalid status) and
  the account was re-read afterwards to confirm nothing had changed.
- **Independent cross-check of the answer.** Three AI reviewers were given only the raw data and the
  brief - not the tool's output - and each derived the end state the CRM should reach, from three
  angles (sales ops, billing audit, "what did the test author plant"). All three matched the tool on
  every account, including the same Kettering tie-break. Two of their ideas were better than mine
  and I adopted them: record what a change replaced in the note, and note Ashtabula's physical
  address instead of doing nothing.
- **Adversarial code review.** Five reviewers each attacked one risk area (SOP and matching, re-run
  safety, write safety, scraper, app and CI), and a second agent tried to refute each finding. It
  found real problems that my tests had missed, which I fixed and covered with tests: an overlapping
  double click on Approve could execute a proposal twice and create two accounts; a partial scrape
  that the scraper itself had flagged was still acted on; a stale "mark as duplicate" could deactivate
  a record that had since gained revenue and open AR; pinning a new survivor after approving a
  duplicate could leave one duplicate pointing at another; and the scheduled workflow and the
  reviewer both wrote the ledger file. The write-safety reviewer also injected a failure at every
  CRM call of every proposal and retried (500+ runs) to confirm nothing is ever written twice.
- **A second review of the fix.** Because the ledger that makes re-runs safe changed the most after
  that review, it was reviewed again on its own (two adversarial reviewers, each followed by a
  refuter, and 150 randomised multi-day sequences), which confirmed five more edge cases -
  overlapping runs, an approval racing a run, a rejected change returning inside a larger proposal,
  reopening a proposal that is no longer produced, and a re-queued proposal still showing its
  earlier approver - all fixed and covered by tests.
- **Tests as the check on the AI.** The suite runs against an in-memory fake CRM seeded from the
  real sandbox data, including one test that approves the whole queue and asserts the next run
  proposes nothing and every location is a confident match.
- **A rehearsal before any real write.** Because the sandbox cannot be reset, every pending
  approval was first run through the real executor against an in-memory copy of the live CRM
  (`rehearse`): 29 of 29 applied cleanly and the follow-up run proposed nothing.
- **Me.** Every write to the CRM was a proposal approved in the review app. The first one (the
  Portsmouth zip fix) I had Claude submit as a deliberate test of the real write path, then checked
  it in the CRM browser; the other 28 I approved myself.

## What I would build next

1. **Contacts.** The website names each administrator; the CRM's administrator contacts are stale
   for several communities, and new accounts start with none. Propose contact changes the same way.
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

About 1 h 50 min from opening the brief to a working tool and a fully corrected CRM (all 29 changes
approved by 2:21 pm). I then spent roughly two more hours on a second AI review of the re-run ledger
and its fixes, a demo mode for the video, and cleanup. About 3 h 45 min elapsed in total, not
counting recording the video.
