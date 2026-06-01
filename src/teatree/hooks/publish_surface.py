"""Publish-surface classification for the pre-publish gates (#126).

The quote-scanner (#1213) and banned-terms (#1415) gates exist to stop
leaks on PUBLIC surfaces -- public-repo issues/PRs, Slack, public REST
posts. A ``git commit`` to a PRIVATE repo is not a public surface: a
private repo's own customer/domain terms are exactly what its commits are
supposed to carry, and hard-blocking them forced an
``--allow-banned-term`` / ``--quote-ok`` override on every commit.

This module decides whether a banned/quoted match on a Bash command should
DOWNGRADE from hard-block to warn, for two private surfaces ONLY -- a
``git commit`` and a pure ``gh``/``glab`` post -- while leaving every public
surface hard-blocked.

The carve-out is an ALLOWLIST, not a denylist. Six prior cycles tried to
DETECT a hidden public ``gh``/``glab`` invocation by enumerating transport
mechanisms (shell ``-c``, ``env -S``, here-string, ``eval``, pipe-to-shell,
...); every cycle a new un-enumerated transport leaked. Static analysis of
"will this command, by ANY means, post to a public repo" is undecidable, so
enumeration cannot win. :func:`command_is_pure_private_gh_glab_post` INVERTS
the model: it PROVES the whole command is a pure private post and fails closed
on anything it cannot prove. A hidden public post is then impossible -- it
requires a second non-``gh`` verb, a transport construct, or a public
``--repo``, all of which fail the proof.

``is_git_commit_command`` decides the command's first segment is a
``git commit`` -- one surface eligible for the private-repo carve-out (its
chained segments must be provably publish-inert or pure private posts).

``command_is_pure_private_gh_glab_post`` is the single positive decision for
the posting path: EVERY top-level segment is a benign ``cd``/env navigation
segment OR a structurally-pure ``gh``/``glab`` invocation (NOT ``gh api`` /
``glab api`` raw REST, NOT ``curl``/Slack) targeting a POSITIVELY known-private
repo (``--repo``/``-R`` LAST-WINS, then ``GH_REPO`` for ``gh``, then CWD), with
at least one posting segment. The structural half (per-token purity) lives in
:mod:`teatree.hooks._gh_glab_hiding`.

``commit_targets_private_repo`` decides whether the commit's resolved CWD repo
is known-private. The "is this repo private?" question (offline
``[teatree] private_repos`` allowlist first, then a cached ``gh``/``glab``
visibility probe) lives in :mod:`teatree.hooks._repo_visibility`. Detection
is conservative and offline-first; an unknown/unresolvable repo is treated
as NOT private so the gate stays hard-blocking, never weakened by a
detection failure.

Secrets (API keys, tokens) are blocked on EVERY surface regardless of
the carve-out -- see :func:`contains_secret`.
"""

import os
import re
from pathlib import Path
from typing import Final

from teatree.hooks import _gh_glab_hiding, _repo_visibility
from teatree.hooks._command_parser import first_segment_words

# Repo-visibility / privacy resolution lives in ``_repo_visibility``; the
# structural purity primitives (segment splitting, per-token classification)
# live in ``_gh_glab_hiding`` (both split out for module-health LOC).
# Re-exported / re-imported here so existing callers and tests keep using the
# ``publish_surface`` names.
slug_for_cwd = _repo_visibility.slug_for_cwd
_command_segments = _gh_glab_hiding.command_segments
_segment_is_pure_gh_glab_post = _gh_glab_hiding.segment_is_pure_gh_glab_post
_strip_benign_prefix = _gh_glab_hiding.strip_benign_prefix
_token_has_substitution_marker = _gh_glab_hiding.token_has_substitution_marker
_token_is_transport_construct = _gh_glab_hiding.token_is_transport_construct
_ENV_ASSIGNMENT_RE = _gh_glab_hiding.ENV_ASSIGNMENT_RE

# ``git commit`` is the first command name + verb (after any env prefix).
_COMMIT_WORD_COUNT: Final[int] = 2

# A posting segment is ``<tool> <sub> <verb>`` at minimum (e.g. ``gh pr
# create``); a raw-REST segment is ``<tool> api`` at minimum.
_POSTING_WORD_COUNT: Final[int] = 3
_RAW_REST_WORD_COUNT: Final[int] = 2

