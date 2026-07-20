r"""Inline ``--body``/``--description`` value resolution for the pre-publish gates.

Split out of :mod:`teatree.hooks._body_file_resolution` to keep that module
under the module-health LOC cap. This module owns one concern: resolving an
inline body VALUE's indirection to the ACTUAL published body, so the
banned-terms / quote scan runs against real content rather than an unexpanded
shell token. Three forms are resolved -- a ``$(cat <path>)`` command
substitution, a ``$VAR`` environment reference, and a ``$(cat <<DELIM … DELIM)``
heredoc-fed substitution -- with a live-vs-inert quote-context check so a body
that merely MENTIONS a ``$(...)`` / ``$VAR`` snippet is scanned verbatim while a
genuinely-unreadable live indirection fails closed.

The matching primitives (``FAIL_CLOSED_SENTINEL``,
``UNAVAILABLE_BODY_SOURCE_SENTINEL``, ``read_file_arg``) live in the
dependency-free :mod:`_parser_primitives` leaf and the quote-context walker
(``raw_substitution_sees_live``) in :mod:`_shell_lexer`; this module imports DOWN
into both leaves, so its consumers (``_command_parser``, ``_t3_review_post``)
import it without a cycle.
"""

import os
import re
from pathlib import Path
from typing import Final

from teatree.hooks._parser_primitives import FAIL_CLOSED_SENTINEL, UNAVAILABLE_BODY_SOURCE_SENTINEL, read_file_arg
from teatree.hooks._shell_lexer import raw_substitution_sees_live

# A body value that IS exactly a ``$(cat <path>)`` command substitution. Agents
# pass a body inline as ``--description "$(cat <path>)"`` / ``--body "$(cat
# <path>)"``; the lexer keeps the whole quoted value as ONE token with the
# substitution UNEXPANDED, so the gate would scan the literal ``$(cat ...)``
# string -- rejecting a clean file and missing a banned term inside it. The
# path is read so the scan runs against the ACTUAL body. Backticks (``$(cat …)``
# only -- the modern form) and a single optional ``-- `` are tolerated; the path
# may be quoted.
_CAT_SUBST_RE: Final[re.Pattern[str]] = re.compile(
    r"^\$\(\s*cat\s+(?:--\s+)?(?P<path>'[^']+'|\"[^\"]+\"|\S+)\s*\)$",
)

# A body value that IS exactly a ``$(cat <<DELIM ... DELIM)`` heredoc-fed
# command substitution -- the canonical ``git commit -m "$(cat <<'EOF' …
# EOF)"`` idiom. The lexer keeps the whole multi-line value as ONE token, so
# ``_CAT_SUBST_RE`` above (a bare path argument) never matches it and the
# generic embedded-``$(...)``  check below would fail-close on a body that is
# actually fully present in the token text. :func:`unredirected_heredoc_bodies`
# already extracts and scans this exact heredoc body elsewhere in
# :func:`_command_parser.extract_bash_payload`, so a match here is resolved
# (empty return, not the sentinel) to avoid emitting a spurious fail-closed
# line alongside the correctly-scanned content (#1213 self-block).
_CAT_HEREDOC_SUBST_RE: Final[re.Pattern[str]] = re.compile(
    r"^\$\(\s*cat\s+<<-?\s*['\"]?(?P<delim>\w+)['\"]?\s*\n.*\n(?P=delim)\s*\n?\s*\)$",
    re.DOTALL,
)

# A body value that IS exactly a single shell-variable reference (``$VAR`` or
# ``${VAR}``). Resolved best-effort from the hook subprocess's environment (it
# inherits the agent's env, the same channel the ``ALLOW_BANNED_TERM`` override
# reaches the gate through). An absent variable is genuinely unresolvable and
# fails closed.
_VAR_REF_RE: Final[re.Pattern[str]] = re.compile(r"^\$\{?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}?$")

# A whole-value ``$VAR`` / ``${VAR}`` reference anchored INSIDE a double-quote
# span (``"$VAR"``) -- the live form the env resolver reads. A single-quoted
# ``'$VAR'`` is inert literal text bash never expands, so it must NOT be env
# resolved (the ``$VAR`` is the published body itself, e.g. documenting a flag).
_DOUBLE_QUOTED_VAR_REF_RE: Final[re.Pattern[str]] = re.compile(
    r"^\"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?\"$",
)


def _raw_substitution_is_live(raw: str) -> bool:
    """Return True iff a ``$(...)`` in ``raw`` sits OUTSIDE a single-quoted span.

    A command substitution is expanded by bash only when it is unquoted or
    inside DOUBLE quotes; inside SINGLE quotes (``'...$(x)...'``) it is inert
    literal text bash passes verbatim, so the gate already holds the real body
    in the decoded value and can scan it. This walks the verbatim source span
    with a quote-context state machine (:func:`raw_substitution_sees_live`):
    a ``'`` opens a single-quoted region only when NOT already inside double
    quotes -- inside a double-quoted span an apostrophe is a LITERAL character,
    not a delimiter -- and a ``$(`` is reported live the moment it opens while
    NOT inside a single-quoted region (unquoted OR double-quoted, both of which
    bash expands). Without this double-quote awareness a body like
    ``"it's $(cat secret)"`` -- one double-quoted string whose ``'`` is a
    literal apostrophe -- would mis-toggle into a phantom single-quoted region
    and report the genuinely LIVE ``$(...)`` as inert, scanning the literal
    token instead of failing closed (a fail-open leak).

    ``raw`` defaults to empty for in-process callers that do not carry a source
    span; an empty/absent ``raw`` is treated as live (conservative -- the gate
    keeps failing closed on an embedded ``$(...)`` it cannot prove inert).
    """
    if not raw:
        return True
    return raw_substitution_sees_live(raw, ("$(",))


