"""Affirmative-public visibility scope for the pre-publish leak gates (#1415/#1213).

The banned-terms (#1415) and quote-scanner (#1213) gates protect against
leaking internal vocabulary / user quotes onto PUBLIC surfaces. A segment is
skip-eligible ONLY when its target is PROVABLY non-public: an allowlisted-private
slug, an internal-namespace slug, or a ``private``/``internal`` probe verdict. A
target the gate cannot prove non-public -- an affirmatively-``public`` probe
verdict, OR a RESOLVABLE ``owner/repo`` slug whose visibility probe could not be
confirmed (a network/API error, an absent ``gh``/``glab``, an unrecognised
answer) -- is scanned. The probe-error case FAILS CLOSED: it is never a silent
skip (#3442). This reconciles the Python scope with its bash mirror
(:file:`scripts/hooks/refuse-public-push-with-leak.sh`, whose undetermined-
visibility branch scans anyway) and the fail-closed-always leak-gate doctrine in
:file:`hooks/CLAUDE.md` -- both now agree that an unconfirmed visibility on a
resolvable target scans, never skips. The offline ``private_repos`` allowlist
remains the reliable, network-free way to declare a private repo so an
own-private post to it still skips without a probe.

**Owner decision, #3477: a GENUINELY-unresolvable publish destination is SCANNED,
not allowed through.** An EMPTY slug, or one carrying an unexpanded ``$VAR``, used
to classify ``NON_PUBLIC`` (skip-eligible) on the reasoning that there is no target
to probe. But ``$OWNER`` expands at run time and can expand to a PUBLIC repo, and
the sibling classifier :func:`publish_destination.is_public_destination` has always
treated both as PUBLIC -- the two disagreed, and the disagreement was the fail-OPEN
half. Both now resolve ``UNKNOWN`` -> scan. The cost is a scan on a command whose
target cannot be read; the cost of the other choice is an unscanned public egress.
Declare an own-private repo in ``private_repos`` to keep it skip-eligible offline.

The visibility verdict is resolved from the command's OWN target (the
``--repo``/``-R`` flag, the ``gh``/``glab api`` URL path, or the git remote of the
dir bash would run THAT segment in -- every ``cd`` in the chain re-points it,
:func:`_commit_repo_dir.segment_cwds`, else the ambient hook cwd), then classified into
:class:`~teatree.hooks.leak_policy.Visibility`: an allowlisted-private slug, an internal-namespace slug,
and a ``private``/``internal`` probe verdict resolve ``NON_PUBLIC``; a ``public``
probe verdict on a non-allowlisted slug is ``PUBLIC``; a resolvable slug the
probe cannot confirm is ``UNKNOWN`` (fail closed -> scan). The verdict is
day-cached per-repo by :func:`_repo_visibility.slug_visibility`, so repeated gate
evaluations never re-probe.

:func:`gate_skips_for_visibility` is the composed predicate the gates call. It
keeps the ALL-SEGMENTS anti-leak posture -- a ``$(...)``/transport construct, an
unrecognised chained executable (``sh -c``/``make``/``./x.sh``), or a raw
``api`` WRITE whose URL does not resolve are all NON-skippable, so an obscured
PUBLIC post can never hide behind a leading non-public segment. A ``git commit``
segment defers to the landing-repo carve-out and the #703 pre-push backstop and
is never skipped here.

This lives in its own module because :mod:`teatree.hooks.publish_destination`
and :mod:`teatree.hooks._repo_visibility` are both at the per-file LOC cap.
"""

import re
import sys
from pathlib import Path
from typing import Final

from teatree.hooks import _commit_carve_out, _commit_repo_dir, _repo_visibility
from teatree.hooks._command_parser import is_publish_command
from teatree.hooks._gh_glab_hiding import command_segments_with_raw, raw_has_live_substitution
from teatree.hooks._publish_detection import segment_is_api_read, segment_is_api_write
from teatree.hooks._python_rest_detection import find_python_forge_rest_urls, is_python_leader
from teatree.hooks.leak_policy import Visibility, scans_on_visibility
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

# A ``gh``/``glab`` executable token anywhere in a python script's text. The URL scan
# below cannot see where a forge CLI would post, so its presence keeps the fail-closed scan.
_FORGE_CLI_TOKEN_RE: Final = re.compile(r"(?<![\w./-])(?:gh|glab)(?![\w.-])")