# Eligible ``gh`` sub-command pairs: (tool, verb) where "tool" is the
# second word (pr/issue) and "verb" is the third word (create/comment).
# ``gh api`` is NOT in this set -- raw REST can target arbitrary surfaces.
_GH_ELIGIBLE_VERBS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("pr", "create"),
        ("pr", "comment"),
        ("issue", "create"),
        ("issue", "comment"),
    }
)

# Eligible ``glab`` sub-command pairs. ``glab api`` is NOT in this set.
_GLAB_ELIGIBLE_VERBS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("mr", "create"),
        ("mr", "note"),
        ("issue", "create"),
        ("issue", "note"),
    }
)


def is_git_commit_command(command: str) -> bool:
    """Return True iff the first command segment is a ``git commit``.

    A leading inline env assignment (``FOO=1 git commit``) is skipped so
    the command name resolves to ``git``.
    """
    words = first_segment_words(command)
    while words and _ENV_ASSIGNMENT_RE.fullmatch(words[0]):
        words = words[1:]
    return len(words) >= _COMMIT_WORD_COUNT and words[0] == "git" and words[1] == "commit"


def _segment_is_posting_verb(words: list[str]) -> bool:
    """Return True iff ``words`` is an eligible ``gh``/``glab`` posting verb.

    Eligible: ``gh pr create``, ``gh pr comment``, ``gh issue create``,
    ``gh issue comment``, ``glab mr create``, ``glab mr note``,
    ``glab issue create``, ``glab issue note``.
    """
    if len(words) < _POSTING_WORD_COUNT:
        return False
    tool, sub, verb = words[0], words[1], words[2]
    if tool == "gh":
        return (sub, verb) in _GH_ELIGIBLE_VERBS
    if tool == "glab":
        return (sub, verb) in _GLAB_ELIGIBLE_VERBS
    return False


def _segment_is_raw_rest(words: list[str]) -> bool:
    """Return True iff ``words`` is a raw ``gh api`` / ``glab api`` REST call.

    Raw REST can target any surface (an arbitrary endpoint, a public repo),
    so a command carrying ANY such segment can leak and the carve-out must
    fail closed on the whole command.
    """
    return words[0] in {"gh", "glab"} and len(words) >= _RAW_REST_WORD_COUNT and words[1] == "api"


def is_gh_glab_posting_command(command: str) -> bool:
    """Return True iff ANY command segment is an eligible ``gh``/``glab`` posting verb.

    Eligible: ``gh pr create``, ``gh pr comment``, ``gh issue create``,
    ``gh issue comment``, ``glab mr create``, ``glab mr note``,
    ``glab issue create``, ``glab issue note``.

    NOT eligible: ``gh api`` / ``glab api`` (raw REST -- can target any
    surface), ``gh repo view``, ``glab mr list``, or anything that is not
    a structured create-or-comment verb against a single repo target.

    Every segment is inspected (not just the first), so a posting verb
    behind a leading ``cd ... &&`` / env-assignment prefix is still seen.
    The carve-out uses this to gate which posting commands may be
    downgraded from hard-block to warn when the target repo is positively
    known-private.
    """
    return any(_segment_is_posting_verb(words) for words in _command_segments(command))


def commit_targets_private_repo(cwd: Path | None, *, config_path: Path | None = None) -> bool:
    """Return True iff a commit in ``cwd`` targets a known-private repo.

    Offline-first: the ``[teatree] private_repos`` slug-substring allowlist is
    consulted before any network probe, so a fully-offline session still gets
    the carve-out for declared repos. The cached ``gh``/``glab`` visibility
    probe is the fallback. An unresolvable repo is NOT private -- detection
    failure never weakens the gate.
    """
    if cwd is None:
        return False
    slug = _repo_visibility.slug_for_cwd(cwd)
    if not slug:
        return False
    if _repo_visibility.slug_is_allowlisted_private(slug, config_path):
        return True
    return _repo_visibility.slug_is_private(slug)


def _extract_repo_flag(words: list[str]) -> str:
    """Extract the EFFECTIVE ``--repo``/``-R`` value, or return ``""``.

    ``gh`` and ``glab`` resolve a repeated ``--repo``/``-R`` flag LAST-WINS
    (the same effective-resolution rule as ``-X GET -X POST`` for the HTTP
    method). Reading the FIRST match would let a crafted command claim a
    private slug while the tool actually posts to a trailing PUBLIC slug --
    a leak that defeats the carve-out's load-bearing safety property. So
    this scans the WHOLE word list and keeps the LAST occurrence.

    All four forms are recognised and the last one anywhere wins regardless
    of form: ``--repo X``, ``--repo=X``, ``-R X``, ``-R=X``.
    """
    found = ""
    i = 0
    while i < len(words):
        w = words[i]
        if w in {"--repo", "-R"} and i + 1 < len(words):
            found = words[i + 1]
            i += 2
            continue
        if w.startswith("--repo="):
            found = w[len("--repo=") :]
        elif w.startswith("-R="):
            found = w[len("-R=") :]
        i += 1
    return found