def _var_ref_is_live(raw: str) -> bool:
    """Return True iff a whole-value ``$VAR`` in ``raw`` is one bash would EXPAND.

    A ``$VAR`` / ``${VAR}`` reference is expanded by bash when it is DOUBLE-quoted
    (``"$VAR"``) or UNQUOTED (a bare ``$VAR`` argument) -- both live, so the env
    resolver reads the value. Inside SINGLE quotes (``'$VAR'``) it is inert
    literal text bash passes verbatim: the ``$VAR`` string IS the published body
    (documenting a flag), so it must NOT be env-resolved. An empty ``raw`` (an
    in-process caller with no source span) is treated as live (conservative).

    Closes the #F7.4 fail-open where an UNQUOTED ``--body $LEAKVAR`` was scanned
    as the literal token ``$LEAKVAR`` instead of the live env value -- only the
    double-quoted form was resolved before, so an unquoted live env body
    published unscanned.
    """
    if not raw:
        return True
    return bool(_DOUBLE_QUOTED_VAR_REF_RE.match(raw) or _VAR_REF_RE.match(raw))


def resolve_inline_body_value(value: str, base: Path | None, raw: str = "") -> str:
    """Resolve a ``--description``/``--body`` value's indirection to the real body.

    Three forms are resolved so the banned-terms / quote scan runs against the
    ACTUAL published body rather than an unexpanded shell token:

    - ``$(cat <path>)`` -- the file content (read via :func:`read_file_arg`,
        ``base``-relative fallback for the cold-hook reset cwd). An unreadable
        path yields the fail-closed sentinel.
    - ``$VAR`` / ``${VAR}`` -- the environment variable's value when present in
        the hook subprocess env; absent yields the UNAVAILABLE-body-source
        sentinel (the value does not exist before the command runs, so the gate
        renders the actionable "write the body to an absolute file" message,
        #2369). Both the DOUBLE-quoted (``"$VAR"``) and the UNQUOTED (bare
        ``$VAR``) live forms env-resolve (:func:`_var_ref_is_live`); only a
        single-quoted ``'$VAR'`` is inert literal text bash never expands, so it
        is the published body and is scanned verbatim (#F7.4).
    - anything else -- returned verbatim (a normal inline body).

    A value that STILL carries an embedded ``$(...)`` command-substitution
    marker the single-form matchers above did not fully resolve is fail-closed
    ONLY when that substitution is LIVE -- i.e. its source span (``raw``) shows
    the ``$(`` sitting outside any single-quoted region, so bash WOULD expand it
    and the gate cannot see the real content (a mixed ``"prefix $(cat x)"``). A
    ``$(...)`` that sits INSIDE a single-quoted span (``'... $(date) ...'``,
    ``git commit -m 'ran $(date)'``) is inert literal text bash passes verbatim:
    the body is fully present in ``value`` and is SCANNED, not blocked. Without a
    source span (``raw`` empty) an embedded ``$(`` stays fail-closed --
    conservative, since the gate cannot prove it inert. Resolution is never a
    bypass: a live ``$(...)`` source the gate cannot read always fails closed.

    A backtick is NOT a fail-closed trigger. The extracted value is a literal
    argv element the gate only SCANS (never re-feeds to a shell), so a markdown
    inline-code span (a function name / flag / path in backticks, the common
    case in real PR/issue bodies) is inert data fully present in the value and
    fully scanned -- blocking on it was a pure false positive that forced
    ``--body-file``/heredoc workarounds.

    A ``$(cat <<DELIM … DELIM)`` heredoc-fed substitution (the canonical
    ``git commit -m "$(cat <<'EOF' … EOF)"`` idiom) resolves to "" here --
    :func:`unredirected_heredoc_bodies` already scans that exact body
    elsewhere in the same payload, so this walker defers to it instead of
    fail-closing on the outer ``$(...)`` it cannot itself expand (#1213).
    """
    cat_match = _CAT_SUBST_RE.match(value)
    if cat_match is not None and _raw_substitution_is_live(raw):
        path = cat_match.group("path").strip("'\"")
        content = read_file_arg(path, base)
        return content if content is not None else FAIL_CLOSED_SENTINEL
    if _CAT_HEREDOC_SUBST_RE.match(value) is not None:
        return ""
    var_match = _VAR_REF_RE.match(value)
    if var_match is not None and _var_ref_is_live(raw):
        resolved = os.environ.get(var_match.group("name"))
        return resolved if resolved is not None else UNAVAILABLE_BODY_SOURCE_SENTINEL
    if "$(" in value and _raw_substitution_is_live(raw):
        return FAIL_CLOSED_SENTINEL
    return value
