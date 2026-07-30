"""Forge merge-response classification (§17.4.3): transient / policy-refusal / head-moved.

Split out of :mod:`execution` (the module-health LOC cap): deciding whether a
non-zero ``gh``/``glab`` merge response is a momentary transport failure (retry),
a policy refusal (a verdict, never retried), or a head-moved (fail closed) is a
distinct concern from the keystone orchestration. The byte-for-byte forge error
f-strings the keystone tests pin live here. ``execution`` imports the single
public entry point :func:`_raise_bound_merge_failure`; the transient/refusal
classifier :func:`_is_transient_merge_response` is a private helper of THIS
module that :func:`_raise_bound_merge_failure` calls — ``execution`` does not
re-export it (``execution._is_transient_merge_response`` does not resolve).
"""

from teatree.core.backend_protocols import ForgeMergeResult
from teatree.core.merge.errors import MergeHeadMovedError, MergePreconditionError, MergeTransientError

# Lower-cased substrings that mark a forge merge response as TRANSIENT — the
# forge momentarily failing to answer rather than refusing the merge. A
# truncated/empty JSON body (the #1804 window), a network/connection error, a
# timeout, or a 5xx. Matched against the combined stdout+stderr.
_TRANSIENT_MERGE_MARKERS = (
    "unexpected end of json input",
    "unexpected eof",
    "empty response",
    "connection reset",
    "connection refused",
    "connection closed",
    "broken pipe",
    "timeout",
    "timed out",
    "eof",
    "i/o timeout",
    "temporary failure",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "502",
    "503",
    "504",
)

# Lower-cased substrings that mark a forge merge response as a POLICY REFUSAL —
# a verdict on the merge, never retried. Checked first so a refusal that also
# mentions a transient-looking token (rare) is still classified as a refusal.
_POLICY_REFUSAL_MERGE_MARKERS = (
    "not mergeable",
    "is not mergeable",
    "required status check",
    "review required",
    "changes requested",
    "merge conflict",
    "405",
    "422",
)


def _is_transient_merge_response(rc: int, out: str, err: str) -> bool:
    """True iff a non-zero forge merge response is transient (retryable).

    A policy refusal (not-mergeable / required-checks / 405 / 422) is never
    transient — checked first so a refusal is never mis-retried. An empty
    body with no recognisable marker (rc != 0, no stdout, no stderr) is the
    truncated/dropped-response shape and is treated as transient. Anything
    else with an explicit non-transient message is NOT transient.
    """
    if rc == 0:
        return False
    combined = f"{out}\n{err}".lower()
    if any(marker in combined for marker in _POLICY_REFUSAL_MERGE_MARKERS):
        return False
    if any(marker in combined for marker in _TRANSIENT_MERGE_MARKERS):
        return True
    return not combined.strip()


def _raise_bound_merge_failure(
    *,
    result: ForgeMergeResult,
    slug: str,
    pr_id: int,
    expected_head_oid: str,
    host_kind: str,
) -> None:
    """Classify a non-zero merge response and raise the typed forge-specific error.

    GitLab and GitHub have distinct head-moved sniffs and distinct error
    f-strings (``!`` vs ``#``, ``glab`` vs ``gh``); both are preserved verbatim.
    """
    out, err = result.stdout, result.stderr
    combined = f"{out}\n{err}".lower()
    if host_kind == "gitlab":
        if "sha" in combined and ("does not match" in combined or "409" in combined or "conflict" in combined):
            msg = (
                f"GitLab refused the merge of {slug}!{pr_id}: head moved off "
                f"{expected_head_oid} (length={len(expected_head_oid)}, "
                f"expected_head_oid mismatch). Treated as a failed check — "
                f"NOT retried with a new head (§17.4.3)"
            )
            raise MergeHeadMovedError(msg)
        if _is_transient_merge_response(result.returncode, out, err):
            msg = (
                f"merge of {slug}!{pr_id} hit a transient forge response: "
                f"{err.strip() or out.strip() or 'empty glab api response'} — retrying (#1813)"
            )
            raise MergeTransientError(msg)
        msg = f"merge of {slug}!{pr_id} failed: {err.strip() or out.strip() or 'glab api non-zero'}"
        raise MergePreconditionError(msg)
    if "head" in combined and ("modif" in combined or "changed" in combined or "409" in combined):
        # Print the full ``expected_head_oid`` so a length mismatch can never
        # masquerade as a value mismatch (#1162).
        msg = (
            f"GitHub refused the merge of {slug}#{pr_id}: head moved off "
            f"{expected_head_oid} (length={len(expected_head_oid)}, "
            f"expected_head_oid mismatch). Treated as a failed check — "
            f"NOT retried with a new head (§17.4.3)"
        )
        raise MergeHeadMovedError(msg)
    if _is_transient_merge_response(result.returncode, out, err):
        msg = (
            f"merge of {slug}#{pr_id} hit a transient forge response: "
            f"{err.strip() or out.strip() or 'empty gh api response'} — retrying (#1813)"
        )
        raise MergeTransientError(msg)
    msg = f"merge of {slug}#{pr_id} failed: {err.strip() or out.strip() or 'gh api non-zero'}"
    raise MergePreconditionError(msg)
