"""Destination-aware verdict for a HIGH pre-publish quote-scanner match (#1213).

Split out of ``hook_router.py`` (a shrink-only module-health-capped god-module)
so the verdict decision and its rationale live in a bare sibling the router
imports thinly, mirroring ``banned_terms_marker.resolve_marker``. The router
keeps only the I/O (deny emission, stderr warning, ledger write); this module
owns "given a HIGH scan result on a publish/commit surface, does it DENY or
DOWNGRADE to a warn, and with which warning + ledger label?".

Two downgrades-to-warn, else deny. The LOCAL-commit sentinel downgrade (#1415):
the ONLY HIGH finding is the unreadable-body sentinel AND the surface is a LOCAL
``git commit`` -- the gate never saw a real quote, only that an ``-F -`` stdin /
heredoc / ``-m "$VAR"`` body was unreadable at scan time, which is not a leak
before push (it stuck multiple coders mid-commit; the body becomes public only on
push). The private-repo carve-out (#126): a real pattern matched on a private-repo
commit. Every other HIGH match denies -- a REAL verbatim-quote pattern on a public
surface always blocks, and a readable commit body that matched a content rule
keeps blocking too.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.hooks.quote_scanner import ScanResult

_LOCAL_COMMIT_WARNING = (
    "WARNING: pre-publish quote-scanner gate (#1213/#1415) — could not read the commit "
    "body, but it is a LOCAL commit; downgraded to warn. The body becomes public only on "
    "push. Verify any user-attributed content is paraphrased.\n"
)
_PRIVATE_REPO_WARNING = (
    "WARNING: pre-publish quote-scanner gate (#1213) — patterns matched on a "
    "private-repo commit; downgraded to warn (#126). Verify the content is paraphrased.\n"
)


@dataclass(frozen=True)
class QuoteVerdict:
    """Outcome of resolving a HIGH quote-scanner result against the destination.

    ``warning`` is the stderr line to print when the match DOWNGRADES to a warn
    (``deny`` is then ``False``); when ``deny`` is ``True`` the caller emits the
    quote-scanner block message. ``decision`` is the ledger label.
    """

    deny: bool
    warning: str | None
    decision: str


def resolve_high_verdict(
    tool_name: str, result: "ScanResult", command: str, payload: str, cwd: Path | None
) -> QuoteVerdict:
    """Resolve a HIGH quote-scanner result to a deny / downgrade verdict.

    The unreadable-body sentinel on a LOCAL ``git commit`` downgrades regardless
    of the landing repo's visibility (the commit is local; the pre-push gate is
    the real public-surface chokepoint) -- but only when the sentinel is the SOLE
    HIGH finding, so a REAL verbatim-quote pattern on a readable commit body still
    denies. The private-repo carve-out then downgrades a real match on a
    private-repo commit (#126). Everything else denies. Config resolution is the
    default env/home one (the live gate passes no explicit config; tests pin it
    via ``T3_BANNED_TERMS_CONFIG``).
    """
    from teatree.hooks import publish_surface, quote_scanner  # noqa: PLC0415

    if quote_scanner.result_is_unreadable_body_only(result) and publish_surface.command_targets_local_commit(
        command, cwd
    ):
        return QuoteVerdict(deny=False, warning=_LOCAL_COMMIT_WARNING, decision="warn-local-commit")
    if publish_surface.carve_out_applies(tool_name, command, payload, cwd):
        return QuoteVerdict(deny=False, warning=_PRIVATE_REPO_WARNING, decision="warn-private-repo")
    return QuoteVerdict(deny=True, warning=None, decision="deny")