def _python_rest_segment_verdict(words: list[str], command: str, *, config_path: Path | None) -> str | None:
    """Verdict for a python REST-publish segment, or ``None`` when the segment is not one.

    :func:`publish_destination._destination_from_python_script` resolves the target out
    of a URL literal in the segment's WORDS, which a heredoc body never reaches — bash
    tokenises ``python3 - <<'PY'`` to three words and keeps the script text outside them.
    So the shape the owner's own tooling posts with resolved no destination at all and
    fell through to the fail-closed scan, blocking a post that can only ever reach a
    project the config already declares internal.

    Resolution reads the WHOLE command rather than the segment: a heredoc body cannot be
    attributed to its segment, and a superset of targets can only make the all-targets
    rule below stricter.

    Skip-eligible only when EVERY resolved target is provably non-public — the first-match
    resolution the words-based resolver uses would let a script posting to a private AND a
    public project skip on the private one. A forge CLI in the script text is a destination
    no URL scan can read, so it keeps the scan too.
    """
    if not words or not is_python_leader(words[0]):
        return None
    targets = {(forge, slug) for forge, slug in find_python_forge_rest_urls(command)}
    if not targets:
        return None
    if _FORGE_CLI_TOKEN_RE.search(command):
        return _SCAN
    verdicts = {
        _visibility_segment_verdict(Destination(slug=slug, via="api", forge=forge), config_path=config_path)
        for forge, slug in targets
    }
    return _SKIP_PUBLISH if verdicts == {_SKIP_PUBLISH} else _SCAN


def destination_visibility(dest: Destination, *, config_path: Path | None = None) -> Visibility:
    """Classify a RESOLVED ``dest`` into :class:`~teatree.hooks.leak_policy.Visibility`.

    ``NON_PUBLIC`` (skip-eligible) only when the target is PROVABLY non-public: an
    ``internal_publish_namespaces`` match, a ``private_repos`` allowlist match, or
    a ``private``/``internal`` (any non-``PUBLIC``, non-``None``) probe verdict.
    ``PUBLIC`` only on a confirmed-``PUBLIC`` probe verdict for a non-allowlisted
    slug. ``UNKNOWN`` -- the fail-CLOSED case the gate must SCAN -- in the two
    can't-tell cases:

    * the slug IS probe-resolvable but the probe returns no verdict (``None`` --
        a network/API error, an absent ``gh``/``glab``, an unrecognised answer)
        (#3442); and
    * the destination is GENUINELY unresolvable -- an EMPTY slug, or one carrying
        an unexpanded ``$VAR`` whose run-time value could be a PUBLIC repo (#3477).
        This case used to resolve ``NON_PUBLIC``, disagreeing with the sibling
        classifier :func:`publish_destination.is_public_destination`, which has
        always scanned it. The two now agree, fail-closed.

    ``dest.forge`` qualifies a bare ``owner/repo`` slug up to its canonical host
    so the host-keyed probe routes to the right tool.
    """
    slug = dest.slug.strip().lower()
    if not slug or "$" in slug:
        return Visibility.UNKNOWN
    if any(_repo_visibility.slug_namespace_matches(entry, slug) for entry in _internal_publish_namespaces(config_path)):
        return Visibility.NON_PUBLIC
    if _repo_visibility.slug_is_allowlisted_private(slug, config_path):
        return Visibility.NON_PUBLIC
    probe_slug = _repo_visibility.forge_qualified_slug(slug, dest.forge)
    verdict = _repo_visibility.slug_visibility(probe_slug)
    if verdict == _PUBLIC:
        return Visibility.PUBLIC
    if verdict is None:
        return Visibility.UNKNOWN
    return Visibility.NON_PUBLIC


def is_affirmatively_public(dest: Destination | None, *, config_path: Path | None = None) -> bool:
    """Return True iff ``dest`` resolves to an affirmatively-PUBLIC repo.

    True ONLY on a confirmed-``PUBLIC`` probe verdict for a non-allowlisted slug
    (:attr:`Visibility.PUBLIC`); every other case -- private/internal/allowlisted
    (``NON_PUBLIC``) and a target the probe cannot confirm (``UNKNOWN``) -- is
    False. Callers that must FAIL CLOSED on ``UNKNOWN`` (the leak-gate scope) use
    :func:`destination_visibility` directly rather than this boolean, which cannot
    distinguish ``UNKNOWN`` from ``NON_PUBLIC``.
    """
    if dest is None:
        return False
    return destination_visibility(dest, config_path=config_path) is Visibility.PUBLIC


