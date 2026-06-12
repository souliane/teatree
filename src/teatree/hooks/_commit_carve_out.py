r"""Git-commit downgrade decisions for the private-repo carve-out.

Split out of :mod:`teatree.hooks.publish_surface` to keep that module under
the module-health LOC cap. This module owns the ``git commit`` half of the
#126 carve-out: whether a banned/quoted match on a commit BODY downgrades to
warn, given where the commit LANDS. The ``gh``/``glab`` posting half and the
shared structural primitives stay in :mod:`publish_surface`.

Two decision predicates. :func:`commit_branch_downgrades` is True when the
commit's landing repo is private (or a genuinely-unresolvable LOCAL commit that
cannot leak) AND every chained segment is provably publish-inert or a pure
private post. :func:`own_slug_term_downgrades` is True when the matched term IS
the repo's own slug (a ``[teatree] private_repos`` allowlist entry) and the
landing repo clears the same target check -- making an own-org work-item URL a
non-leak even when the bare-commit cwd resolution missed the worktree, while a
foreign term and a resolvable PUBLIC landing repo both keep the block.

The structural helpers (``command_segments``, ``segment_is_pure_gh_glab_post``,
substitution/transport token checks, repo-dir resolution) are imported from
their leaf modules; the three ``publish_surface``-local predicates it needs are
imported lazily inside the call to avoid an import cycle.
"""

from pathlib import Path
from typing import Final

from teatree.hooks import _commit_repo_dir, _gh_glab_hiding, _repo_visibility

# A forge-tool command word the body could be posted through. Detected as a
# SUBSTRING in any token so a forge invocation hidden inside a quoted shell
# string is not treated as publish-inert.
_FORGE_TOOL_MARKERS: Final[tuple[str, ...]] = ("gh", "glab", "curl")

# Bash network pseudo-devices: a redirect TO one of these exfiltrates over the
# network, so such a redirect target is NEVER treated as a benign local write.
_NETWORK_REDIRECT_TARGETS: Final[tuple[str, ...]] = ("/dev/tcp/", "/dev/udp/")


def commit_target_downgrades(command: str, cwd: Path | None, *, config_path: Path | None) -> bool:
    r"""Return True iff the commit BODY's repo target makes it downgrade-eligible.

    The repo the commit lands in is resolved by ``resolve_commit_dir`` -- a
    leading ``cd``/``pushd`` prefix, then ``--git-dir`` else the ``-C``-adjusted
    dir, never ``--work-tree`` -- anchored on the ambient ``cwd`` (a RELATIVE
    ``-C``/``cd`` target, e.g. a sub-agent's ``git -C ../worktree``, resolves
    against ``cwd``, never the cold hook's process cwd), else the ambient
    ``cwd`` itself for a plain commit; the nearest enclosing ``.git`` root is
    then walked up to. Three states: a known-PRIVATE enclosing repo is
    downgrade-eligible (True); a resolvable but PUBLIC/unknown enclosing repo
    hard-blocks (False), so a commit in the public clone keeps the block; NO
    resolvable commit dir at all (no ``cd``/``-C``/``--git-dir``, no ambient
    cwd, or the dir is in no git repo) FAILS-OPEN (True), because a local commit
    cannot leak and git rejects a commit outside a repo.

    The ``UNRESOLVABLE_REPO_DIR`` sentinel (a ``-C`` value carrying a
    substitution marker) hard-blocks rather than fail-opens.
    """
    from teatree.hooks.publish_surface import commit_targets_private_repo  # noqa: PLC0415

    commit_target = _commit_repo_dir.resolve_commit_dir(command, cwd)
    if commit_target == _commit_repo_dir.UNRESOLVABLE_REPO_DIR:
        return False
    if commit_target is None:
        return True
    repo_root = _commit_repo_dir.git_root_for_dir(Path(commit_target))
    if repo_root is None:
        return True
    return commit_targets_private_repo(repo_root, config_path=config_path)