def _segment_target_slug(words: list[str], cwd: Path | None) -> str:
    """Resolve THIS posting segment's own target slug, mirroring gh/glab.

    Resolution order, scoped to ``words`` (never to a sibling ``cd``
    segment -- a ``cd`` in another segment does NOT change where gh/glab
    posts):

    - ``--repo``/``-R`` from this segment (explicit flag, LAST-WINS).
    - For ``gh`` ONLY: the ``GH_REPO`` env var, when no flag is present.
        ``gh`` reads ``GH_REPO`` as its default target; the hook shares the
        process environment gh inherits, so ``os.environ`` reflects it.
        ``glab`` has no equivalent env var, so this step is skipped for it.
    - The CWD origin slug, as the final fallback.

    Unresolvable/empty => ``""`` (caller treats as NOT private).
    """
    explicit_repo = _extract_repo_flag(words)
    if explicit_repo:
        return explicit_repo
    if words[0] == "gh" and os.environ.get("GH_REPO", ""):
        return os.environ["GH_REPO"]
    if cwd is not None:
        return _repo_visibility.slug_for_cwd(cwd)
    return ""


def _segment_target_is_private(words: list[str], cwd: Path | None, *, config_path: Path | None) -> bool:
    """Return True iff this posting segment's resolved target is known-private.

    An explicit ``--repo owner/name`` slug has no host prefix; it is matched
    against the allowlist as-is, then passed to the visibility probe directly
    (``gh`` probe for GitHub slugs, ``glab`` probe requires the host to detect
    GitLab; a bare ``owner/name`` defaults to the GitHub probe path).

    Unknown/unresolvable target => NOT private (default-deny preserved).
    """
    slug = _segment_target_slug(words, cwd)
    if not slug:
        return False
    if _repo_visibility.slug_is_allowlisted_private(slug, config_path):
        return True
    return _repo_visibility.slug_is_private(slug)


def command_is_pure_private_gh_glab_post(
    command: str,
    cwd: Path | None,
    *,
    config_path: Path | None = None,
) -> bool:
    r"""Return True iff the WHOLE command is PROVABLY a pure private gh/glab post.

    This is the carve-out's single positive decision predicate -- the
    INVERSION of the old "detect a hidden public invocation" denylist. Rather
    than enumerate every transport mechanism a public post could hide behind
    (shell ``-c``, ``env -S``, here-string, ``eval``, pipe-to-shell,
    ``source <(...)``, ``ssh host gh ...``, ``node -e "...gh..."``,
    ``make`` with a ``gh`` recipe, ...), which is an unbounded list that leaks
    on every un-enumerated construct, this PROVES the command is entirely good
    and fails closed on anything it cannot prove.

    The command must have at least one segment and EVERY top-level segment
    (``&&`` / ``||`` / ``;`` / ``|`` / ``&`` / newline) must be, all of:

    - STRUCTURALLY a pure ``gh``/``glab`` posting invocation
        (:func:`_gh_glab_hiding.segment_is_pure_gh_glab_post`) -- ``gh``/``glab``
        EXACTLY at ``words[0]`` after a benign ``cd <path>`` / ``VAR=value``
        prefix, every token a flag / opaque flag-value / positional with no
        execution-transport or substitution construct anywhere;
    - a RECOGNISED posting verb (:func:`_segment_is_posting_verb`) -- ``gh pr
        create``, ``gh issue comment``, ``glab mr create``, ... but NOT
        ``gh api`` / ``glab api`` raw REST (which can target any surface) nor a
        read verb (``gh issue view``); and
    - targeting a known-PRIVATE repo (:func:`_segment_target_is_private`) --
        ``--repo``/``-R`` LAST-WINS, then ``GH_REPO`` for ``gh``, then the CWD
        fallback. One public/unknown target fails the proof.

    A single non-conforming segment -- a second non-``gh`` verb, ANY transport
    construct, a raw-REST segment, a read verb, or a public/unknown target --
    makes the command not pure, so this returns False and the banned-term
    hard-block stands. A hidden public post is therefore impossible: it
    requires a second non-``gh`` verb, a transport construct, or a public
    ``--repo`` -- all of which fail the proof.

    Accepted (safe, recoverable) over-block: an exotic-but-legitimate private
    post that uses ``$()`` / a pipe / a here-doc / etc. in its body or chain
    fails the proof and is HARD-BLOCKED. That is the price of an unbypassable
    privacy guarantee -- the operator can split it into a plain post, and a
    hard-block is recoverable where a leak is not.
    """
    segments = _command_segments(command)
    if not segments:
        return False
    if not any(_segment_is_posting_verb(_strip_cd_prefix(words)) for words in segments):
        return False
    return all(_segment_proves_pure_private_post(words, cwd, config_path=config_path) for words in segments)


