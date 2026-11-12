# Bellhaven ownership sync

Keeps the CRM's picture of who owns which facility in line with the operator's own website.

Every day it scrapes the Bellhaven Senior Living website, matches each community to the CRM,
and turns every difference into a **proposal** with evidence. A reviewer approves or rejects each
proposal in a small local web app. **Only approved proposals are written to the CRM**, and a
decision, once made, is never asked for again.

```
 website ──scrape──┐
                   ├─► match + classify ─► proposals ─► ledger (SQLite) ─► review app ─► approved? ─► CRM API
 CRM API ──read────┘        (pure function)             remembers every              (human)        (guarded write)
                                                         decision
        └──────────── daily job: read-only ───────────────┘                └──── only place that writes ────┘
```

See [WRITEUP.md](WRITEUP.md) for the matching approach, the judgment calls and what I would build next.

## Quick start

```bash
python -m venv .venv
```

```bash
.venv/Scripts/pip install -r requirements.txt
```

(On macOS/Linux use `.venv/bin/` instead of `.venv/Scripts/`.)

Copy `.env.example` to `.env` and put your sandbox token in `CRM_API_TOKEN`. `.env` is gitignored.

```bash
.venv/Scripts/python -m ownership_sync run
```

```bash
.venv/Scripts/python -m ownership_sync serve
```

Then open http://127.0.0.1:5000 and review the queue.

| Command | What it does |
|---|---|
| `python -m ownership_sync run` | The daily job: scrape, read the CRM, match, record proposals. Read-only against the CRM. |
| `python -m ownership_sync rehearse` | Dry-runs every pending approval on a private copy of the live CRM. Writes nothing. |
| `python -m ownership_sync serve` | The review app. Approve / reject; approvals write to the CRM. |
| `python -m ownership_sync scrape` | Just the scraper: prints every location as JSON, with self-check stats. |
| `python -m ownership_sync report` | Every proposal and its decision as a Markdown table. |
| `python -m pytest` | The test suite; it uses an in-memory fake CRM and never touches the sandbox. |

## How it works

### 1. Scraper - `ownership_sync/scraper.py`

A small crawler rather than a `?page=N` loop. It starts at the home page, visits every non-community
page once (directory pages via the pager's real "Next" links, About, ...), collects every
`/communities/<slug>` link it sees anywhere, then parses each community page (name, street, city,
state, zip, care offerings, plus administrator and phone, which are used only as matching evidence).

Two properties of the real site drove this:

- **Out-of-range pages re-serve the last page** (`?page=99` returns page 3), so "keep incrementing
  until a page is empty" never terminates. Following links that are actually on the page does.
- **One community is not in the directory at all.** The directory lists 34; the 35th (Bellhaven
  Meadows of Findlay) is linked only from the home page's "New this year" notice. Harvesting links
  from every page finds it.

The scraper checks itself against the site's own numbers ("34 communities listed" in the directory,
"serve 35 communities" on the home page) and reports a warning on any mismatch. A community page
that cannot be parsed aborts the run instead of being skipped; so does a home or directory page
that will not load. A dead link elsewhere (footer, nav) is skipped with a warning.

### 2. Matching and classification - `ownership_sync/matcher.py`

`build_plan()` is a pure function: website locations + CRM snapshot in, proposals out. No network,
no database, fully deterministic.

**Is this account the facility at this location?** (`compare()`)

1. **Address**: same normalised street + same state + same city or zip. Same building means same
   facility, whatever the account is called and whoever its parent is. Normalisation handles
   `Street/St`, `Road/Rd`, `Northwest/NW`, `Pike/Pk`, trailing `West/W`, punctuation and case.
2. **Name**, only when the street cannot decide (PO box, typo): near-identical name + same city,
   state and zip **and** a corroborating signal (PO box, phone, or the website's administrator being
   a contact on the account).

A similar name alone is never a match. That keeps the decoys out: an "Amberly Manor" in Colorado,
a "Union Square Senior Living" two streets away, "New Carlisle, OH" vs "Carlisle, PA". Rejected
near-misses are shown to the reviewer on the relevant proposal.

**What should happen to the accounts that match?** (`Planner.plan_location()`)

