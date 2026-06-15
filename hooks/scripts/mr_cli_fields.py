"""``glab mr create``/``update`` title & description extraction for the gate.

Split out of ``hook_router.py`` by concern (module health): the ``glab mr`` CLI
inline / file / dynamic-value title & description parsing and its helpers. The
gate handler (``handle_validate_mr_metadata``), the out-of-band
``glab api``/``gh api`` surface, and the target-repo parsing stay in the router;
the router delegates the CLI surface here via :func:`extract_cli_mr_fields`.

A bare sibling module (like ``unknown_repo_push_gate``): the router puts its own
dir on ``sys.path`` so ``from mr_cli_fields import …`` resolves both as the live
hook and when imported as ``hooks.scripts.hook_router`` in tests.
"""

import re
from pathlib import Path

# Inline ``--title``/``--description`` (single OR double quoted, multi-line via
# DOTALL). The `glab mr` CLI uses ``--title``/``--description``; the long-flag
# value is captured verbatim from the matching opening quote.
_MR_TITLE_FLAG_RE = re.compile(r"""--title[ =]+(['"])(?P<val>.*?)\1""", re.DOTALL)
_MR_DESC_FLAG_RE = re.compile(r"""--description[ =]+(['"])(?P<val>.*?)\1""", re.DOTALL)
# A file-based description flag (``--description-file``/``-F``) is PRESENT even
# when the inline quote-capture fails: used to decide whether to fall back to
# :func:`_read_message_file` rather than pass a falsely-empty description
# through the validator. ``--description`` is included because the inline
# capture failing on it (``--description "$(cat f)"`` / heredoc) still means a
# description WAS intended — re-read it from the resolvable file arg if any.
_MR_DESC_FLAG_PRESENT_RE = re.compile(r"(?:--description-file|--description\b|\s-F\b)")

# An unexpanded shell construct inside a DOUBLE-quoted value — command
# substitution ``$(…)``, parameter expansion ``${…}``/``$VAR``, or a backtick.
_DYNAMIC_VALUE_RE = re.compile(r"\$[({A-Za-z_]|`")

# File-based message arg — the standard multi-line path (#831's shape):
# ``glab mr create --description-file FILE`` / ``-F FILE``. The captured token
# is a path (optionally quoted); a missing/binary file fails open in
# :func:`_read_message_file`. Long flags require a space or ``=`` separator;
# the short ``-F``/``-C`` branch also accepts git's glued form (``-F<path>``).
_MSG_FILE_FLAG_RE = re.compile(
    r"(?:(?:--description-file|--body-file|--file|--description)[ =]+|-[FC][ =]*)['\"]?([^'\"\s]+)['\"]?",
)


def _looks_dynamic_value(match: "re.Match[str] | None") -> bool:
    """True when a captured ``--title``/``--description`` value is unresolvable.

    An unexpanded shell construct the PreToolUse hook cannot resolve statically.
    The hook sees the raw command BEFORE the shell expands it, so the value
    captured from ``--description "$(cat "$F")"`` is the truncated fragment
    ``$(cat `` (a nested quote ends the non-greedy capture early), not the real
    description. Validating that fragment false-blocks a legitimate dynamic
    description, so it must be skipped — the remote CI gate is the backstop. A
    SINGLE-quoted (or absent) value is literal as captured: a literal ``$`` /
    backtick there is real text and is still validated. ``match.group(1)`` is the
    opening quote char (the shared shape of ``_MR_TITLE_FLAG_RE`` /
    ``_MR_DESC_FLAG_RE``).
    """
    if match is None:
        return False
    if match.group(1) != '"':
        return False
    return bool(_DYNAMIC_VALUE_RE.search(match.group("val")))


def _read_message_file(command: str) -> str | None:
    """Read a file-based message arg (``-F``/``--description-file``/etc.).

    The standard multi-line path is exactly #831's shape. A
    missing/unreadable/binary file fails open (returns ``None``: no scan, no
    crash) — matching the other t3-shelling hooks' posture of never blocking the
    agent on a broken environment.
    """
    match = _MSG_FILE_FLAG_RE.search(command)
    if match is None:
        return None
    path = Path(match.group(1))
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _extract_inline_or_file_desc(command: str) -> str | None:
    """Description text from a Bash MR command — inline quote, then file/heredoc.

    Inline ``--description 'x'`` wins. When the flag is present but the inline
    capture is empty (file-based ``-F``/``--description-file``, a heredoc), fall
    back to :func:`_read_message_file` so a multi-line description is actually
    read and validated rather than passed through as a falsely-empty (and
    trivially "valid"-looking) string.

    Returns ``None`` when the inline value is an unexpanded ``$(…)``/``$VAR`` the
    hook cannot resolve (:func:`_looks_dynamic_value`) — the caller then skips
    validation entirely (never-lockout; the remote CI gate validates the real,
    runtime-expanded body). Returns ``""`` only when a file-based source is
    unreadable — the validator then rejects the empty first line, the correct
    verdict for a genuinely empty description.
    """
    inline = _MR_DESC_FLAG_RE.search(command)
    if inline is not None and inline.group("val"):
        if _looks_dynamic_value(inline):
            return None
        return inline.group("val")
    if _MR_DESC_FLAG_PRESENT_RE.search(command):
        from_file = _read_message_file(command)
        if from_file is not None:
            return from_file
    return ""


def extract_cli_mr_fields(command: str, operation: str) -> tuple[str, str] | None:
    """Title/description for a ``glab mr create``/``update`` CLI command.

    ``operation`` is ``"create"`` or ``"update"``. ``None`` skips validation
    (never-lockout); a tuple is validated.

    Skips when the hook cannot resolve a field statically — an unexpanded
    ``$(…)``/``${…}``/``$VAR``/backtick the shell only expands at runtime (the
    captured fragment, e.g. ``$(cat ``, is not the real value). For ``update``
    it validates ONLY the field(s) the command actually sets: a metadata-only
    edit (reviewer/label/assignee/state — neither field present) is skipped, and
    an unset field is back-filled with a known-good placeholder so the combined
    validator's verdict reflects only the field under edit. ``create`` keeps the
    stricter both-fields contract — an empty title/description on a create is
    exactly the bad metadata the gate must catch (#119).
    """
    title_match = _MR_TITLE_FLAG_RE.search(command)
    if _looks_dynamic_value(title_match):
        return None
    description = _extract_inline_or_file_desc(command)
    if description is None:
        return None
    title = title_match.group("val") if title_match else ""
    if operation == "update":
        desc_present = bool(_MR_DESC_FLAG_PRESENT_RE.search(command))
        if title_match is None and not desc_present:
            return None
        if title_match is None:
            title = description.split("\n", 1)[0]
        elif not desc_present:
            description = f"{title}\n\n## What\n-"
    return title, description