def _segment_proves_pure_private_post(words: list[str], cwd: Path | None, *, config_path: Path | None) -> bool:
    """Return True iff one top-level segment is provably good for the carve-out.

    A segment is good when it is EITHER a benign navigation segment (a bare
    ``cd <path>`` and/or ``VAR=value`` assignments, nothing else -- the
    ``cd /x &&`` / ``ENV=1 &&`` prefix the lexer splits into its own segment),
    OR a STRUCTURALLY pure ``gh``/``glab`` invocation
    (:func:`_segment_is_pure_gh_glab_post`) that is NOT raw REST
    (:func:`_segment_is_raw_rest`) and targets a known-PRIVATE repo. A chained
    private READ (``gh issue view 5 --repo PRIV``) is provably good -- it posts
    nothing and touches no public surface -- so it does not have to be a
    posting verb; the command-level proof separately requires at least one
    posting segment so a pure-read command is not eligible. Anything else --
    a second non-``gh`` verb, a transport construct, a raw-REST call, or a
    public/unknown target -- makes the segment not provably good, so the whole
    command fails the proof.
    """
    rest = _strip_cd_prefix(words)
    if not rest:
        return True
    return (
        _segment_is_pure_gh_glab_post(words)
        and not _segment_is_raw_rest(rest)
        and _segment_target_is_private(rest, cwd, config_path=config_path)
    )


def _strip_cd_prefix(words: list[str]) -> list[str]:
    """Return ``words`` with a leading ``cd <path>`` and ``VAR=value`` prefix removed.

    The posting-verb recognition and target resolution must see the ``gh``/
    ``glab`` invocation itself, not the benign ``cd``/env prefix that
    :func:`_gh_glab_hiding.segment_is_pure_gh_glab_post` already validates as
    benign. A malformed prefix (``cd`` with no path) collapses to the original
    words -- the structural proof has already rejected it, so the downstream
    posting/target checks are never reached for such a segment.
    """
    rest = _strip_benign_prefix(words)
    return rest if rest is not None else words


# -- Always-on secret detection -----------------------------------------------

# High-confidence secret shapes. These are blocked on EVERY surface,
# including a private-repo commit -- the carve-out is about a repo's own
# domain words, never about leaking a live credential into git history.
# The patterns are intentionally narrow (recognisable provider prefixes
# + length) to avoid false positives on ordinary prose.
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # GitHub personal-access / fine-grained / OAuth / app tokens.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b"),
    # GitLab personal/project/deploy tokens.
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    # Slack bot/user/app tokens.
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    # AWS access key id.
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    # Google API key.
    re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"),
    # OpenAI / Anthropic style secret keys.
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9-]{20,}\b"),
    # PEM private-key block header.
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
)


def contains_secret(text: str) -> bool:
    """Return True iff ``text`` carries a high-confidence secret shape.

    Used by both gates to keep secrets hard-blocked even on a private-repo
    commit that is otherwise eligible for the domain-word carve-out.
    """
    if not text:
        return False
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def carve_out_applies(
    tool_name: str,
    command: str,
    payload: str,
    cwd: Path | None,
    *,
    config_path: Path | None = None,
) -> bool:
    """Return True iff a HIGH/banned match on ``payload`` should DOWNGRADE.

    The private-repo carve-out applies when ALL hold:

    - The tool is ``Bash``.
    - The payload was actually resolved (fail-closed sentinel => hard-block).
    - The payload carries no high-confidence secret (credentials always leak).
    - The command is a ``git commit`` to a known-private CWD repo, OR a
        structured ``gh``/``glab`` create-or-comment command whose RESOLVED
        TARGET is positively known-private (--repo/-R first, CWD fallback).

    Ineligible regardless: ``gh api`` / ``glab api`` raw REST, ``curl``,
    Slack, and any non-structured verb. Public/unknown targets stay blocked.
    """
    from teatree.hooks._command_parser import is_fail_closed_sentinel  # noqa: PLC0415

    if tool_name != "Bash" or is_fail_closed_sentinel(payload) or contains_secret(payload):
        return False

    if is_git_commit_command(command):
        return _commit_branch_downgrades(command, cwd, config_path=config_path)

    return command_is_pure_private_gh_glab_post(command, cwd, config_path=config_path)


