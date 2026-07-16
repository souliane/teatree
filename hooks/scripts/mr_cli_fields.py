"""``glab mr create``/``update`` title & description extraction for the gate.

Split out of ``hook_router.py`` by concern (module health): the ``glab mr`` CLI
inline / file / dynamic-value title & description parsing and its helpers, plus
the MR TARGET-repo slug parsing (:func:`extract_mr_target_repo`). The gate
handler (``handle_validate_mr_metadata``) and the out-of-band
``glab api``/``gh api`` field surface stay in the router; the router delegates
the CLI title/description surface here via :func:`extract_cli_mr_fields` and the
target-repo parsing via :func:`extract_mr_target_repo`.

A bare sibling module (like ``unknown_repo_push_gate``): the router puts its own
dir on ``sys.path`` so ``from mr_cli_fields import …`` resolves both as the live
hook and when imported as ``hooks.scripts.hook_router`` in tests.
"""

import re
from pathlib import Path
from urllib.parse import unquote

# The MR-mutation verb itself — ``glab mr create``/``update``. Matched against
# the command with quoted spans and heredoc bodies stripped (see
# :func:`strip_quoted_and_heredoc`) so it fires only on a REAL invocation, not
# on the phrase merely embedded in a ``git commit -m '… glab mr create …'``
# message, a doc string, or a heredoc body.
_MR_OP_RE = re.compile(r"\bglab\s+mr\s+(create|update)\b")
# A heredoc body — ``<<['"]?DELIM['"]?`` up to a line that is just ``DELIM``.
# Stripped FIRST (before quotes) because a quoted delimiter (``<<'PY'``) would
# otherwise be eaten by the quote-stripper and orphan the body.
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(?P<delim>\w+)\1.*?^\s*(?P=delim)\b", re.DOTALL | re.MULTILINE)
# Single- and double-quoted argument spans — a ``-m``/``-F`` message, a quoted
# title/description value, any quoted text. Removed for DETECTION only; value
# extraction still runs on the original command.
_SQUOTE_SPAN_RE = re.compile(r"'[^']*'")
_DQUOTE_SPAN_RE = re.compile(r'"[^"]*"')

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


def strip_quoted_and_heredoc(command: str) -> str:
    """Command with heredoc bodies and quoted spans removed — for verb DETECTION.

    Heredoc bodies first (line-structured, and a quoted delimiter would confuse
    the quote pass), then single- and double-quoted argument spans. What remains
    is the bare command skeleton: a ``glab mr create/update`` here is a real
    invocation, while the same text inside a ``-m`` message, a quoted title, or a
    heredoc body is gone. The residual false-negative — a real create/update fed
    *through* a stripped span (``bash -c "glab mr create …"`` or a heredoc piped
    to a shell) — is rare and backstopped by the remote MR-title CI gate; the
    common case (a commit message / doc / verification script that merely quotes
    the phrase) no longer false-blocks.
    """
    without_heredoc = _HEREDOC_RE.sub(" ", command)
    without_squote = _SQUOTE_SPAN_RE.sub(" ", without_heredoc)
    return _DQUOTE_SPAN_RE.sub(" ", without_squote)


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


