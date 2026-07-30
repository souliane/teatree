"""Offline, worktree-aware ``origin`` remote-URL resolution from ``.git/config``.

The cwd-remote half of the publish-surface destination resolution
(:func:`teatree.hooks._repo_visibility.slug_for_cwd`) must work inside the
restricted PreToolUse hook subprocess, whose inherited PATH frequently does NOT
resolve a bare ``git`` -- the same restriction the live-visibility probe
augments PATH around (:data:`_repo_visibility._PROBE_PATH_EXTRA`). Shelling out
to ``git remote get-url`` there raises ``FileNotFoundError`` and yields an empty
slug, so a flagless ``glab mr create`` / ``gh pr create`` resolves to NO
destination and the banned-terms gate (#1415) over-blocks a post to the user's
OWN private repo -- the offline ``[teatree] private_repos`` allowlist never got
a slug to match against.

This module reads the ``origin`` URL by PARSING ``.git/config`` directly -- no
subprocess, so it is immune to the restricted hook PATH. It resolves the config
the way git itself does for a linked worktree: a worktree's ``.git`` is a FILE
``gitdir: <path>`` pointing at the per-worktree admin dir under the main repo's
``.git/worktrees/<name>``; that dir's ``commondir`` names the shared common dir
holding the single ``config`` (remotes are shared across every worktree of a
repo). A main checkout's ``.git`` is a directory that IS its own common dir.

Detection is fail-safe: an unresolvable repo, config, or remote yields ``""``,
which keeps the destination ``None`` and the gate hard-blocking. The minimal
git-config reader covers the ordinary ``[remote "origin"] url = ...`` form; the
PATH-augmented ``git`` subprocess fallback in :mod:`_repo_visibility` handles
the rare configs an offline parse cannot read (e.g. an ``[include]``-redirected
url).
"""

import re
from pathlib import Path
from typing import Final

# A linked worktree's ``.git`` FILE points at its admin dir via this prefix.
_GITDIR_PREFIX: Final[str] = "gitdir:"

# ``[remote "<name>"]`` section header. Section names are case-insensitive in
# git config; the quoted subsection (the remote name) is case-sensitive, so it
# is captured and compared exactly rather than under the IGNORECASE flag.
_REMOTE_HEADER_RE: Final[re.Pattern[str]] = re.compile(r'^\[\s*remote\s+"([^"]*)"\s*\]', re.IGNORECASE)


def origin_url(cwd: Path, remote: str = "origin") -> str:
    """Return ``cwd``'s ``remote`` URL parsed offline from ``.git/config``.

    Never spawns a process, so it resolves the remote inside the restricted
    PreToolUse hook subprocess where a bare ``git`` is unresolvable. Returns
    ``""`` when the repo, its config, or the remote cannot be resolved.
    """
    config = _git_config_path(cwd)
    if config is None:
        return ""
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        return ""
    return _remote_url_from_config(text, remote)


def _git_config_path(cwd: Path) -> Path | None:
    """Resolve the shared ``config`` file governing ``cwd``'s repo, or ``None``."""
    git_dir = _git_dir(cwd)
    if git_dir is None:
        return None
    config = _common_dir(git_dir) / "config"
    return config if config.is_file() else None


def _git_dir(cwd: Path) -> Path | None:
    """Resolve ``cwd``'s ``.git`` admin dir, walking up parents like git does."""
    for directory in (cwd, *cwd.parents):
        dot_git = directory / ".git"
        if dot_git.is_dir():
            return dot_git
        if dot_git.is_file():
            return _gitdir_from_file(dot_git)
    return None


def _gitdir_from_file(dot_git: Path) -> Path | None:
    """Resolve the admin dir a linked worktree's ``.git`` FILE points at."""
    try:
        content = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not content.startswith(_GITDIR_PREFIX):
        return None
    target = content[len(_GITDIR_PREFIX) :].strip()
    if not target:
        return None
    git_dir = Path(target)
    if not git_dir.is_absolute():
        git_dir = (dot_git.parent / git_dir).resolve()
    return git_dir if git_dir.is_dir() else None


def _common_dir(git_dir: Path) -> Path:
    """Return the shared common dir holding ``config`` for ``git_dir``.

    A linked worktree's admin dir carries a ``commondir`` file naming the
    repo's shared common dir (where the single ``config`` and its remotes
    live); a main checkout's ``.git`` directory IS its own common dir.
    """
    commondir = git_dir / "commondir"
    if not commondir.is_file():
        return git_dir
    try:
        target = commondir.read_text(encoding="utf-8").strip()
    except OSError:
        return git_dir
    if not target:
        return git_dir
    common = Path(target)
    if not common.is_absolute():
        common = (git_dir / common).resolve()
    return common


def _remote_url_from_config(text: str, remote: str) -> str:
    """Return the ``url`` of section ``[remote "<remote>"]``, or ``""``.

    A minimal git-config reader for the ``url`` key only: section names are
    case-insensitive, the quoted remote name is case-sensitive, and the key
    name is case-insensitive. Lines beginning with ``#`` / ``;`` are comments.
    """
    in_remote = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            match = _REMOTE_HEADER_RE.match(line)
            in_remote = match is not None and match.group(1) == remote
            continue
        if in_remote:
            key, sep, value = line.partition("=")
            if sep and key.strip().lower() == "url":
                return value.strip()
    return ""
