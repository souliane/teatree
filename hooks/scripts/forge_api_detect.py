"""Shared forge REST-API write/endpoint detection for the PreToolUse gates (#81 PR-step-1).

The effective-HTTP-method classifier and the endpoint regexes several PreToolUse
gates share — the AI-signature gate, the MR-metadata gate, the uncovered-diff
gate, the out-of-band-merge gate, and the raw-review-post sibling. Extracted whole
out of ``hook_router`` (behavior-identical) so the dispatcher shrinks and there is
exactly ONE canonical definition each; the router top-imports them back under
their original names, and the raw-review-post sibling back-imports them lazily.

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with
no guarantee ``teatree`` / Django is importable, so the module top imports only
stdlib ``re``.
"""

import re

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


def _effective_method_is_write(command: str) -> bool:
    """Whether a gh/glab REST command's EFFECTIVE HTTP method is a write (not GET).

    The LAST ``-X``/``--method`` value wins; with no method flag the forge
    defaults to POST when a body/field flag is present, else GET. A GET is the
    only read. Shared by the create-endpoint and merge-endpoint gates so the
    classifier cannot drift between them.
    """
    methods = [m.upper() for pair in _REVIEW_POST_METHOD_RE.findall(command) for m in pair if m]
    if methods:
        return methods[-1] != "GET"
    return bool(_REVIEW_POST_BODY_FLAG_RE.search(command))


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
