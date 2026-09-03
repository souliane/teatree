"""Shared pure logic for the containerized ``t3`` workflow (#3232).

Teatree runs exclusively in Docker — the ``t3`` CLI as well as every server — so
an operator needs no host Python / uv / prereqs. ``t3`` on the host is one fixed
thing: an executable LAUNCHER on ``PATH`` that ``exec``s the main checkout's
container-wrapping entry ``deploy/t3``, which ``docker compose exec``s into the
running worker. There is no second way to run ``t3``: a host CLI cannot reach the
control DB (:data:`teatree.paths.DEFAULT_CONTROL_DB_DIR` exists only inside the
container) and silently resolves every setting from shipped defaults.

One executable on ``PATH`` reaches every caller — an interactive shell, a script,
a git hook, cron, a sub-agent — so no shell alias is installed. A managed alias a
previous version wrote is retired instead (:func:`remove_alias_block`); it was
the split-brain, reaching interactive shells only.

Because the retired host CLI is also what used to WRITE the launcher, the
container writes it now, through a bind mount of the host's ``PATH`` bin dir at
:data:`CONTAINER_HOST_BIN_DIR` (``deploy/docker-compose.yml``). ``t3 setup``
(:mod:`teatree.cli.setup.docker_launcher`) installs through whichever side it
runs on; ``t3 doctor`` (:mod:`teatree.cli.doctor.checks_docker`) verifies the
result. Both consume the helpers here so the launcher script, its marker, and the
checkout it names never drift.
"""

import os
import tempfile
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

# Repo-relative locations of the compose stack and the container-wrapping entry.
COMPOSE_REL = Path("deploy") / "docker-compose.yml"
WRAPPER_REL = Path("deploy") / "t3"

# The service the wrapper execs into (kept in sync with deploy/t3's default).
DOCKER_CLI_SERVICE = "teatree-worker"

# Marker-delimited managed block a pre-launcher `t3 setup` wrote into the shell
# rc files. Only ever REMOVED now — kept in sync with deploy/t3's own retirement
# of the same block, which is what reaches the host rc from inside a container
# (tests/test_deploy_alias_retirement.py pins the two spellings).
ALIAS_MARKER_BEGIN = "# >>> teatree docker t3 alias >>>"
ALIAS_MARKER_END = "# <<< teatree docker t3 alias <<<"

# Stamped into the generated launcher so a re-run recognises its own file and
# refuses to overwrite anything it did not write.
LAUNCHER_MARKER = "# >>> teatree docker t3 launcher >>>"

# Where the compose stack mounts the host's `PATH` bin dir, so a containerized
# `t3 setup` can write the HOST launcher. Deliberately not path-identical: the
# container's own `~/.local/bin` is on its `PATH`, and the host launcher execs
# `docker compose`, so mounting there would make it resolvable inside.
CONTAINER_HOST_BIN_DIR = Path("/var/lib/teatree/host-bin")

# Names the HOST checkout holding `deploy/`, exported by deploy/t3 and deploy.sh
# and forwarded into the container — the launcher's exec target is rendered from
# it, so a relocated checkout is repaired from the checkout being invoked.
DEPLOY_CHECKOUT_ENV = "TEATREE_DEPLOY_CHECKOUT"

# The path fragment identifying a console script `uv tool install` left behind —
# the one pre-existing `t3` the launcher install is allowed to replace.
_UV_TOOL_LINK_FRAGMENT = f"{os.sep}uv{os.sep}tools{os.sep}"

_EXEC_PREFIX = 'exec "'
_EXEC_SUFFIX = '" "$@"'


class AliasRemoval(StrEnum):
    """Outcome of a :func:`remove_alias_block` call."""

    REMOVED = "removed"
    ABSENT = "absent"
    UNWRITABLE = "unwritable"


class LauncherInstall(StrEnum):
    """Outcome of an :func:`install_launcher` call."""

    INSTALLED = "installed"
    UPDATED = "updated"
    ALREADY_PRESENT = "already-present"
    REFUSED = "refused"
    UNWRITABLE = "unwritable"
    UNVERIFIED = "unverified"


def compose_path(repo: Path) -> Path:
    """Absolute path to the deploy compose stack in *repo*."""
    return (repo / COMPOSE_REL).resolve()


def wrapper_path(repo: Path) -> Path:
    """Absolute path to the container-wrapping ``t3`` entry in *repo*."""
    return (repo / WRAPPER_REL).resolve()


def launcher_bin_dir(env: Mapping[str, str]) -> Path:
    """The ``PATH`` directory owning the ``t3`` console script."""
    override = env.get("UV_TOOL_BIN_DIR", "").strip()
    if override:
        return Path(override)
    home = env.get("HOME", "").strip()
    return (Path(home) if home else Path.home()) / ".local" / "bin"


def launcher_path(env: Mapping[str, str]) -> Path:
    """Where the managed ``t3`` launcher lives — the one ``t3`` on ``PATH``."""
    return launcher_bin_dir(env) / "t3"


def render_launcher_script(repo: Path) -> str:
    """The launcher's contents: ``exec`` *repo*'s container-wrapping entry.

    The checkout is baked in at install time and the script never consults its
    own cwd, so ``t3`` means the same teatree from every directory.
    """
    return (
        "#!/usr/bin/env bash\n"
        f"{LAUNCHER_MARKER}\n"
        "# Managed by `t3 setup` — teatree runs only in Docker. Do not edit.\n"
        f"{_EXEC_PREFIX}{wrapper_path(repo)}{_EXEC_SUFFIX}\n"
    )


