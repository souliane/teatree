import re

# Trailing digits of an issue URL are its forge issue number; a ``/0`` suffix is a
# placeholder no forge assigns to a real issue (issue numbers start at 1), so it is
# treated as "no number" and falls back to the pk downstream.
_TRAILING_DIGITS = re.compile(r"(\d+)$")


def derive_issue_number(issue_url: str) -> str:
    """The forge issue number encoded in *issue_url*, or ``""`` when there is none.

    The single source of truth for the ``Ticket.issue_number`` denormalization,
    the ``ticket_number`` property, and the backfill migration — so the persisted
    indexed column and the derived property can never drift.
    """
    match = _TRAILING_DIGITS.search(issue_url)
    if match and match.group(1) != "0":
        return match.group(1)
    return ""