One record survives and is brought in line with the website (parent, name, zip, care type). Every
other record for the same facility becomes its duplicate. The survivor is chosen by a single ranking:
already under Bellhaven > allowed to move under the SOP > Active > has billing history > website's
administrator is a contact on it > phone matches > number of contacts > has no parent. When the top
two are tied on everything but the last, weak criterion, the proposal is flagged **ambiguous**
(medium confidence) and says so; the reviewer can pin a different survivor in `overrides.json`
(before approving anything for that address - once approved duplicates point at a record, it is
never demoted, so no duplicate can end up pointing at another duplicate).

| Finding | Proposal |
|---|---|
| Matches, nothing differs | none - "confident match" |
| Wrong or missing parent, SOP allows a move | PATCH `parent_id` (and `name` if outdated); the note records the previous parent and name |
| Wrong parent, revenue history **and** outstanding AR | **CHOW**: POST a new account under Bellhaven, PATCH only `chow_current_account` on the old one |
| Outdated or differently spelled name | PATCH `name`; the note records the previous name |
| Same street, different zip/city | PATCH the differing field |
| Matched, but the billing street is a PO box | optional note with the website's physical address; the PO box is never overwritten |
| Extra record for the same facility | PATCH `duplicate_of_account` = survivor, `status` = Inactive, plus a note; its contacts are moved to the survivor |
| On the website, no CRM account | POST a new account under Bellhaven |
| Under Bellhaven, not on the website, same address now under another owner | CHOW link to that existing account (revenue + AR) or duplicate of it (otherwise) |
| Under Bellhaven, not on the website, no trace of a new owner | PATCH `status` = Needs Review plus a note |

The CRM keeps no field history, so every change to an existing record also writes what it replaced
into the note (previous parent, previous name, old zip). A re-parent would otherwise erase the very
ownership trail this system exists to maintain. The one exception is a CHOW-preserved account, which
gets nothing but `chow_current_account`.

**The CHOW SOP** lives in one 10-line function, `sop.must_preserve()`. The matcher calls it when it
plans a move, and the executor calls it again on the live record at the moment of writing.

### 3. Review app - `ownership_sync/app.py`

Each proposal card shows: what will change (before / after), the SOP check with the actual revenue
and AR, website listing vs CRM record side by side with a match/differs mark per field, the reasons
in plain words, related records (survivor, duplicates, rejected near-misses), and the exact API
call(s) an approval will make. Links open the community page and the account in the CRM browser.

- **Approve** executes the proposal and shows the outcome with a link to the account in the CRM.
- **Reject** records the decision (with an optional reason). It is never proposed again.
- **All locations** shows all 35 communities with their classification and matched account, plus
  the Bellhaven accounts that are not on the website.
- **Audit log** lists every decision, who made it and every API write it caused.

### 4. Guarded writes - `ownership_sync/apply.py`

Nothing writes without approval, structurally:

- The daily job builds its CRM client with `read_only=True`; any write through it raises.
- The only writable client is created inside the app's `approve()` handler.
- Every POST to the app must carry a per-process form token, and the app answers only on
  `localhost` / `127.0.0.1`, so only this UI can approve.
- A proposal is claimed atomically before anything is written, so a double click (or a Retry racing
  the first attempt) executes it once.

When an approval is executed:

1. **Idempotency**: if the CRM already shows the proposed end state, nothing is written. A retried
   CHOW finds the account it created earlier instead of creating a second one.
2. **Drift**: the account's `updated_at` must equal what the reviewer saw. If someone changed the
   account in the meantime, nothing is written and the proposal is closed; the next run re-proposes
   from fresh data. Likewise, if a pipeline run refreshed the proposal after the reviewer's page
   loaded, the click is refused and they are asked to look again.
3. **SOP, again**: a parent change, or deactivating a record as a duplicate, is refused if the
   live record now has revenue and open AR.
4. **Read-back**: after writing, the account is fetched and compared with what was intended. A new
   account whose POST ignored a field gets that field set explicitly, because accounts cannot be
   deleted and a half-filled one would stay forever.

## Testing it against the real CRM

The pipeline and the review app always work on the real CRM: `run` reads it, and Approve writes to
it. Only the automated test suite uses an in-memory stand-in, because the sandbox has no delete and
no reset - a test that created accounts there would leave them behind for good.

Three ways to test on real data, from safest to most conclusive:

1. **Read path** - `python -m ownership_sync run`. Scrapes the live site, reads the live CRM, and
   prints what it found. Every proposal card links to the account in the CRM browser.
