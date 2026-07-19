"""The shared "resolve the overlay code host" preamble (F3.7).

Eight commands (``pr``, ``followup``, ``mr-reminder``, and the ``_test_plan``
posters) opened with the same "``code_host_from_overlay()`` is ``None`` → no
code host configured" preamble in slightly divergent wording ("check overlay
tokens" / "check overlay GitLab token" / bare). This centralises the canonical
message so the phrasing can never drift again.

The *resolution* stays at each call site: many of the callers' test suites
``patch`` ``code_host_from_overlay`` in the command module's own namespace, so a
shared resolver that imported its own reference would silently bypass those
patches. Each caller keeps its own ``code_host_from_overlay()`` call (and its own
control-flow shape — a ``{"error": ...}`` dict, a ``(None, error)`` tuple, or
``write_err`` + ``SystemExit``) and sources only the message from here.
"""

from typing import Final

#: The one canonical "no code host" message every preamble now shares.
NO_CODE_HOST_MESSAGE: Final = "No code host configured (check overlay GitLab/GitHub token)."


def no_code_host_error() -> dict[str, str]:
    """The canonical ``{"error": <no code host>}`` payload for dict-returning commands."""
    return {"error": NO_CODE_HOST_MESSAGE}
