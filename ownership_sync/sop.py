"""The change-of-ownership (CHOW) SOP, in one place.

From the brief: "When an account needs to move to a different parent, check [lifetime_revenue and
outstanding_ar] first. If the account has revenue history AND outstanding AR greater than zero, our
billing team needs the old account preserved, so you must NOT change its parent. Instead, leave the
existing account exactly as it is, create a new account for the facility under the correct parent,
and set chow_current_account on the OLD account to the new account's id. If the account has no
revenue history, or no outstanding AR, re-parent the existing account directly."

Both the matcher (when it plans a move) and the executor (again, on the live record, at the moment
of writing) call must_preserve(), so the rule cannot be bypassed by a stale proposal.
"""


def must_preserve(account):
    """True -> billing needs this account left exactly as it is; a move must go the CHOW route."""
    return (account.get("lifetime_revenue") or 0) > 0 and (account.get("outstanding_ar") or 0) > 0


def sop_evidence(account):
    """The numbers and the branch taken, shown to the reviewer on every proposal that moves an account."""
    revenue, ar = account.get("lifetime_revenue") or 0, account.get("outstanding_ar") or 0
    if must_preserve(account):
        branch = "PRESERVE: revenue history AND outstanding AR > 0, so the parent must not change (CHOW route)"
    elif revenue <= 0:
        branch = "RE-PARENT DIRECTLY: no revenue history"
    else:
        branch = "RE-PARENT DIRECTLY: revenue history but no outstanding AR"
    return {"lifetime_revenue": revenue, "outstanding_ar": ar, "must_preserve": must_preserve(account), "branch": branch}
