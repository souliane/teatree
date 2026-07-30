"""Shared pure logic for the containerized ``t3`` workflow (#3232).

The headless deployment runs the ``t3`` CLI and every server exclusively in
Docker, so an operator needs no host Python / uv / prereqs. On a host, ``t3
<args>`` is made transparent by a shell alias pointing at the container-wrapping
entry script ``deploy/t3``, which ``docker compose exec``s into the running
worker container. ``t3 setup`` installs the alias (a marker-delimited managed
block); ``t3 doctor`` verifies the wiring.

Both the setup installer (:mod:`teatree.cli.setup.docker_alias`) and the doctor
check (:mod:`teatree.cli.doctor.checks_docker`) consume the helpers here so the
alias line, the marker block, and the wired-state probe never drift.
"""

import os
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path

# Repo-relative locations of the compose stack and the container-wrapping entry.
COMPOSE_REL = Path("deploy") / "docker-compose.yml"
WRAPPER_REL = Path("deploy") / "t3"

# The service the wrapper execs into (kept in sync with deploy/t3's default).
DOCKER_CLI_SERVICE = "teatree-worker"

# Marker-delimited managed block, so a re-run replaces exactly its own region and
# never clobbers the operator's surrounding shell rc (idempotent dotfile edit).
ALIAS_MARKER_BEGIN = "# >>> teatree docker t3 alias >>>"
ALIAS_MARKER_END = "# <<< teatree docker t3 alias <<<"


class AliasInstall(StrEnum):
    """Outcome of an :func:`install_alias_block` call."""

    INSTALLED = "installed"
    UPDATED = "updated"
    ALREADY_PRESENT = "already-present"
    UNWRITABLE = "unwritable"


def compose_path(repo: Path) -> Path:
    """Absolute path to the deploy compose stack in *repo*."""
    return (repo / COMPOSE_REL).resolve()


def wrapper_path(repo: Path) -> Path:
    """Absolute path to the container-wrapping ``t3`` entry in *repo*."""
    return (repo / WRAPPER_REL).resolve()


def alias_line(repo: Path) -> str:
    """The single ``alias t3=…`` line pointing at *repo*'s wrapper script."""
    return f'alias t3="{wrapper_path(repo)}"'


def render_alias_block(repo: Path) -> str:
    """The full marker-delimited managed block (trailing newline included)."""
    return f"{ALIAS_MARKER_BEGIN}\n{alias_line(repo)}\n{ALIAS_MARKER_END}\n"


def is_running_in_container(
    env: Mapping[str, str] | None = None,
    dockerenv: Path = Path("/.dockerenv"),
) -> bool:
    """True when this process is the containerized runtime, not a host shell.

    Inside a container the wrapper/alias are meaningless — the container *is* the
    runtime — so both ``t3 setup``'s alias install and ``t3 doctor``'s wiring
    check no-op there. Detected via ``$TEATREE_ROLE`` (the deploy entrypoint sets
    it for every role) or the ``/.dockerenv`` marker Docker writes into images.
    """
    resolved = env if env is not None else os.environ
    return bool(resolved.get("TEATREE_ROLE")) or dockerenv.exists()


def _replace_block(text: str, block: str) -> tuple[str, bool]:
    """Replace an existing marker region in *text* with *block*.

    Returns ``(new_text, changed)`` where ``changed`` is False when the region
    was already byte-identical. Assumes both markers are present in *text*.
    """
    begin = text.index(ALIAS_MARKER_BEGIN)
    end = text.index(ALIAS_MARKER_END) + len(ALIAS_MARKER_END)
    # Absorb a single trailing newline after the end marker so the block's own
    # trailing newline does not accumulate blank lines across re-runs.
    tail = text[end:]
    tail = tail.removeprefix("\n")
    new_text = text[:begin] + block + tail
    return new_text, new_text != text


def install_alias_block(rc_path: Path, repo: Path) -> AliasInstall:
    """Idempotently install the managed alias block into *rc_path*.

    A missing rc file is created with just the block (:attr:`AliasInstall.INSTALLED`).
    An rc already carrying the markers has that region refreshed — a no-op when
    byte-identical (:attr:`AliasInstall.ALREADY_PRESENT`), otherwise rewritten to
    the current wrapper path (:attr:`AliasInstall.UPDATED`, e.g. after a clone
    relocation). An rc without the markers gets the block appended
    (:attr:`AliasInstall.INSTALLED`). A path the process cannot write degrades to
    (:attr:`AliasInstall.UNWRITABLE`) rather than raising, so this convenience
    write never aborts ``t3 setup``.
    """
    block = render_alias_block(repo)
    existing = ""
    if rc_path.is_file():
        try:
            existing = rc_path.read_text(encoding="utf-8")
        except OSError:
            return AliasInstall.UNWRITABLE

    if ALIAS_MARKER_BEGIN in existing and ALIAS_MARKER_END in existing:
        new_text, changed = _replace_block(existing, block)
        if not changed:
            return AliasInstall.ALREADY_PRESENT
        outcome = AliasInstall.UPDATED
    else:
        prefix = existing if existing.endswith("\n") or not existing else existing + "\n"
        new_text = prefix + block
        outcome = AliasInstall.INSTALLED

    try:
        rc_path.parent.mkdir(parents=True, exist_ok=True)
        rc_path.write_text(new_text, encoding="utf-8")
    except OSError:
        return AliasInstall.UNWRITABLE
    return outcome


def installed_alias_block(rc_paths: list[Path]) -> str | None:
    """Return the first rc file's managed-block text, or None if none carry it.

    Used by the doctor check to decide whether the operator has opted into the
    containerized workflow (an installed block) before verifying its health.
    """
    for path in rc_paths:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if ALIAS_MARKER_BEGIN in text and ALIAS_MARKER_END in text:
            begin = text.index(ALIAS_MARKER_BEGIN)
            end = text.index(ALIAS_MARKER_END) + len(ALIAS_MARKER_END)
            return text[begin:end]
    return None


def workflow_problems(
    repo: Path,
    installed_block: str,
    which: Callable[[str], str | None],
) -> list[str]:
    """Health problems with an opted-in containerized workflow (empty == healthy).

    Verifies the pieces the wrapper depends on: the compose stack and the entry
    script exist, the entry script is executable, the ``docker`` CLI is on PATH,
    and the installed alias still points at *repo*'s current wrapper (a stale
    path survives a clone relocation until ``t3 setup`` refreshes it).
    """
    problems: list[str] = []
    if not compose_path(repo).is_file():
        problems.append(f"compose stack missing at {compose_path(repo)}")
    wrapper = wrapper_path(repo)
    if not wrapper.is_file():
        problems.append(f"wrapper missing at {wrapper}")
    elif not os.access(wrapper, os.X_OK):
        problems.append(f"wrapper not executable at {wrapper}")
    if which("docker") is None:
        problems.append("docker CLI not found on PATH")
    if alias_line(repo) not in installed_block:
        problems.append("installed alias points at a stale path")
    return problems
