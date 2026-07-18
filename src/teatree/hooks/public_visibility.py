"""Affirmative-public visibility scope for the pre-publish leak gates (#1415/#1213).

The banned-terms (#1415) and quote-scanner (#1213) gates protect against
leaking internal vocabulary / user quotes onto PUBLIC surfaces. They therefore
enforce ONLY when the target repository is affirmatively known ``public``. For
every OTHER case -- a ``private`` or ``internal`` repo, an unknown/unresolvable
target, or an in-hook visibility lookup error -- the gate is SKIPPED (bias hard
toward not firing): a non-public repo must never be falsely blocked.

The visibility verdict is resolved from the command's OWN target (the
``--repo``/``-R`` flag, the ``gh``/``glab api`` URL path, or the cwd git remote
-- reusing ``publish_destination``'s resolver), then classified: an
allowlisted-private slug, an internal-namespace slug, a ``private``/``internal``
probe verdict, and an unknown verdict all resolve NON-public; only a ``public``
probe verdict on a non-allowlisted slug is public. The verdict is day-cached
per-repo by :func:`_repo_visibility.slug_visibility`, so repeated gate
evaluations never re-probe.

:func:`gate_skips_for_visibility` is the composed predicate the gates call. It
keeps the ALL-SEGMENTS anti-leak posture of the destination classifier it
replaced -- a ``$(...)``/transport construct, an unrecognised chained executable
(``sh -c``/``make``/``./x.sh``), or a raw ``api`` WRITE whose URL does not
resolve are all NON-skippable, so an obscured PUBLIC post can never hide behind
a leading non-public segment -- but with the destination polarity FLIPPED: a
segment is skip-eligible when its target is NOT affirmatively public (rather
than only when provably-internal), so an unknown/unresolvable target now SKIPS
instead of failing closed. A ``git commit`` segment defers to the landing-repo
carve-out and the #703 pre-push backstop and is never skipped here.

This lives in its own module because :mod:`teatree.hooks.publish_destination`
and :mod:`teatree.hooks._repo_visibility` are both at the per-file LOC cap.
"""

from pathlib import Path

from teatree.hooks import _commit_carve_out, _repo_visibility
from teatree.hooks._gh_glab_hiding import command_segments_with_raw
from teatree.hooks._publish_detection import segment_is_api_read, segment_is_api_write
from teatree.hooks.publish_destination import (
    Destination,
    _destination_from_api,
    _destination_from_words,
    _internal_publish_namespaces,
    _segment_carries_substitution_or_transport,
    _segment_is_skip_inert,
)
from teatree.hooks.publish_surface import strip_cd_prefix

_PUBLIC = "PUBLIC"

# Per-segment visibility verdicts feeding :func:`gate_skips_for_visibility`.
_SCAN = "scan"  # forces the whole command to scan (never skip) -- anti-leak
_SKIP_PUBLISH = "skip-publish"  # skip-eligible AND counts as a repo-targeted publish
_SKIP_INERT = "skip-inert"  # skip-eligible, not a publish (nav/local/api-read)


def is_affirmatively_public(dest: Destination | None, *, config_path: Path | None = None) -> bool:
    """Return True iff ``dest`` resolves to an affirmatively-PUBLIC repo.

    NON-public (False) when the slug is empty / carries an unexpanded ``$``,
    matches an ``internal_publish_namespaces`` entry, matches a ``private_repos``
    allowlist entry, or its ``gh``/``glab`` visibility verdict is anything other
    than ``"PUBLIC"`` (``private``/``internal``/unknown). ``dest.forge`` qualifies
    a bare ``owner/repo`` slug up to its canonical host so the host-keyed probe
    routes to the right tool.
    """
    if dest is None:
        return False
    slug = dest.slug.strip().lower()
    if not slug or "$" in slug:
        return False
    if any(_repo_visibility.slug_namespace_matches(entry, slug) for entry in _internal_publish_namespaces(config_path)):
        return False
    if _repo_visibility.slug_is_allowlisted_private(slug, config_path):
        return False
    probe_slug = _repo_visibility.forge_qualified_slug(slug, dest.forge)
    return _repo_visibility.slug_visibility(probe_slug) == _PUBLIC