2. **Rehearsal** - `python -m ownership_sync rehearse`. Copies the live CRM into memory and sends
   every pending approval through the real executor and all of its guards (drift, SOP,
   idempotency, read-back) against that copy, then runs the matcher on the result. It reports,
   per proposal, the calls it would send or the reason it would be refused, and what the next run
   would propose once everything was approved (the goal is nothing). The CRM and the ledger are
   not touched.
3. **A real approval** - approve one proposal in the app. The app writes to the CRM, reads the
   account back to verify it, and links to it in the CRM browser. This is the only step that proves
   how the real API answers a write, so start with a small reversible one (a zip or name fix) before
   the creates, which cannot be undone.

## Running it daily

The job is one command, `python -m ownership_sync run`. It is read-only against the CRM, exits
non-zero (and proposes nothing) if the scrape looks incomplete or the CRM cannot be read, and is safe
to run as often as you like.

**On the machine that hosts the review app** - the setup I would use, because the job and the app
then share one ledger and there is nothing to keep in sync:

```
0 6 * * *  cd /opt/ownership-sync && .venv/bin/python -m ownership_sync run >> sync.log 2>&1
```

**As a GitHub Actions workflow** - [`.github/workflows/daily-sync.yml`](.github/workflows/daily-sync.yml)
runs every day at 11:00 UTC with `CRM_API_TOKEN` as a repository secret (without the secret it runs
the tests and skips the sync with a notice). It runs the tests, runs the pipeline against the ledger
committed in the repository, and writes the result to the run summary ("N new proposals awaiting
review"). It deliberately does not commit anything: the ledger has exactly
one writer, the review app. The reviewer presses **Run pipeline now** (same fingerprints, so the same
proposals), decides, and pushes `data/state.db`.

## Why re-runs are safe

Two independent mechanisms:

1. **The ledger.** Each proposal has a fingerprint derived from *what it would change* (account,
   fields and values - not wording, dates or evidence). `store.sync_proposals()` inserts unseen
   fingerprints, refreshes ones that are still pending, and leaves approved or rejected ones alone.
   A pending proposal that is no longer produced is closed automatically.
2. **Convergence.** Records resolved by an earlier approval (marked duplicate, CHOW-linked, flagged)
   are recognised and left alone, so an approved change does not come back as a new diff.
   `tests/test_reruns.py` approves the whole queue against a fake CRM and asserts that the next run
   proposes nothing and classifies all 35 locations as confident matches.

A partial scrape cannot cause damage either. A run aborts before proposing anything if the directory
says it lists N communities and a different number was scraped from it, or if it finds fewer than 80%
of the communities the last good run found (a real drop that large has to be acknowledged by running
once with `MIN_SCRAPE_RATIO=0`).

## State

`data/state.db` (SQLite) holds runs, proposals, decisions and the audit log. It contains no secrets.
It is committed on purpose: it is the record of what was decided and why. It has one writer - the
machine running the review app - so commit and push it after a review session. In production this
would be a managed database shared by the job and a hosted review app.

## Where to change things

| To change... | Edit |
|---|---|
| Website markup / a new field to scrape | `scraper.py` -> `parse_detail()` |
| Street or name normalisation rules | `normalize.py` |
| What counts as a match | `matcher.py` -> `compare()` |
| Which record survives a duplicate group | `matcher.py` -> `Planner.rank()` |
| The CHOW rule | `sop.py` -> `must_preserve()` |
| Website care offering -> CRM care type | `config.py` -> `CARE_TYPE_MAP` |
| What an approval writes, and its guards | `apply.py` |
| What the reviewer sees | `templates/queue.html` |
| Schedule | `.github/workflows/daily-sync.yml` |

## Layout

```
ownership_sync/
  config.py      settings, .env loading, care-type map
  scraper.py     website -> Location list (+ self-check stats)
  crm.py         CRM API client (read-only mode, write log)
  normalize.py   street / name / phone normalisation
  sop.py         the CHOW rule
  matcher.py     locations x accounts -> classification + proposals
  store.py       SQLite ledger: runs, proposals, decisions, audit
  pipeline.py    one scheduled run
  apply.py       execute one approved proposal, with guards
  app.py         Flask review app
  templates/     queue, all locations, audit log
tests/           tests + fixtures (the untouched sandbox seed, scraped locations)
.github/workflows/daily-sync.yml
```