def commit_branch_downgrades(command: str, cwd: Path | None, *, config_path: Path | None) -> bool:
    r"""Return True iff a ``git commit`` command may downgrade to warn.

    The body downgrade-eligibility is decided by :func:`commit_target_downgrades`;
    every CHAINED segment must additionally be PROVABLY publish-inert (no forge
    tool, no execution-transport or substitution construct) or a pure private
    ``gh``/``glab`` post. Any other publishing construct fails the proof and the
    hard-block stands -- the unresolvable-body fail-open never relaxes a chained
    public post.
    """
    from teatree.hooks.publish_surface import (  # noqa: PLC0415
        is_git_commit_command,
        segment_target_is_private,
        strip_cd_prefix,
    )

    if not commit_target_downgrades(command, cwd, config_path=config_path):
        return False
    for words in _gh_glab_hiding.command_segments(command):
        if is_git_commit_command(" ".join(words)):
            continue
        if segment_is_publish_inert(words):
            continue
        if _gh_glab_hiding.segment_is_pure_gh_glab_post(words) and segment_target_is_private(
            strip_cd_prefix(words), cwd, config_path=config_path
        ):
            continue
        return False
    return True


def command_has_git_commit_segment(command: str) -> bool:
    """Return True iff ANY top-level segment is a ``git commit``.

    Wider than :func:`publish_surface.is_git_commit_command`, which recognises a
    ``git commit`` only as the command's EFFECTIVE FIRST action (after a leading
    ``cd``/``pushd`` prefix). A ``git commit`` can also sit behind a NON-``cd``
    leading segment -- the agent's standard body-file idiom writes the message
    with a ``cat > <bodyfile> <<EOF … EOF`` heredoc-writer first, and a ``true
    &&`` / setup preamble has the same shape. Each segment is tested with the
    same per-segment recogniser ``commit_branch_downgrades`` already uses, so the
    commit segment is seen wherever it sits.

    The carve-out dispatch uses this so a ``git commit`` behind such a prefix
    routes to the commit downgrade path. Safety is preserved by the per-segment
    proof in :func:`commit_branch_downgrades`: a chained PUBLIC post in the same
    command fails the proof and keeps the hard-block.
    """
    from teatree.hooks.publish_surface import is_git_commit_command  # noqa: PLC0415

    return any(is_git_commit_command(" ".join(words)) for words in _gh_glab_hiding.command_segments(command))


def command_targets_private_only(command: str, cwd: Path | None, *, config_path: Path | None = None) -> bool:
    """Return True iff ``command`` is a private-only git commit / gh-glab post.

    The BODY-INDEPENDENT half of :func:`publish_surface.carve_out_applies`: it
    decides ONLY whether the command's destination is a provably-private surface
    (a ``git commit`` landing in a known-private repo, or a pure private
    ``gh``/``glab`` post), with NO reference to the publish payload. This is what
    an UNREADABLE-body case needs: when the body file cannot be read, the
    payload-driven ``carve_out_applies`` fails closed on the unresolved-body
    marker, but a commit to a PRIVATE repo is not a public surface at all, so
    whether the body is readable is irrelevant -- the commit lands in private
    history regardless of what its message says. A PUBLIC/unknown destination
    returns False, so an unreadable body to a public surface still hard-blocks
    (#1415).

    Secrets are deliberately NOT considered here -- the caller scans the wide
    secret surface separately and blocks a secret on every surface before this
    is reached (#1672). Same destination logic as ``carve_out_applies``: a
    ``git commit`` segment (recognised ANYWHERE, so the agent's heredoc
    body-file idiom ``cat > <bodyfile> <<EOF … EOF; git -C <wt> commit -F
    <bodyfile>`` and any ``true &&`` preamble route to the commit path, not just
    a commit that is the literal first word) -> :func:`commit_branch_downgrades`,
    else :func:`publish_surface.command_is_pure_private_gh_glab_post`.
    """
    from teatree.hooks.publish_surface import command_is_pure_private_gh_glab_post  # noqa: PLC0415

    if command_has_git_commit_segment(command):
        return commit_branch_downgrades(command, cwd, config_path=config_path)
    return command_is_pure_private_gh_glab_post(command, cwd, config_path=config_path)


