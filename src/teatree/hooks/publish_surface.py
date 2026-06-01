"""Publish-surface classification for the pre-publish gates (#126).

The quote-scanner (#1213) and banned-terms (#1415) gates exist to stop
leaks on PUBLIC surfaces -- public-repo issues/PRs, Slack, public REST
posts. A ``git commit`` to a PRIVATE repo is not a public surface: a
private repo's own customer/domain terms are exactly what its commits are
supposed to carry, and hard-blocking them forced an
``--allow-banned-term`` / ``--quote-ok`` override on every commit.

This module classifies a Bash command into one of two surface classes
so the gates can DOWNGRADE from hard-block to warn for the private-repo
commit case ONLY, while leaving every public surface hard-blocked:

``is_git_commit_command`` decides the command is a ``git commit`` -- the
one surface eligible for the private-repo carve-out.

``is_gh_glab_posting_command`` decides the command is a structured
``gh``/``glab`` PR/issue create-or-comment command (NOT ``gh api`` /
``glab api`` raw REST, NOT ``curl``/Slack) that posts to a specific
repo target. These are eligible for the carve-out ONLY when the target
repo is POSITIVELY known-private (resolved from ``--repo``/``-R`` flag
first, then CWD fallback). Unknown or public targets stay hard-blocked.

``commit_targets_private_repo`` / ``posting_command_targets_private_repo``
decide whether the commit's / posting command's resolved target repo is
known-private. The "is this repo private?" question (offline
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
# hidden-``gh``/``glab``-invocation detection lives in ``_gh_glab_hiding``
# (both split out for module-health LOC). Re-exported / re-imported here so
# existing callers and tests keep using the ``publish_surface`` names.
slug_for_cwd = _repo_visibility.slug_for_cwd
_command_segments = _gh_glab_hiding.command_segments
_command_hides_gh_glab = _gh_glab_hiding.command_hides_gh_glab
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


def posting_command_targets_private_repo(
    command: str,
    cwd: Path | None,
    *,
    config_path: Path | None = None,
) -> bool:
    """Return True iff EVERY posting segment's target repo is known-private.

    The command is split into segments; the posting segments
    (:func:`_segment_is_posting_verb`) are isolated and each resolves its OWN
    target (``--repo``/``-R`` first, then ``GH_REPO`` for ``gh``, then the CWD
    fallback -- never a sibling ``cd`` segment).

    Fail-closed rules:

    - ANY ``gh``/``glab`` hidden from the segment scan -- a count of
        ``gh``/``glab`` command-words exceeding the recognised top-level
        segments, or a ``$(gh``/backtick-``gh`` substitution marker => False
        (:func:`_command_hides_gh_glab`). The segment scan cannot resolve a
        wrapped/procsub/wrapper-word invocation's target, so its presence blocks
        the whole command -- otherwise a PUBLIC post hidden in ``... && ( gh ...
        --repo PUBLIC ...)`` or ``... && eval gh ... --repo PUBLIC`` would leak
        behind a private segment.
    - No posting segment => False (nothing eligible to downgrade).
    - ANY raw ``gh api`` / ``glab api`` segment => False. Raw REST can target
        an arbitrary surface, so its mere presence blocks the whole command.
    - Otherwise, the carve-out applies only when ALL posting segments target a
        known-private repo. One public/unknown target blocks the whole command
        -- a ``... && gh issue create --repo PUBLIC`` half would leak.
    """
    if _command_hides_gh_glab(command):
        return False
    segments = _command_segments(command)
    if any(_segment_is_raw_rest(words) for words in segments):
        return False
    posting = [words for words in segments if _segment_is_posting_verb(words)]
    if not posting:
        return False
    return all(_segment_target_is_private(words, cwd, config_path=config_path) for words in posting)


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

    if is_gh_glab_posting_command(command):
        return posting_command_targets_private_repo(command, cwd, config_path=config_path)

    return False


def _commit_branch_downgrades(command: str, cwd: Path | None, *, config_path: Path | None) -> bool:
    """Return True iff a ``git commit`` command may downgrade to warn.

    The commit body is private-repo-eligible only when the CWD repo is
    known-private AND any chained posting segment
    (``git commit && gh issue create --repo PUBLIC``) is ALSO entirely
    private -- that posting half would carry the SAME body to a public
    surface, so a public/unknown target there blocks the whole command.
    """
    if not commit_targets_private_repo(cwd, config_path=config_path):
        return False
    if is_gh_glab_posting_command(command):
        return posting_command_targets_private_repo(command, cwd, config_path=config_path)
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