def read_managed_launcher(path: Path) -> str | None:
    """The launcher teatree wrote at *path*, or ``None`` when it wrote none."""
    if path.is_symlink() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return text if LAUNCHER_MARKER in text else None


def is_managed_launcher(path: Path) -> bool:
    """True when *path* is a teatree-written ``t3`` launcher."""
    return read_managed_launcher(path) is not None


def launcher_wrapper_target(script: str) -> Path | None:
    """The ``deploy/t3`` a managed launcher *script* execs, read back from itself.

    Lets a verifier ask which checkout an already-installed launcher points at
    without re-deriving it, so a launcher left behind by a moved or deleted
    checkout is a fact rather than an inference.
    """
    for line in script.splitlines():
        if line.startswith(_EXEC_PREFIX) and line.endswith(_EXEC_SUFFIX):
            return Path(line[len(_EXEC_PREFIX) : -len(_EXEC_SUFFIX)])
    return None


def _is_uv_tool_link(path: Path) -> bool:
    """True when *path* is the console-script symlink ``uv tool install`` leaves."""
    return path.is_symlink() and _UV_TOOL_LINK_FRAGMENT in str(path.readlink())


def _publish_launcher(path: Path, script: str) -> None:
    """Write *script* to a sibling temp file and rename it over *path*.

    The rename is what makes this safe to interrupt and safe to race: a reader
    resolving ``t3`` sees either the previous launcher or the new one and never a
    truncated file, a crash mid-write leaves the temp rather than an unexecutable
    ``t3``, and two concurrent installs end at one whole launcher rather than an
    interleaved one. The temp is a sibling so the rename stays within one
    filesystem, where it is atomic.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, staged_name = tempfile.mkstemp(prefix=".t3-launcher-", dir=path.parent)
    staged = Path(staged_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(script)
        staged.chmod(0o755)
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def install_launcher(path: Path, repo: Path) -> LauncherInstall:
    """Idempotently write the executable ``t3`` launcher for *repo* at *path*.

    Replaces the ``uv tool install`` console-script symlink and a previously
    managed launcher (:attr:`LauncherInstall.UPDATED`); a byte-identical launcher
    is left alone (:attr:`LauncherInstall.ALREADY_PRESENT`). Anything else already
    at *path* — a hand-rolled script, a foreign binary, a symlink elsewhere — is
    :attr:`LauncherInstall.REFUSED` untouched, so setup never silently clobbers
    an operator's own file. A path the process cannot write degrades to
    :attr:`LauncherInstall.UNWRITABLE` rather than raising.

    Success is READ BACK, never assumed: a published file that does not return
    the intended script for *repo*, or is not executable, is
    :attr:`LauncherInstall.UNVERIFIED` — the caller must not act on it as an
    installed launcher.
    """
    script = render_launcher_script(repo)
    if path.is_symlink() or path.exists():
        managed = read_managed_launcher(path)
        if managed == script:
            return LauncherInstall.ALREADY_PRESENT
        if managed is None and not _is_uv_tool_link(path):
            return LauncherInstall.REFUSED
        outcome = LauncherInstall.UPDATED
    else:
        outcome = LauncherInstall.INSTALLED

    try:
        _publish_launcher(path, script)
    except OSError:
        return LauncherInstall.UNWRITABLE
    if read_managed_launcher(path) != script or not os.access(path, os.X_OK):
        return LauncherInstall.UNVERIFIED
    return outcome


def is_running_in_container(
    env: Mapping[str, str] | None = None,
    dockerenv: Path = Path("/.dockerenv"),
) -> bool:
    """True when this process is the containerized runtime, not a host shell.

    Decides which SIDE of the boundary ``t3 setup`` and ``t3 doctor`` are acting
    from: a host writes and reads its own ``PATH`` launcher directly, a container
    reaches the host's through :data:`CONTAINER_HOST_BIN_DIR`. Detected via
    ``$TEATREE_ROLE`` (the deploy entrypoint sets it for every role) or the
    ``/.dockerenv`` marker Docker writes into images.
    """
    resolved = env if env is not None else os.environ
    return bool(resolved.get("TEATREE_ROLE")) or dockerenv.exists()


def remove_alias_block(rc_path: Path) -> AliasRemoval:
    """Drop the managed alias block from *rc_path*, leaving every other byte alone.

    The alias was the split-brain a ``PATH`` launcher makes unnecessary, so setup
    retires it rather than refreshing it. Only the fenced region goes: the rc
    files hold the operator's own functions, and one of them is commonly a
    symlink into a dotfiles repo, so the rewrite goes THROUGH the path rather
    than renaming over it. A missing rc, an rc without the markers, and an
    unreadable one are all left untouched.
    """
    if not rc_path.is_file():
        return AliasRemoval.ABSENT
    try:
        text = rc_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return AliasRemoval.UNWRITABLE
    if ALIAS_MARKER_BEGIN not in text or ALIAS_MARKER_END not in text:
        return AliasRemoval.ABSENT

    begin = text.index(ALIAS_MARKER_BEGIN)
    end = text.index(ALIAS_MARKER_END, begin) + len(ALIAS_MARKER_END)
    # Absorb the newline the end marker's own line carries, so removing the block
    # does not leave the blank line it used to occupy.
    remainder = text[end:].removeprefix("\n")
    try:
        rc_path.write_text(text[:begin] + remainder, encoding="utf-8")
    except OSError:
        return AliasRemoval.UNWRITABLE
    return AliasRemoval.REMOVED