def _commit_branch_downgrades(command: str, cwd: Path | None, *, config_path: Path | None) -> bool:
    r"""Return True iff a ``git commit`` command may downgrade to warn.

    The first segment is the ``git commit`` (:func:`is_git_commit_command`),
    whose body is private-repo-eligible only when the CWD repo is known-private.
    Every CHAINED segment must additionally be PROVABLY publish-inert with
    respect to that body: either a pure private ``gh``/``glab`` post
    (:func:`command_is_pure_private_gh_glab_post` over that segment), or a
    segment that provably cannot carry the body to an external surface
    (:func:`_segment_is_publish_inert` -- no forge tool, no execution-transport
    or substitution construct anywhere). A chained ``&& gh ... --repo PUBLIC``,
    a ``&& sh -c "gh ... PUBLIC"``, or any other publishing construct that is
    not a proven pure private post fails the proof and the hard-block stands.

    This mirrors the posting-path inversion: the commit downgrades only when
    the WHOLE chain is provably good, never by failing to detect a hidden
    public post.
    """
    if not commit_targets_private_repo(cwd, config_path=config_path):
        return False
    for words in _command_segments(command):
        if is_git_commit_command(" ".join(words)):
            continue
        if _segment_is_publish_inert(words):
            continue
        if _segment_is_pure_gh_glab_post(words) and _segment_target_is_private(
            _strip_cd_prefix(words), cwd, config_path=config_path
        ):
            continue
        return False
    return True


# A forge-tool command word the body could be posted through. Detected as a
# SUBSTRING in any token so a forge invocation hidden inside a quoted shell
# string (``sh -c "gh ... PUBLIC"``) is not treated as publish-inert.
_FORGE_TOOL_MARKERS: Final[tuple[str, ...]] = ("gh", "glab", "curl")


def _segment_is_publish_inert(words: list[str]) -> bool:
    r"""Return True iff ``words`` provably cannot publish a body externally.

    A chained segment after a private ``git commit`` is publish-inert when it
    carries NO forge tool (``gh``/``glab``/``curl`` as a substring of any
    token -- so a forge call hidden in a quoted ``sh -c "gh ..."`` string is
    NOT inert) and NO execution-transport / substitution construct
    (substitution marker, redirection, group opener) anywhere. Such a segment
    (``git push origin main``, ``echo done``, ``make build``) cannot carry the
    commit body to a public surface, so it does not block the commit downgrade.

    This is the positive complement of the pure-post proof for the commit
    chain: a chained segment is either provably inert (here) or a proven pure
    private post; anything else fails the proof and the hard-block stands.
    """
    for token in words:
        if _token_has_substitution_marker(token) or _token_is_transport_construct(token):
            return False
        if any(marker in token for marker in _FORGE_TOOL_MARKERS):
            return False
    return True


def visibility_unknown_for_block(
    command: str,
    cwd: Path | None,
    *,
    config_path: Path | None = None,
) -> str | None:
    """Return the first target slug whose visibility is UNKNOWN in-hook, or ``None``.

    Read-only diagnostic for the deny path: it NEVER changes a verdict. When
    a banned-term block fires, this reports the first posting/commit target
    that is NEITHER allowlisted NOR probe-resolved (the probe returned
    ``None`` -- tool absent in-hook or auth differs), so the operator gets a
    one-line hint to add it to ``[teatree] private_repos`` for a reliable
    offline carve-out.

    Returns ``None`` when every resolvable target is allowlisted-private or
    genuinely PUBLIC (a public target is correctly blocked, not "unknown" --
    emitting the add-to-allowlist hint there would be misleading).
    """
    slugs: list[str] = []
    if is_git_commit_command(command) and cwd is not None:
        slugs.append(_repo_visibility.slug_for_cwd(cwd))
    slugs.extend(
        _segment_target_slug(words, cwd) for words in _command_segments(command) if _segment_is_posting_verb(words)
    )
    for slug in slugs:
        if not slug or _repo_visibility.slug_is_allowlisted_private(slug, config_path):
            continue
        if _repo_visibility.probe_visibility(slug) is None:
            return slug
    return None
