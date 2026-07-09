"""Read-before-overwrite gate for tracked user config / dotfiles.

Two real incidents motivate this gate: an agent overwrote a tracked dotfile
(a symlink into the user's dotfiles repo) via a blind ``Write``, and an agent
nearly restored a config file from git (``git checkout -- <config>``) without
first reading the live on-disk content — discarding uncommitted edits the user
had made directly on disk.

The shared rule: **a config / dotfile is authoritative as it exists on disk
right now**, even when that on-disk content diverges from the committed
version. Overwriting it (a full ``Write`` or an ``Edit`` whose ``old_string``
the agent assumed rather than read) or restoring it from git
(``git checkout`` / ``git restore`` that lands on the file) without first
reading the current content is a blind destructive write. The agent must read
the live content this session before clobbering it.

This module is the pure, overlay-agnostic decision core. The thin PreToolUse
hook (``hooks/scripts/config_overwrite_guard.py``) supplies the
``was-read-this-session`` predicate (the existing ``<session>.reads`` state)
and the deny emission; this module decides *whether* a tool call is a blind
config overwrite, never *how* to block it.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# A predicate the hook supplies: path -> "was it read this session?".
ReadPredicate = Callable[[str], bool]

# Bare config-file basenames (no leading dot) that are user configuration even
# when they live at a non-dotfile path — the canonical teatree config and the
# common dotfile-repo / home-config shapes. A leading-dot basename is ALWAYS a
# dotfile (handled separately), so these are the non-dotted exceptions.
_CONFIG_BASENAMES: frozenset[str] = frozenset(
    {
        ".teatree",
        "config.toml",
        "config.yaml",
        "config.yml",
        "settings.toml",
        "credentials.toml",
    }
)

# Path components that, when present, mark a file as user config / dotfiles —
# regardless of basename. ``.config`` is the XDG home; ``dotfiles`` is the
# conventional dotfiles-repo dir name. ANY file under ``.config`` is treated as
# user config regardless of suffix (``init.lua``, ``data.json``, a binary), so
# the suffix set below only matters for the ``dotfiles`` dir and dotfiles
# outside ``.config``.
_CONFIG_DIR_COMPONENTS: frozenset[str] = frozenset({".config", "dotfiles"})
_XDG_CONFIG_COMPONENT: str = ".config"

# Config file extensions that, when the file ALSO sits under a non-``.config``
# config dir (``dotfiles``) or is a dotfile, confirm config-ness. A bare
# ``.toml`` in a source tree is NOT user config; the dir/dotfile context is what
# qualifies it. Covers the common config/data shapes an editor or app keeps.
_CONFIG_SUFFIXES: frozenset[str] = frozenset(
    {".toml", ".cfg", ".ini", ".conf", ".rc", ".lua", ".json", ".yaml", ".yml"}
)

# git subcommands that RESTORE a NAMED path's working-tree content from a
# committed object, overwriting whatever is on disk at that path. Each
# discards uncommitted on-disk edits at the named path — exactly the authority
# this gate protects. ``git stash pop`` is deliberately OUT of scope: it takes
# a ``<stash>`` ref, not a path operand, and clobbers the whole working tree
# rather than a named config, so a path-operand scan cannot precisely target
# it — the Write/checkout/restore surfaces cover the named-config case cleanly.
_GIT_RESTORE_VERBS: tuple[str, ...] = ("checkout", "restore")

_GIT_RE = re.compile(r"\bgit\b")


@dataclass(frozen=True, slots=True)
class ConfigOverwriteFinding:
    """A blind config-overwrite attempt the gate refuses.

    ``path`` is the config/dotfile being clobbered (best-effort for the git
    paths). ``kind`` is the surface — ``"write"`` for a ``Write`` overwrite,
    ``"git-restore"`` for a git checkout/restore/stash that lands on the file.
    """

    path: str
    kind: str


_DOT_NAVIGATION: frozenset[str] = frozenset({".", ".."})


def _is_dotfile(name: str) -> bool:
    """True for a leading-dot basename (``.zshrc``, ``.gitconfig``)."""
    return name.startswith(".") and name not in _DOT_NAVIGATION


def _dotfile_is_config(name: str, *, under_config_dir: bool) -> bool:
    """Whether a leading-dot *name* qualifies as user config.

    A dotfile at home (``~/.zshrc``) or inside a dotfiles repo is config; a
    dotfile buried in a source tree (``.coveragerc`` next to code) qualifies
    only when it is a recognised rc / config shape.
    """
    if under_config_dir:
        return True
    return Path(name).suffix in _CONFIG_SUFFIXES or name in _CONFIG_BASENAMES or "rc" in name


def is_user_config_path(path: str) -> bool:
    """True iff *path* is a user config file or dotfile this gate protects.

    The predicate is deliberately broad on the config side and narrow
    elsewhere. A file is config when it is: a known config basename; ANY file
    under an XDG ``.config`` dir (regardless of suffix — ``init.lua``,
    ``data.json``, a binary); a dotfile (at home, in a ``dotfiles`` repo, or a
    recognised rc shape in a source tree); or a config-suffixed file under a
    ``dotfiles`` dir. A bare ``.toml`` deep in a ``src/`` tree is NOT user
    config. Empty / unparsable paths are not config (the caller skips them).
    """
    p = Path(path)
    name = p.name
    if not name:
        return False
    if name in _CONFIG_BASENAMES:
        return True
    parts = set(p.parts)
    if _XDG_CONFIG_COMPONENT in parts:
        # Everything an XDG ``.config`` dir holds is user config, suffix-agnostic.
        return True
    under_config_dir = bool(parts & _CONFIG_DIR_COMPONENTS)
    if _is_dotfile(name):
        return _dotfile_is_config(name, under_config_dir=under_config_dir)
    return under_config_dir and p.suffix in _CONFIG_SUFFIXES


def write_overwrites_existing(file_path: str, *, exists: bool) -> bool:
    """True iff a ``Write`` to *file_path* would OVERWRITE existing content.

    A ``Write`` to a path that does not yet exist creates a new file — there is
    no current content to discard, so the gate does not fire. ``exists`` is
    injected (the hook stats the live path, following symlinks) so this core
    stays filesystem-free and unit-testable.
    """
    return exists and is_user_config_path(file_path)


def _restore_targets_config(command: str) -> list[str]:
    """Best-effort: config paths a git-restore command would clobber.

    Scans the tokens after a ``checkout`` / ``restore`` verb (and the whole
    command for ``stash pop`` / ``stash apply``) for any token that is a user
    config path. Returns the matching paths; empty when the restore does not
    touch a config file (or is not a restore at all).
    """
    if not _GIT_RE.search(command):
        return []
    tokens = command.split()
    config_tokens: list[str] = []

    is_restore = any(
        tok == "git" and i + 1 < len(tokens) and tokens[i + 1] in _GIT_RESTORE_VERBS for i, tok in enumerate(tokens)
    )
    if not is_restore:
        return []

    for tok in tokens:
        # Skip flags and the verbs themselves; the path operands carry config.
        if tok.startswith("-") or tok in {"git", *_GIT_RESTORE_VERBS}:
            continue
        if is_user_config_path(tok):
            config_tokens.append(tok)
    return config_tokens


def find_blind_write(file_path: str, *, exists: bool, was_read: bool) -> ConfigOverwriteFinding | None:
    """Return a finding iff a ``Write`` is a blind config overwrite, else None.

    Fires only when ALL hold: the path is user config, the file already exists
    on disk (overwrite, not create), and it was NOT read this session. Reading
    the live content first clears the gate — that is the whole contract.
    """
    if not write_overwrites_existing(file_path, exists=exists):
        return None
    if was_read:
        return None
    return ConfigOverwriteFinding(path=file_path, kind="write")


def find_blind_git_restore(command: str, *, was_read: ReadPredicate) -> ConfigOverwriteFinding | None:
    """Return a finding iff a git-restore would blindly clobber a tracked config.

    ``was_read`` is a predicate ``(path) -> bool`` answering "was this path read
    this session?" — a git restore of a config the agent already read live is
    allowed; restoring one it never read discards uncommitted on-disk content
    sight-unseen and is refused. Returns the FIRST unread config target.
    """
    for target in _restore_targets_config(command):
        if not was_read(target):
            return ConfigOverwriteFinding(path=target, kind="git-restore")
    return None


def deny_reason(finding: ConfigOverwriteFinding) -> str:
    """The actionable deny message for a blind config-overwrite finding."""
    if finding.kind == "git-restore":
        return (
            f"BLOCKED: `git` would restore the tracked config `{finding.path}` from a "
            "committed/stashed version, discarding any uncommitted on-disk edits. "
            "The on-disk content is authoritative — Read it FIRST and confirm you intend "
            "to overwrite it. If the live content is genuinely the one you want to "
            "replace, add `[config-overwrite-ok: <reason>]` to the command to proceed."
        )
    return (
        f"BLOCKED: this Write would overwrite the existing config/dotfile `{finding.path}` "
        "without you having read its current content this session. The on-disk content is "
        "authoritative (it may carry uncommitted edits) — Read the file FIRST, confirm what "
        "you intend to change, then re-issue the Write. To overwrite deliberately, add "
        "`[config-overwrite-ok: <reason>]` to the Write content."
    )