def _signal_probe_error_scan(slug: str) -> None:
    """Emit a loud, one-line stderr signal that a probe error drove a fail-CLOSED scan.

    A probe-error-driven scan must NEVER be silent (#3442): this mirrors the bash
    pre-push gate's ``echo ... >&2`` on undetermined visibility. Best-effort and
    crash-proof -- a failed stderr write never breaks the fast hook.
    """
    try:
        sys.stderr.write(
            f"leak gate: could not confirm '{slug or '<repo>'}' repo visibility "
            "(probe unavailable or errored) - scanning anyway (fail closed, #3442).\n"
        )
    except OSError:
        return


def _visibility_segment_verdict(dest: Destination, *, config_path: Path | None) -> str:
    """Map a RESOLVED destination's :class:`Visibility` to a segment verdict.

    The scan/skip half of the decision comes from the one policy
    (:func:`~teatree.hooks.leak_policy.scans_on_visibility`): only ``NON_PUBLIC``
    is skip-eligible. ``UNKNOWN`` (an unconfirmed probe, or a genuinely
    unresolvable target) FAILS CLOSED to :data:`_SCAN` and emits
    :func:`_signal_probe_error_scan` so the decision is never silent (#3442/#3477).
    """
    visibility = destination_visibility(dest, config_path=config_path)
    if not scans_on_visibility(visibility):
        return _SKIP_PUBLISH
    if visibility is Visibility.UNKNOWN:
        _signal_probe_error_scan(dest.slug)
    return _SCAN


def _api_write_segment_verdict(words: list[str], *, config_path: Path | None) -> str:
    """Segment verdict for a raw ``gh``/``glab api`` WRITE (see :func:`_visibility_segment_verdict`).

    A ``gh``/``glab api`` write carries its body only to the endpoint its URL
    path names. When that path resolves to a PROVABLY non-public repo (a
    probe-confirmed private/internal repo, or an allowlisted-private /
    internal-namespace slug), the write cannot leak to a public surface -- e.g. a
    private customer MR-description PUT -- so it is :data:`_SKIP_PUBLISH`. The slug
    must come from the URL path itself (``via="api"``): an ``-R`` flag does not
    constrain a raw endpoint.

    An UNRESOLVABLE endpoint FAILS CLOSED to :data:`_SCAN`. Per this module's
    ALL-SEGMENTS anti-leak contract a raw api WRITE with an unresolvable URL is
    non-skippable, because it is an immediate public egress with no pre-push
    backstop and a leading non-public segment must never route it around the leak
    scan. Unresolvable means: no ``api`` destination at all (a flagless call, an
    ambiguous unknown flag, a non-repo endpoint), OR a slug carrying an unexpanded
    ``$`` (a ``$OWNER``/``$VAR`` that could expand to a PUBLIC repo at run time --
    e.g. ``gh api "repos/$OWNER/repo/issues" -f body=...``). A slug that resolves
    but whose probe cannot confirm visibility ALSO fails closed (``UNKNOWN`` via
    :func:`_visibility_segment_verdict`, #3442).
    """
    if not words or words[0] not in {"gh", "glab"}:
        return _SCAN
    dest = _destination_from_api(words, words[0])
    if dest is None or dest.via != "api" or "$" in dest.slug:
        return _SCAN
    return _visibility_segment_verdict(dest, config_path=config_path)


# Span openers a LIVE substitution rides. The tail after each opener is checked
# for a DETECTABLE publish invocation (:func:`_command_parser.is_publish_command`),
# so a ``$(gh pr create …)`` hidden inside a body value can never ride a
# private-target skip, while a ``$(cat <path>)`` / ``$(git log …)`` body cannot.
_SUBSTITUTION_OPENERS: Final[tuple[str, ...]] = ("$(", "<(", ">(", "`")


