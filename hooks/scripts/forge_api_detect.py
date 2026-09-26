"""Shared forge REST-API write/endpoint detection for the PreToolUse gates (#81 PR-step-1).

The effective-HTTP-method classifier and the endpoint regexes several PreToolUse
gates share — the AI-signature gate, the MR-metadata gate, the uncovered-diff
gate, the out-of-band-merge gate, and the raw-review-post sibling. Extracted whole
out of ``hook_router`` (behavior-identical) so the dispatcher shrinks and there is
exactly ONE canonical definition each; the router top-imports them back under
their original names, and the raw-review-post sibling back-imports them lazily.

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with
no guarantee ``teatree`` / Django is importable, so the module top imports only
stdlib ``re`` plus the equally stdlib-only ``managed_repo`` sibling.
"""

import re
from typing import Final

from hooks.scripts.managed_repo import teatree_src_on_path

# REST-API create-endpoint: .../pulls or .../merge_requests WITHOUT /N/merge.
# Distinguishes a PR/MR create from a list read (GET) or the merge endpoint
# already covered by _MERGE_ENDPOINT_RE.  The optional /\d+ matches both the
# collection endpoint (/pulls, /merge_requests) and a per-MR update endpoint
# (/pulls/42, /merge_requests/42) when written as a POST.
#
# The trailing class keeps `/` so the collection-create form written WITH a
# trailing slash (`/merge_requests/ -f title=…`) still matches as a create —
# dropping `/` here lets a real trailing-slash MR/PR-create POST escape all
# three consumers. The sub-resource exclusion lives entirely in the lookahead
# `(?!/\d*/?[A-Za-z])`: a read-only nested GET (`/merge_requests/42/approvals`,
# `/pulls/123/commits`, `/notes`, `/files`, `/pipelines`) is `/\d+` then `/`
# then a letter, so the lookahead rejects it; the trailing-slash create is
# `/` then a space (not a letter), so the lookahead admits it.
_API_CREATE_ENDPOINT_RE = re.compile(r"/(?:pulls|merge_requests)(?:/\d+)?(?!/\d*/?[A-Za-z])(?:[/?'\"\s]|$)")

# REST-API merge endpoint: ``(merge_requests|pulls)/<n>/merge``.
# Matches both GitHub (``repos/OWNER/REPO/pulls/<n>/merge``) and
# GitLab (``projects/<id>/merge_requests/<n>/merge``) URL shapes.
_MERGE_ENDPOINT_RE = re.compile(r"(?:merge_requests|pulls)/\d+/merge\b")

# Two captured forms of the gh/glab HTTP-method flag, both empirically valid
# against gh (2.87.3) / glab (1.80.4): the spaced/``=`` form (``-X PUT``,
# ``--method=POST``) and the pflag NO-SPACE shorthand (``-XPUT``). The
# no-space form is a real method override (``gh api -XGET /rate_limit`` returns
# 200), so omitting it let ``-XPUT`` evade classification → ``is_read=True`` →
# the merge/review write slipped through. Consumers flatten the two capture
# groups and keep last-wins effective-method semantics.
_REVIEW_POST_METHOD_RE = re.compile(
    r"(?:-X|--method)[\s=]+['\"]?([A-Za-z]+)\b"
    r"|(?<=-X)([A-Za-z]+)\b",
)
_REVIEW_POST_BODY_FLAG_RE = re.compile(
    r"(?:^|\s)(?:-f|--field|-F|--raw-field|--input|-d|--data)\b",
)

_GLAB_GH_API_RE = re.compile(r"\b(?:glab|gh)\s+api\b")


def _effective_method(command: str) -> str:
    """The EFFECTIVE HTTP method of a gh/glab REST command, upper-cased.

    The LAST ``-X``/``--method`` value wins; with no method flag the forge
    defaults to POST when a body/field flag is present, else GET.
    """
    methods = [m.upper() for pair in _REVIEW_POST_METHOD_RE.findall(command) for m in pair if m]
    if methods:
        return methods[-1]
    return "POST" if _REVIEW_POST_BODY_FLAG_RE.search(command) else "GET"


def _effective_method_is_write(command: str) -> bool:
    """Whether a gh/glab REST command's EFFECTIVE HTTP method is a write (not GET).

    A GET is the only read. Shared by the create-endpoint and merge-endpoint
    gates so the classifier cannot drift between them.
    """
    return _effective_method(command) != "GET"


def _is_api_create_endpoint_write(command: str) -> bool:
    """Whether *command* is a REST-API POST/PATCH to a PR/MR collection endpoint.

    True only when the command targets a ``.../pulls`` or
    ``.../merge_requests`` endpoint (without the ``/N/merge`` suffix already
    covered by :data:`_MERGE_ENDPOINT_RE`) AND its effective HTTP method is
    not GET.  Reuses the gate-3 effective-method classifier (last
    ``-X``/``--method`` wins; default POST with a body flag, else GET).
    A bare GET to the list endpoint reads PR list and must NOT be treated as
    a create-class mutation.
    """
    if not _API_CREATE_ENDPOINT_RE.search(command):
        return False
    # Exclude the merge endpoint (already handled by out-of-band-merge gate).
    if _MERGE_ENDPOINT_RE.search(command):
        return False
    return _effective_method_is_write(command)