def _api_write_targets_non_public(words: list[str], *, config_path: Path | None = None) -> bool:
    """Return True iff a raw ``api`` WRITE segment RESOLVES to a NON-public repo.

    A ``gh``/``glab api`` write carries its body only to the endpoint its URL
    path names. When that path resolves to a repo slug that is affirmatively NOT
    public (a probe-confirmed private/internal repo, or an allowlisted-private /
    internal-namespace slug), the write cannot leak to a public surface -- e.g. a
    private customer MR-description PUT -- so it is skip-eligible. The slug must
    come from the URL path itself (``via="api"``): an ``-R`` flag does not
    constrain a raw endpoint.

    An UNRESOLVABLE endpoint is NOT skip-eligible (returns False -> the caller
    forces a SCAN). Per this module's ALL-SEGMENTS anti-leak contract a raw api
    WRITE with an unresolvable URL is non-skippable, because it is an immediate
    public egress with no pre-push backstop and a leading non-public segment must
    never route it around the leak scan. Unresolvable means: no ``api``
    destination at all (a flagless call, an ambiguous unknown flag, a non-repo
    endpoint), OR a slug carrying an unexpanded ``$`` (a ``$OWNER``/``$VAR`` that
    could expand to a PUBLIC repo at run time -- e.g. ``gh api
    "repos/$OWNER/repo/issues" -f body=...``). Only a slug that resolves to an
    affirmatively non-public repo returns True.
    """
    if not words or words[0] not in {"gh", "glab"}:
        return False
    dest = _destination_from_api(words, words[0])
    if dest is None or dest.via != "api":
        return False
    if "$" in dest.slug:
        return False
    return not is_affirmatively_public(dest, config_path=config_path)


def _segment_visibility_verdict(
    words: list[str], raws: list[str], cwd: Path | None, *, config_path: Path | None
) -> str:
    """Classify one top-level segment as :data:`_SCAN` / :data:`_SKIP_PUBLISH` / :data:`_SKIP_INERT`.

    A LIVE ``$(...)``/transport construct or an unrecognised chained executable
    forces :data:`_SCAN` (the ALL-SEGMENTS anti-leak posture); a repo-targeted
    publish to an affirmatively-PUBLIC target forces :data:`_SCAN`; a repo-targeted
    publish (structured or ``api`` WRITE) to a NON-public or UNRESOLVABLE target
    is :data:`_SKIP_PUBLISH` (an unknown target skips, per #1415); an ``api``
    read or an inert nav/local segment is :data:`_SKIP_INERT`.

    ``raws`` carries each token's as-written source span (index-aligned with
    ``words``) so the substitution check fires only on a marker bash would actually
    expand -- an inert marker inside a single-quoted body value does not force a
    scan on a private-target post (#3357).
    """
    if _segment_carries_substitution_or_transport(words, raws):
        return _SCAN
    if segment_is_api_write(words):
        return _SKIP_PUBLISH if _api_write_targets_non_public(words, config_path=config_path) else _SCAN
    if segment_is_api_read(words):
        return _SKIP_INERT
    rest = strip_cd_prefix(words)
    dest = _destination_from_words(rest, cwd)
    if dest is not None:
        return _SCAN if is_affirmatively_public(dest, config_path=config_path) else _SKIP_PUBLISH
    if rest and rest[0] in {"gh", "glab"}:
        return _SKIP_PUBLISH
    return _SKIP_INERT if _segment_is_skip_inert(words) else _SCAN


def gate_skips_for_visibility(command: str, cwd: Path | None, *, config_path: Path | None = None) -> bool:
    """Return True iff a pre-publish leak gate should SKIP ``command`` on visibility.

    SKIP (True) only when EVERY top-level segment is provably safe on visibility
    grounds and at least one is a repo-targeted publish: the leak gate enforces
    ONLY on an affirmatively-public target (#1415/#1213). A segment is safe when
    it is one of:

    - a ``gh``/``glab``/``t3 review`` publish whose destination is NOT
        affirmatively public (private/internal/unknown/unresolvable) and carries
        no substitution/transport construct;
    - a raw ``gh``/``glab api`` WRITE whose URL path resolves to a NON-public
        repo (:func:`_api_write_targets_non_public`);
    - a read-only ``api`` GET (posts no body); or
    - a provably-inert navigation / local-only / git-transport segment
        (:func:`publish_destination._segment_is_skip_inert`).

    Do NOT skip (False) when:

    - any repo-targeted publish resolves to an affirmatively-PUBLIC repo (the
        gate must fire to catch a real public leak);
    - a segment carries a ``$(...)``/transport construct, is an unrecognised
        chained executable (``sh -c``/``make``/``./x.sh`` -- can shell out to a
        hidden public post), or is a raw ``api`` WRITE to an affirmatively-public
        URL -- these keep the ALL-SEGMENTS anti-leak posture so an obscured public
        post cannot hide behind a leading non-public segment;
    - the command carries a ``git commit`` segment (the landing-repo carve-out
        and the #703 pre-push backstop own that surface, unchanged); or
    - the command has no repo-targeted publish segment at all (a Slack/curl post
        is not repo-scoped, so this scope leaves it to the gate's default).
    """
    if _commit_carve_out.command_has_git_commit_segment(command):
        return False
    segments = command_segments_with_raw(command)
    if not segments:
        return False
    saw_repo_publish = False
    for words, raws in segments:
        verdict = _segment_visibility_verdict(words, raws, cwd, config_path=config_path)
        if verdict == _SCAN:
            return False
        if verdict == _SKIP_PUBLISH:
            saw_repo_publish = True
    return saw_repo_publish