def _live_substitution_hides_publish(raw: str) -> bool:
    """Return True iff a LIVE substitution span in ``raw`` carries a detectable publish.

    ``raw`` is one token's as-written source span. Only a substitution bash
    would EXPAND counts (:func:`raw_has_live_substitution` -- a single-quoted
    marker is inert literal text, #3357). For each live opener the tail of the
    token is run through the SAME publish detection the gates key on
    (:func:`is_publish_command`), so the detection posture inside a substitution
    equals the posture at the top level: a ``$(gh issue create …)`` /
    ``$(glab api … -f body=…)`` is a hidden publish (the caller must SCAN); a
    ``$(cat body.md)`` / ``$(echo x)`` is not. An undetectable wrapper inside a
    substitution (``$(./release.sh)``) is accepted exactly as it is at the top
    level, where a bare ``./release.sh`` is not a publish either -- the
    provably-private destination (#1213/#1415 skip scope) bounds the blast
    radius to a non-public surface.
    """
    if not raw_has_live_substitution(raw):
        return False
    for opener in _SUBSTITUTION_OPENERS:
        start = raw.find(opener)
        while start != -1:
            if is_publish_command(raw[start + len(opener) :]):
                return True
            start = raw.find(opener, start + len(opener))
    return False


def _construct_bearing_segment_verdict(
    words: list[str], raws: list[str], cwd: Path | None, *, command: str, config_path: Path | None
) -> str:
    """Verdict for a segment carrying a LIVE substitution or transport construct.

    A PROVABLY-private target has no public-leak surface, so a construct in the
    command must not force a scan-impossibility block on it (#1213/#1415: the
    ``-d "$(cat body.md)"`` / heredoc-writer shapes were hard-blocking every
    private ``glab mr create``, forcing the QUOTE_OK/ALLOW_BANNED_TERM escapes).
    The construct is dangerous only when it could carry a HIDDEN PUBLIC post, so
    the segment stays skip-eligible when ALL of:

    - no live substitution span detectably publishes
        (:func:`_live_substitution_hides_publish`);
    - the segment's destination resolves to a LITERAL slug (an unexpanded ``$``
        in the slug could name a PUBLIC repo at run time -> :data:`_SCAN`); and
    - that literal destination classifies ``NON_PUBLIC``
        (:func:`_visibility_segment_verdict` -- ``PUBLIC`` and probe-unconfirmed
        ``UNKNOWN`` both keep the fail-closed :data:`_SCAN`, #3442).

    A construct-bearing segment with NO destination is skip-eligible only when
    it is a provably-inert local segment (:func:`_segment_is_skip_inert` -- e.g.
    the ``cat > <bodyfile> <<EOF`` writer that materialises the post's own body
    file); every other no-destination construct still scans (:data:`_SCAN`).
    """
    if any(_live_substitution_hides_publish(raw) for raw in raws):
        return _SCAN
    if segment_is_api_write(words):
        return _api_write_segment_verdict(words, config_path=config_path)
    rest = strip_cd_prefix(words)
    python_verdict = _python_rest_segment_verdict(rest, command, config_path=config_path)
    if python_verdict is not None:
        return python_verdict
    dest = _destination_from_words(rest, cwd)
    if dest is not None:
        if "$" in dest.slug:
            return _SCAN
        return _visibility_segment_verdict(dest, config_path=config_path)
    return _SKIP_INERT if _segment_is_skip_inert(words) else _SCAN


def _segment_visibility_verdict(
    words: list[str], raws: list[str], cwd: Path | None, *, command: str, config_path: Path | None
) -> str:
    """Classify one top-level segment as :data:`_SCAN` / :data:`_SKIP_PUBLISH` / :data:`_SKIP_INERT`.

    A LIVE ``$(...)``/transport construct routes through
    :func:`_construct_bearing_segment_verdict`: it skips ONLY for a literal,
    provably-non-public destination (or a provably-inert local segment) with no
    detectable publish inside any substitution span -- a hidden public post, an
    unexpanded ``$`` in the slug, an affirmatively-PUBLIC or probe-unconfirmed
    target all still force :data:`_SCAN` (fail closed, #3442). A repo-targeted
    publish to a PROVABLY non-public target is :data:`_SKIP_PUBLISH`; a
    repo-targeted publish (structured or ``api`` WRITE) to an affirmatively-PUBLIC
    OR probe-unconfirmed target forces :data:`_SCAN`; an ``api`` read or an inert
    nav/local segment is :data:`_SKIP_INERT`.

    ``raws`` carries each token's as-written source span (index-aligned with
    ``words``) so the substitution check fires only on a marker bash would actually
    expand -- an inert marker inside a single-quoted body value does not force a
    scan on a private-target post (#3357).
    """
    if _segment_carries_substitution_or_transport(words, raws):
        return _construct_bearing_segment_verdict(words, raws, cwd, command=command, config_path=config_path)
    if segment_is_api_write(words):
        return _api_write_segment_verdict(words, config_path=config_path)
    if segment_is_api_read(words):
        return _SKIP_INERT
    return _plain_segment_verdict(words, cwd, command=command, config_path=config_path)


