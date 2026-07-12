"""Destination-aware verdict for a HIGH pre-publish quote-scanner match (#1213).

Split out of ``hook_router.py`` (a shrink-only module-health-capped god-module)
so the verdict decision and its rationale live in a bare sibling the router
imports thinly, mirroring ``banned_terms.marker.resolve_marker``. The router
keeps only the I/O (deny emission, stderr warning, ledger write); this module
owns "given a HIGH scan result on a publish/commit surface, does it DENY or
DOWNGRADE to a warn, and with which warning + ledger label?".

One downgrade-to-warn, else deny: the private-repo carve-out (#126) downgrades a
HIGH match on a provably-PRIVATE commit/post (a private repo cannot leak to the
public). Every other HIGH match DENIES -- a real verbatim-quote pattern on a
public surface, and the unreadable-body fail-closed sentinel on a public commit,
both block.

Why the unreadable-body sentinel is NOT downgraded on a public commit the way
the banned-terms gate downgrades it (#1415): the banned-terms downgrade is
backstopped by the pre-push gate (``refuse-public-push-with-leak.sh`` #703),
which re-scans every commit message via ``privacy-scan`` for banned terms before
a public push. ``privacy-scan`` has NO verbatim-quote detector, so the quote
gate has NO such backstop -- a verbatim user quote committed via a genuinely
opaque body (``cat | git commit -F -`` / ``-m "$VAR"``) would reach public
history un-scanned. So the quote gate keeps base ``main``'s conservative DENY on
a public unreadable-body commit. The coder-unblock for clean commits still holds
for ALL visibilities, because a readable stdin/heredoc/``printf``-piped body is
RESOLVED and scanned upstream (``_body_file_resolution``) and never produces the
sentinel; only the genuinely-opaque public case reverts to deny.
"""

from dataclasses import dataclass
from pathlib import Path

_PRIVATE_REPO_WARNING = (
    "WARNING: pre-publish quote-scanner gate (#1213) — patterns matched on a "
    "private-repo commit; downgraded to warn (#126). Verify the content is paraphrased.\n"
)

# The non-public-destination SKIP reuses the ``warning`` channel with an EMPTY
# string so the router's ``_quote_scanner_high_io`` prints nothing (a no-op
# ``stderr.write("")``) yet takes the allow (non-deny) branch — the leak gate
# enforces ONLY on an affirmatively-public target (#1415/#1213), so a HIGH match
# whose repo target is not affirmatively public is silently allowed.
_NON_PUBLIC_SKIP = ""


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


def resolve_high_verdict(command: str, cwd: Path | None) -> QuoteVerdict:
    """Resolve a HIGH quote-scanner result to a deny / skip / downgrade verdict.

    A HIGH match whose repo target is NOT affirmatively PUBLIC is SKIPPED
    entirely (#1415/#1213 -- the leak gate enforces ONLY on an affirmatively-
    public target). ``gate_skips_for_visibility`` resolves the command's own
    target (the ``--repo``/``-R`` flag, the ``gh``/``glab api`` URL path, or the
    cwd remote) and skips a private/internal/unknown/unresolvable one; the skip
    verdict is silent (empty ``warning``, ``allow-nonpublic-destination`` ledger
    label). This is checked BEFORE the private-commit downgrade so a private
    post never reaches the ``command_targets_private_only`` warn.

    A HIGH match whose destination is provably PRIVATE downgrades to a warn (#126
    -- a private repo cannot leak to the public). The check is body-INDEPENDENT
    (``command_targets_private_only``: a ``git commit`` landing in a known-private
    repo, or a pure private ``gh``/``glab`` post), so it covers BOTH a real quote
    pattern AND the unreadable-body fail-closed sentinel on a private commit --
    the same private predicate the banned-terms sibling marker uses. Everything
    else DENIES: a real verbatim-quote pattern on a public surface, and the
    unreadable-body sentinel on a PUBLIC commit.

    This is deliberately the banned-terms marker MINUS its ``command_targets_local_commit``
    public-local downgrade: the banned-terms gate may downgrade an unreadable
    PUBLIC commit because the pre-push gate re-scans commit messages for banned
    terms; the quote gate may NOT, because ``privacy-scan`` carries no
    verbatim-quote detector (see the module docstring). Config resolution is the
    default env/home one (the live gate passes no explicit config; tests pin it
    via ``T3_BANNED_TERMS_CONFIG``).
    """
    from teatree.hooks import public_visibility, publish_surface  # noqa: PLC0415 — deferred: cold-hook import

    if public_visibility.gate_skips_for_visibility(command, cwd):
        return QuoteVerdict(deny=False, warning=_NON_PUBLIC_SKIP, decision="allow-nonpublic-destination")
    if publish_surface.command_targets_private_only(command, cwd):
        return QuoteVerdict(deny=False, warning=_PRIVATE_REPO_WARNING, decision="warn-private-repo")
    return QuoteVerdict(deny=True, warning=None, decision="deny")