def extract_cli_mr_fields(command: str) -> tuple[str, str] | None:
    """Title/description for a ``glab mr create``/``update`` CLI command.

    ``None`` means "not an MR mutation to validate" — either the command does
    not actually invoke ``glab mr create/update`` (the verb only appears inside a
    quoted arg / heredoc body — see :func:`strip_quoted_and_heredoc`), or it is
    one but must be skipped (never-lockout). A tuple is validated.

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
    op_match = _MR_OP_RE.search(strip_quoted_and_heredoc(command))
    if op_match is None:
        return None
    operation = op_match.group(1)
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


def cli_update_is_title_only(command: str) -> bool:
    """True for a ``glab mr update`` that sets a title but touches NO description (#3254).

    A pure retitle carries no new description content, so the overlay's
    required-section completeness check (``## Configuration`` / ``## Security &
    privacy impact`` …) must not fire on the hook's back-filled placeholder body —
    the existing MR description is not being changed. ``create`` is never
    title-only (both fields are required), and an update that DOES set a
    description is validated in full.
    """
    op = _MR_OP_RE.search(strip_quoted_and_heredoc(command))
    if op is None or op.group(1) != "update":
        return False
    if _MR_DESC_FLAG_PRESENT_RE.search(command):
        return False
    return _MR_TITLE_FLAG_RE.search(command) is not None


# The MR TARGET repo flag — ``-R <slug>`` / ``--repo <slug>`` on ``glab mr``
# (owner/repo, optionally host-qualified). The slug runs to the next whitespace;
# an optional surrounding quote is tolerated.
_MR_TARGET_REPO_FLAG_RE = re.compile(r"""(?:-R|--repo)[ =]+['"]?(?P<slug>[^\s'"]+)['"]?""")
# ``glab api .../projects/<url-encoded-namespace>/merge_requests…`` — the
# namespace is URL-encoded (``acme-group%2Fwidget``); decoded below. A leading
# slash is optional (``glab api projects/…`` vs ``/api/v4/projects/…``).
_GLAB_API_PROJECT_RE = re.compile(r"\bprojects/(?P<ns>[^/\s'\"]+)/merge_requests")
# ``gh api repos/<owner>/<repo>/pulls…`` — the slug is the two path segments
# after ``repos/``.
_GH_API_REPO_RE = re.compile(r"\brepos/(?P<slug>[^/\s'\"]+/[^/\s'\"]+)/pulls")
# A GitHub PR WEB URL operand to ``gh pr merge`` —
# ``https://<host>/<owner>/<repo>/pull/<n>`` yields ``owner/repo``. The ``/pull/``
# path segment matches case-insensitively; the captured slug stays case-preserved.
_GH_WEB_PR_URL_RE = re.compile(r"https?://[^/\s]+/(?P<slug>[^/\s]+/[^/\s]+)/pull/\d+", re.IGNORECASE)
# A GitLab MR WEB URL operand to ``glab mr merge`` —
# ``https://<host>/<namespace…>/-/merge_requests/<n>`` yields the namespace.
# The namespace may span subgroups (multiple path segments) up to the ``/-/``
# separator, so it is captured non-greedily and URL-decoded like the api form.
_GL_WEB_MR_URL_RE = re.compile(r"https?://[^/\s]+/(?P<ns>[^\s]+?)/-/merge_requests/\d+", re.IGNORECASE)


def extract_mr_target_repo(command: str) -> str | None:
    """Return the MR's TARGET repo slug (``owner/repo``), or ``None`` if absent.

    Parses the target from whichever surface the gate watches so the validator
    can be keyed to the MR's target overlay instead of the agent's cwd. The
    ``-R``/``--repo`` flag on ``glab mr`` gives the slug directly; the
    ``glab api .../projects/<ns>/merge_requests…`` namespace is URL-decoded
    (``acme-group%2Fwidget`` → ``acme-group/widget``); a ``gh api
    repos/<owner>/<repo>/pulls…`` path yields the two segments after ``repos/``;
    and a forge WEB URL operand to ``gh pr merge`` / ``glab mr merge``
    (``…/<owner>/<repo>/pull/<n>`` or ``…/<namespace>/-/merge_requests/<n>``)
    yields the owner/repo or namespace.

    ``None`` when no target is parseable — the validator then keeps its
    cwd-keyed resolution (the established never-lockout fallback).
    """
    flag_match = _MR_TARGET_REPO_FLAG_RE.search(command)
    if flag_match:
        return flag_match.group("slug")

    project_match = _GLAB_API_PROJECT_RE.search(command)
    if project_match:
        return unquote(project_match.group("ns"))

    gh_api_match = _GH_API_REPO_RE.search(command)
    if gh_api_match:
        return gh_api_match.group("slug")

    gh_web_match = _GH_WEB_PR_URL_RE.search(command)
    if gh_web_match:
        return gh_web_match.group("slug")

    gl_web_match = _GL_WEB_MR_URL_RE.search(command)
    if gl_web_match:
        return unquote(gl_web_match.group("ns"))

    return None


def merge_target_is_managed(command: str, managed_slugs: list[str]) -> bool:
    """Whether the command's MR-TARGET slug names a teatree-managed repo.

    Classifies the merge TARGET (parsed by :func:`extract_mr_target_repo`),
    NOT the agent's cwd, so a raw merge form aimed at a managed repo is caught
    regardless of where it runs. Returns ``True`` only when a target slug
    parses AND contains one of the ``managed_slugs`` signal substrings (the
    same offline set the cwd-keyed check uses). A non-parseable target — a
    numeric GitLab project id, or a bare ``gh pr merge <n>`` with no
    ``--repo`` — returns ``False`` so the caller falls back to its cwd-keyed
    classification (the established fail-safe).
    """
    target = extract_mr_target_repo(command)
    if target is None:
        return False
    target_lower = target.strip().lower()
    return bool(target_lower) and any(entry in target_lower for entry in managed_slugs)
