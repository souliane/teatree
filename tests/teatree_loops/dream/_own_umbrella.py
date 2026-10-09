"""The one fact every dream umbrella fake owes the #162 Rule 5 guard: we filed it.

:mod:`teatree.loops.dream.umbrella_ledger` reads the umbrella through
``require_self_authored_issue``, so the body it computes an upsert from is the body
it was AUTHORISED on — one fetch for both facts. A fake payload naming no author
therefore reads as a colleague's ticket, and the pass correctly leaves it alone:
a refusal, not the flow under test.

Stated here once rather than fifteen times across the dream suite. A test that
wants the refusal asks for it explicitly (``author="someone.else"``).
"""

from typing import Any

SELF_LOGIN = "souliane"


def ours(payload: dict[str, Any]) -> dict[str, Any]:
    """*payload* plus the author that makes the Rule 5 guard read it as ours."""
    return {"user": {"login": SELF_LOGIN}, **payload}


def claims_self(host: Any) -> Any:
    """Make a ``MagicMock`` code host authenticate as us, and return it.

    The self-set narrows to ``host.current_user()`` under an empty test DB, and a
    bare ``MagicMock`` answers that with a non-string the set drops — leaving it
    empty, so every author fails closed.
    """
    host.current_user.return_value = SELF_LOGIN
    return host