def segment_is_publish_inert(words: list[str]) -> bool:
    r"""Return True iff ``words`` provably cannot publish a body externally.

    Publish-inert when the segment carries NO forge tool (``gh``/``glab``/
    ``curl`` as a substring of any token), NO command/process-SUBSTITUTION
    construct (``$(``/``<(``/``>(``/backtick -- a second unverifiable command),
    and NO redirect/here-doc that targets anything but a LOCAL FILE. A plain
    local file redirect / here-doc (``cat > <bodyfile> <<EOF … EOF``, ``printf
    '%s' … > <bodyfile>``) is the agent's standard idiom for MATERIALISING the
    commit's own ``-F`` body file, which is local I/O -- it cannot carry the
    body to a public surface -- so it stays inert; only a network-device
    redirect target (``> /dev/tcp/host/port``) exfiltrates and breaks the proof.
    Such a segment (``git push``, ``echo``, ``make build``, a body-file writer)
    cannot carry the commit body to a public surface.
    """
    for i, token in enumerate(words):
        if _gh_glab_hiding.token_has_substitution_marker(token):
            return False
        if any(marker in token for marker in _FORGE_TOOL_MARKERS):
            return False
        if _gh_glab_hiding.token_is_transport_construct(token) and not _transport_token_is_local_redirect(
            token, words, i
        ):
            return False
    return True


def _transport_token_is_local_redirect(token: str, words: list[str], i: int) -> bool:
    """Return True iff a transport ``token`` is a redirect/here-doc to a LOCAL file.

    A group/subshell opener (``(``/``{``/``)``/``}``) is never a redirect, so it
    is not local-benign. A here-doc (``<<EOF``) writes local content. A redirect
    operator's target -- glued (``>file``) or the next token (``> file``) -- must
    not be a network pseudo-device (``/dev/tcp/``/``/dev/udp/``). The target
    token is NOT consumed by the caller: it stays in the per-token scan so a
    substitution / forge marker hidden in the redirect target (``> >(curl …)``)
    still breaks the proof on its own pass.
    """
    if not _gh_glab_hiding.token_is_redirect_operator(token):
        return False
    if token.startswith("<<"):
        return True
    operator = next((op for op in (">>", ">|") if token.startswith(op)), token[:1])
    glued = token[len(operator) :]
    target = glued or (words[i + 1] if i + 1 < len(words) else "")
    return not any(target.startswith(net) for net in _NETWORK_REDIRECT_TARGETS)


def own_slug_term_downgrades(command: str, term: str, cwd: Path | None, *, config_path: Path | None) -> bool:
    """Return True iff a ``git commit`` tripped on its OWN repo-slug term and may downgrade.

    A work-item URL naming the repo (``host/<org>/<repo>/-/issues/N``) is the
    repo's own identity, not a foreign leak. Fires ONLY for a ``git commit``
    (never a ``gh``/``glab`` post), ONLY when ``term`` is (a token-run of) a
    ``private_repos`` allowlist entry -- the whole org/repo slug OR its org
    prefix, since the scanner reports the prefix token tokenized out of a
    work-item URL (#1958); a foreign customer term is neither and stays blocked.

    The landing repo must still clear :func:`commit_branch_downgrades` -- be
    PROVABLY private (allowlist or probe) or a genuinely-unresolvable LOCAL
    commit -- so a resolvable PUBLIC/UNKNOWN landing keeps the hard block (the
    commit could genuinely land in a public repo; never a leak). The SAME
    per-segment chain proof runs, so an own-slug term never relaxes a chained
    PUBLIC post: ``is_git_commit_command`` matches the FIRST segment only and
    the scanner reports the FIRST matched term, so a chained ``&& gh issue
    create --repo <PUBLIC>`` still defeats the downgrade.

    The over-block this closes (#1958): the org PREFIX of a multi-token private
    slug now qualifies as the repo's own identity, so an own-org work-item URL
    whose scanner-reported token is the prefix downgrades on the repo's OWN
    private commit exactly as the whole-slug spelling already did.
    """
    from teatree.hooks.publish_surface import is_git_commit_command  # noqa: PLC0415

    if not is_git_commit_command(command):
        return False
    if not _repo_visibility.term_is_own_repo_slug(term, config_path):
        return False
    return commit_branch_downgrades(command, cwd, config_path=config_path)