# An EXISTING PR/MR's own endpoint, carrying its number: ``/pulls/3887``,
# ``/merge_requests/77``. The lookahead rejects a nested sub-resource
# (``/pulls/3887/commits``) so only the PR object itself matches.
_API_NUMBERED_PR_ENDPOINT_RE = re.compile(r"/(?:pulls|merge_requests)/\d+(?![/\d])")

#: What separates a field flag from its value. Whitespace covers the TAB and the newline
#: both CLIs tokenise exactly as they do a space; ``=`` covers ``--field=key=value``.
API_FIELD_SEPARATOR: Final[str] = r"[\s=]"
#: This module's flags, a SUPERSET of the metadata matcher's (it reads any key, so ``-d``
#: counts). Long ones ALWAYS need a separator — ``--fieldkey=value`` is a spelling neither
#: pflag nor cobra accepts — while both CLIs take a short one GLUED to its value. That
#: glued form was missing here while the sibling accepted it, so a ``-Fstate_event=close``
#: was invisible to this matcher and the write that changes a PR's STATE read as a
#: title-only edit, skipping the gate that governs it.
_LONG_FIELD_FLAGS: Final[str] = r"--field|--raw-field|--data"
_SHORT_FIELD_FLAGS: Final[str] = r"-[fFd]"

# The ``key`` of a ``-f key=value`` / ``--field`` / ``-F`` / ``--raw-field`` /
# ``-d`` / ``--data`` argument.
_API_FIELD_KEY_RE = re.compile(
    rf"(?:^|\s)(?:(?:{_LONG_FIELD_FLAGS}){API_FIELD_SEPARATOR}+|{_SHORT_FIELD_FLAGS}{API_FIELD_SEPARATOR}*)"
    r"['\"]?([A-Za-z_][A-Za-z0-9_]*)=",
)

# Fields carrying only a PR/MR's DESCRIPTIVE metadata. Editing these changes no
# repository state: the PR still exists, still targets the same base branch, and
# keeps its open/closed and draft status. Anything outside this set (``state``,
# ``base``, ``target_branch``, ``draft``, …) is a state change and stays gated.
_PR_METADATA_FIELDS = frozenset({"title", "body", "description"})

# The update methods a metadata edit uses. A POST to a numbered endpoint is not an
# edit (the forges treat it as a create/sub-resource action), so it stays gated.
_PR_UPDATE_METHODS = frozenset({"PATCH", "PUT"})


def _is_existing_pr_metadata_only_edit(command: str) -> bool:
    """Whether *command* only edits an EXISTING PR/MR's descriptive metadata.

    True for an update (``PATCH``/``PUT``) against a PR's own numbered endpoint
    that sets ONLY fields in :data:`_PR_METADATA_FIELDS`. Retitling a PR or
    rewriting its description creates nothing, merges nothing, and closes
    nothing, so it must not be classified alongside the create/merge/close
    writes that move a PR toward merge.

    Deliberately narrow and fail-safe: no recognised field, an unrecognised
    field, the collection endpoint, or any other method all return ``False``,
    leaving the caller's existing classification in force.
    """
    if not _API_NUMBERED_PR_ENDPOINT_RE.search(command):
        return False
    if _MERGE_ENDPOINT_RE.search(command):
        return False
    if _effective_method(command) not in _PR_UPDATE_METHODS:
        return False
    keys = {m.group(1) for m in _API_FIELD_KEY_RE.finditer(command)}
    return bool(keys) and keys <= _PR_METADATA_FIELDS


def is_raw_merge_api_write(command: str) -> bool:
    """Whether *command* is a raw forge REST WRITE to a merge endpoint.

    Delegates to :func:`teatree.hooks.raw_merge_detect.is_raw_merge_api_write` —
    the SAME leaf the shared hard-deny registry (Lane B) uses, so the two lanes
    classify the merge API write identically. Fails CLOSED (treats the command as
    a possible merge write) on any import error so a broken environment cannot
    weaken the gate; the cwd-managed check then blocks on uncertainty.
    """
    return _raw_merge_probe("is_raw_merge_api_write", command)


def invokes_raw_merge_subcommand(command: str) -> bool:
    """Whether ``command`` INVOKES ``gh pr merge`` / ``glab mr merge`` as an executed program.

    Delegates to the action-aware :mod:`teatree.hooks.raw_merge_detect`, which
    fires only when the merge subcommand sits at a command position — never when
    the phrase appears inside a heredoc body, a quoted argument, an
    ``echo``/``printf`` string, or a ``#`` comment (#2387).
    """
    return _raw_merge_probe("invokes_raw_merge_subcommand", command)


def _raw_merge_probe(classifier: str, command: str) -> bool:
    """Run the named :mod:`teatree.hooks.raw_merge_detect` classifier, failing CLOSED.

    The leaf lives under ``src/``, which a cold PreToolUse subprocess reaches only through
    the bootstrap; an unimportable leaf answers True so a broken environment can never
    weaken the merge gate — the cwd-managed check then blocks on that uncertainty.
    """
    try:
        with teatree_src_on_path():
            from teatree.hooks import raw_merge_detect  # noqa: PLC0415 — deferred: cold-hook import

            return bool(getattr(raw_merge_detect, classifier)(command))
    except Exception:  # noqa: BLE001 — fail-closed: a broken import must not weaken the merge gate
        return True
