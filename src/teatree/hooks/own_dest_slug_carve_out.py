r"""Own-destination-slug carve-out for the banned-terms posting gate (#2597).

Sibling of the own-repo-URL carve-out (:mod:`own_repo_url_carve_out`) and the
own-slug commit downgrade (``_commit_carve_out.own_slug_term_downgrades``). It
closes the #2597 false positive: a status comment that mentions the overlay
name, posted to the overlay's OWN private tracker whose repo slug literally
carries that name. On a private tracker the overlay name is the repo's own
identity, not a foreign leak -- yet the banned-terms posting gate fired on it,
forcing the ``ALLOW_BANNED_TERM=1`` escape on every internal status post.

The carve-out is DERIVED, never hardcoded, and -- unlike the ``private_repos``
carve-outs -- needs NO config: it keys off the RESOLVED DESTINATION SLUG. A
post whose own destination repo path carries the tripped term proves itself the
overlay's own repo; a repo whose forge path already carries the (private)
overlay name has by construction already published that name in its slug, so a
comment to it is not a NEW leak the gate could prevent. It is the posting-path
sibling of ``_commit_carve_out.own_slug_term_downgrades`` (#1951), which keys
off ``private_repos`` for the commit surface.

The matching reuses the whole-token matcher (:mod:`teatree.hooks.term_match`)
and the per-segment destination resolution / fail-closed segment predicates of
:mod:`teatree.hooks.publish_destination`, so the destination resolution stays in
one place across the destination skip and this downgrade. The predicate is
fail-safe-to-block: it downgrades ONLY when EVERY top-level segment is a
structured post whose resolved destination slug carries the term (or a provably
inert nav segment), with no substitution / transport / raw-REST segment -- so a
chained or substituted post to a public repo whose slug omits the term defeats
the downgrade and that public surface stays hard-blocked.
"""

from pathlib import Path

from teatree.hooks._gh_glab_hiding import command_segments
from teatree.hooks._publish_detection import segment_is_api_read, segment_is_api_write
from teatree.hooks.publish_destination import (
    _destination_from_words,
    _segment_carries_substitution_or_transport,
    _segment_is_skip_inert,
)
from teatree.hooks.term_match import _contains_run, tokens


def _term_is_slug_token_run(term: str, slug: str) -> bool:
    """Return True iff ``term``'s tokens form a contiguous run within ``slug``'s tokens.

    The org-prefix token ``democorp`` of ``democorp-eng/tracker`` matches, a
    substring ``demo`` of ``democorp`` does not (whole-token), and a SUPERSET
    ``democorp-services`` is a longer run than the slug carries and so is NOT
    contained. An empty term tokenizes to nothing and never matches
    (``_contains_run`` rejects an empty needle).
    """
    return _contains_run(tokens(slug), tokens(term))


def term_is_destination_own_slug(command: str, term: str, cwd: Path | None) -> bool:
    """Return True iff ``term`` is the OWN repo-slug of EVERY publish destination of ``command``.

    The #2597 false positive: a status comment mentioning the overlay name,
    posted to the overlay's OWN private tracker whose repo slug literally carries
    that name. The name is the destination repo's own identity on its own
    surface, not a foreign leak -- so the banned-term match downgrades to a warn.
    A public destination whose slug does NOT carry the term (the canonical public
    ``souliane/teatree``) is not matched, so that surface stays hard-blocked
    (#2597 acceptance criterion 2).

    Fires ONLY when EVERY top-level segment is either a structured
    ``gh``/``glab``/``t3 review`` post whose resolved destination slug carries
    ``term`` as a token-run, or a provably-inert nav/local segment -- with at
    least one publish segment and no substitution/transport/raw-REST segment.
    A chained post to a PUBLIC repo whose slug does NOT carry the term therefore
    defeats the downgrade, and a raw ``api`` WRITE (which can carry its body to
    any endpoint) is never eligible. Mirrors
    :func:`publish_destination.gate_skips_destination`'s all-segments
    fail-closed posture.
    """
    segments = command_segments(command)
    if not segments:
        return False
    saw_publish = False
    for words in segments:
        if _segment_carries_substitution_or_transport(words):
            return False
        if segment_is_api_write(words):
            return False
        if segment_is_api_read(words):
            continue
        dest = _destination_from_words(words, cwd)
        if dest is not None:
            if not _term_is_slug_token_run(term, dest.slug):
                return False
            saw_publish = True
        elif not _segment_is_skip_inert(words):
            return False
    return saw_publish
