"""Resolve the ``review post-comment`` body from its three input sources (#32).

``t3 <overlay> review post-comment REPO MR NOTE`` historically took the comment
body as the positional ``NOTE`` only. A large MR-thread evidence body is awkward
and error-prone to pass as a single shell-quoted argument, so #32 adds the two
flag forms the sibling forge comment commands already use:

- ``-m`` / ``--body <text>`` — an inline body, and
- ``--body-file <path>`` — read the body from a file on disk.

Exactly one of the three sources must be supplied. Resolving them in one pure
helper keeps the typer command a thin shim (no business logic in ``cli``) and
gives the #1415 banned-terms gate a well-known flag (``-m``/``--body``/
``--body-file``) to scan instead of a bespoke positional code path.
"""

from pathlib import Path

# The flag spellings named in the operator-facing error so the message points at
# every accepted source. ``-m`` is the short form of ``--body``.
_BODY_SOURCE_HINT = "Pass the body as the positional NOTE, -m/--body <text>, or --body-file <path>."


class PostBodyError(ValueError):
    """Raised when the comment body sources are absent, ambiguous, or unreadable."""


def resolve_post_body(*, note: str | None, body: str, body_file: str) -> str:
    """Return the single resolved comment body, or raise :class:`PostBodyError`.

    Exactly one of ``note`` (positional), ``body`` (``-m``/``--body``), or
    ``body_file`` (``--body-file``) must be supplied. An empty ``note``/``body``
    string counts as "not supplied" (typer passes ``""`` for an omitted option).
    A ``--body-file`` that cannot be read is an error rather than a silent empty
    body — the agent must fix the path before posting.
    """
    sources: list[str] = []
    if note:
        sources.append("NOTE")
    if body:
        sources.append("--body")
    if body_file:
        sources.append("--body-file")

    if not sources:
        msg = f"No comment body given. {_BODY_SOURCE_HINT}"
        raise PostBodyError(msg)
    if len(sources) > 1:
        joined = ", ".join(sources)
        msg = f"Multiple comment bodies given ({joined}) — choose one. {_BODY_SOURCE_HINT}"
        raise PostBodyError(msg)

    if body_file:
        return _read_body_file(body_file)
    if body:
        return body
    return note or ""


def _read_body_file(path: str) -> str:
    """Read the ``--body-file`` content, raising :class:`PostBodyError` on failure."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"--body-file could not be read ({path}): {exc}"
        raise PostBodyError(msg) from exc