def _plain_segment_verdict(words: list[str], cwd: Path | None, *, command: str, config_path: Path | None) -> str:
    """Verdict for a segment carrying no substitution/transport construct and no ``api`` verb."""
    rest = strip_cd_prefix(words)
    python_verdict = _python_rest_segment_verdict(rest, command, config_path=config_path)
    if python_verdict is not None:
        return python_verdict
    dest = _destination_from_words(rest, cwd)
    if dest is not None:
        return _visibility_segment_verdict(dest, config_path=config_path)
    if rest and rest[0] in {"gh", "glab"}:
        # A ``gh``/``glab`` WRITE whose destination did NOT resolve (a flagless
        # verb with no resolvable cwd remote, a ``gh pr review 5 --body …`` shape)
        # is NOT provably non-public, so it FAILS CLOSED to a scan -- it is never
        # skip-eligible. Only a RESOLVED, provably-non-public dest (handled above)
        # earns ``_SKIP_PUBLISH`` (#F7.2). Previously this returned
        # ``_SKIP_PUBLISH`` and let an unresolved public post skip the leak scan.
        return _SCAN
    return _SKIP_INERT if _segment_is_skip_inert(words) else _SCAN


def gate_skips_for_visibility(command: str, cwd: Path | None, *, config_path: Path | None = None) -> bool:
    """Return True iff a pre-publish leak gate should SKIP ``command`` on visibility.

    SKIP (True) only when EVERY top-level segment is provably safe on visibility
    grounds and at least one is a repo-targeted publish: the leak gate enforces
    on every target it cannot PROVE non-public (#1415/#1213, #3442).

    EACH segment is classified against the dir bash would run THAT segment in
    (:func:`_commit_repo_dir.segment_cwds`), which every ``cd`` in the chain re-points
    -- the ambient hook ``cwd`` is routinely the workspace root while the command
    enters its own repo, and a LATER ``cd`` into a public clone must be judged on that
    clone, never on the first ``cd``'s repo. A segment is safe when it is one of:

    - a ``gh``/``glab``/``t3 review`` publish whose destination is a LITERAL
        slug that is PROVABLY non-public (allowlisted-private /
        internal-namespace / probe-confirmed private) -- a substitution/transport
        construct in the segment is tolerated as long as no live substitution
        span detectably publishes (:func:`_construct_bearing_segment_verdict`),
        so a ``-d "$(cat body.md)"`` / heredoc body toward a provably-private
        repo never needs an escape flag (#1213/#1415);
    - a raw ``gh``/``glab api`` WRITE whose URL path resolves to a PROVABLY
        non-public repo (:func:`_api_write_segment_verdict`);
    - a read-only ``api`` GET (posts no body); or
    - a provably-inert navigation / local-only / git-transport segment
        (:func:`publish_destination._segment_is_skip_inert`).

    Do NOT skip (False) when:

    - any repo-targeted publish resolves to an affirmatively-PUBLIC repo (the
        gate must fire to catch a real public leak);
    - any repo-targeted publish resolves to a RESOLVABLE slug whose visibility
        the probe cannot confirm -- a network/API error, an absent ``gh``/``glab``,
        or an unrecognised answer. This FAILS CLOSED (scans) and emits a loud
        signal, mirroring the bash pre-push gate and the fail-closed-always
        leak-gate doctrine (#3442); the offline ``private_repos`` allowlist is the
        network-free way to keep an own-private post skip-eligible;
    - a segment carries a live substitution span that DETECTABLY publishes
        (``-d "$(gh pr create …)"``), resolves a slug carrying an unexpanded
        ``$``, is an unrecognised chained executable (``sh -c``/``make``/
        ``./x.sh`` -- can shell out to a hidden public post), or is a raw ``api``
        WRITE to an affirmatively-public or unresolvable URL -- these keep the
        ALL-SEGMENTS anti-leak posture so an obscured public post cannot hide
        behind a leading non-public segment;
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
    for (words, raws), segment_cwd in zip(segments, _commit_repo_dir.segment_cwds(command, cwd), strict=True):
        verdict = _segment_visibility_verdict(words, raws, segment_cwd, command=command, config_path=config_path)
        if verdict == _SCAN:
            return False
        if verdict == _SKIP_PUBLISH:
            saw_repo_publish = True
    return saw_repo_publish
