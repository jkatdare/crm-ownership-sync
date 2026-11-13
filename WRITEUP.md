# Writeup

**How I used AI tools.** To start, I copied the brief into Notepad, replaced both API keys with a
placeholder, created a project directory, added a `.env` file holding the real key, and pasted the
brief with the keys removed into Claude Code. I let Claude work, and used that time to pull data
myself to get a better idea of how the system works and to get familiar with the dataset. Once the
base output was ready, I used what I learned from that testing to improve it. Claude also ran
independent AI reviews of its own work, which found real bugs that were then fixed with tests.
Nothing was written to the CRM without my approval: I reviewed the proposals in the review app,
approved all 29, and checked the result in the CRM browser.

**Matching approach.** Address first, name second.

- The same street, state and city or zip means the same facility, whatever the account is called
  and whoever its parent is. This finds renamed facilities, facilities still filed under a previous
  owner, and duplicates.
- A name only counts with supporting evidence: the same city and zip plus a matching phone, PO box
  or administrator. A similar name alone is never a match. "Amberly Manor" in Colorado is not
  Amberly Manor in Ohio.
- Each location is then classified: confident match (17), needs a fix (14), no account yet (4),
  plus 3 Bellhaven accounts no longer on the website. Accounts with revenue and outstanding AR are
  never re-parented; they follow the change-of-ownership SOP.

**Result.** All 35 website communities now have exactly one active account under the Bellhaven
parent.

**What I'd build next.**

- Contacts: the website names an administrator for all 35 communities, but only 20 of the accounts
  have that person as a contact, and 9 have no contacts at all. Propose contact changes through the
  same review flow.
- Find the new owner of the 2 facilities that left Bellhaven (Alliance and Coldwater), using other
  operators' websites and public registries, so "Needs Review" becomes "moved to X".
- Host the review app with sign-in and a shared database, and send a notification when the daily
  run finds something new. Today it runs on one laptop.

The reasoning behind each change, and more ideas for what to build next, are in
[DETAILS.md](DETAILS.md).
