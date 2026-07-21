"""Box-capacity ``_check_*`` probes for `t3 doctor check` — temp headroom + memory cap.

These surface RESOURCE pressure that silently wedges the box: a RAM-backed ``/tmp``
tmpfs filling toward ENOSPC, and a container memory cap set below the commit/lint-hook
floor (a too-low ``mem_limit`` OOM-kills ``ty-check``). Both are surfacing-only WARNs —
they always return ``True`` and never gate the doctor exit code, matching the sibling
advisory checks. Kept out of ``checks_environment`` (which owns clone/install/venv
hygiene) so each module stays a single concern under the module-health LOC cap.
"""

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import cast

import typer

# A parsed JSON object (``~/.claude`` settings / installed_plugins). Values are
# arbitrary JSON, so the leaves stay ``object``; the alias names the shape and keeps
# the module-health dataclass/TypedDict rule satisfied (mirrors cli/setup/claude_settings).
type JsonObject = dict[str, object]

_DEFAULT_TMPFS_WARN_PERCENT = 80
_PERCENT_MAX = 100
_MIN_MOUNT_FIELDS = 3

# Only the WORKER container runs headless agents + their commit/ty-check/lint hooks,
# so it is the only role whose under-sized memory cap or missing skills is a real,
# product-broken fault. The lean admin/slack-listener (web UI / socket receiver) being
# small — and not needing the loop's skills — is correct and must NOT be flagged.
_AGENT_ROLE = "worker"
_CLAUDE_PLUGIN_ID = "t3@souliane"

# The external pyright-lsp plugin + the language-server binary it drives. `t3 setup`
# registers + enables the plugin AND provisions `pyright-langserver` (npm `pyright`),
# which the plugin execs to deliver live type diagnostics. Enabled-but-unprovisioned
# is a HARD FAIL (#3568): the plugin then silently never starts, so a green doctor
# would misreport a dead LSP as healthy. A merely-disabled plugin is a config choice
# and stays an advisory WARN.
_PYRIGHT_PLUGIN_ID = "pyright-lsp@claude-plugins-official"
_PYRIGHT_LANGSERVER = "pyright-langserver"
_PYRIGHT_INSTALL_CMD = "npm install -g --prefix ~/.local pyright"
_DEFAULT_WORKER_FLOOR_GIB = 4
_BYTES_PER_GIB = 1024**3
# cgroup v1's "unlimited" is a near-2**63 page-aligned sentinel, and cgroup v2 uses
# the literal "max"; any cap at/above this floor is treated as no real cap.
_CGROUP_UNLIMITED_MIN = 1 << 60
_CGROUP_MEMORY_MAX_V2 = Path("/sys/fs/cgroup/memory.max")
_CGROUP_MEMORY_MAX_V1 = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")


def _tmpfs_warn_percent(raw: str | None) -> int:
    """Parse ``TEATREE_TMPFS_WARN_PERCENT`` into a 1..100 threshold; default on garbage."""
    if raw is None:
        return _DEFAULT_TMPFS_WARN_PERCENT
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_TMPFS_WARN_PERCENT
    return value if 1 <= value <= _PERCENT_MAX else _DEFAULT_TMPFS_WARN_PERCENT


def _tmp_mount_fstype(mounts_text: str, mount_point: str) -> str | None:
    """Return the fstype backing *mount_point* per ``/proc/mounts`` text, or ``None``.

    Reads the standard ``/proc/mounts`` columns (device, mount point, fstype, ...)
    and returns the LAST matching entry's fstype — a later mount over the same point
    shadows an earlier one. ``None`` when *mount_point* is not mounted.
    """
    fstype: str | None = None
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) >= _MIN_MOUNT_FIELDS and fields[1] == mount_point:
            fstype = fields[2]
    return fstype


def _check_tmp_tmpfs_headroom(
    *,
    mounts_path: Path = Path("/proc/mounts"),
    tmp_dir: str = "/tmp",  # noqa: S108 — auditing the /tmp mount, not creating a temp file
) -> bool:
    """WARN when a RAM-backed (tmpfs) ``/tmp`` is filling toward ENOSPC.

    The box's ``/tmp`` is a small RAM tmpfs; agent ``claude`` sessions, pytest, and
    uv scratch can fill it to 100% and wedge everything with ENOSPC. Runtime temp is
    now routed to DISK (``deploy/entrypoint.sh`` + the managed settings-template
    ``TMPDIR``), but this surfaces residual tmpfs pressure directly so a fill is SEEN
    before it wedges the box. Only meaningful when ``/tmp`` is actually tmpfs — a
    disk-backed ``/tmp`` (e.g. the container overlay) is silently skipped, as is a
    box with no ``/proc/mounts`` (non-Linux). Surfacing-only: a WARN that keeps the
    run GREEN (never extracted into the watchdog FAIL DM), matching the sibling
    advisory checks. Threshold overridable via ``TEATREE_TMPFS_WARN_PERCENT`` (1..100,
    default 80). Crash-proof — any probe error degrades to a silent pass so this
    diagnostic never aborts the doctor run.
    """
    try:
        if not mounts_path.is_file():
            return True
        if _tmp_mount_fstype(mounts_path.read_text(encoding="utf-8"), tmp_dir) != "tmpfs":
            return True
        threshold = _tmpfs_warn_percent(os.environ.get("TEATREE_TMPFS_WARN_PERCENT"))
        stats = os.statvfs(tmp_dir)
        total = stats.f_blocks * stats.f_frsize
        if total <= 0:
            return True
        used_pct = round((total - stats.f_bavail * stats.f_frsize) / total * 100)
        if used_pct >= threshold:
            typer.echo(
                f"WARN  {tmp_dir} is a RAM-backed tmpfs at {used_pct}% used (>= {threshold}% threshold) — "
                f"agent/pytest/uv scratch can fill it to ENOSPC and wedge the box. Trim it: "
                f"`find {tmp_dir} -maxdepth 1 -name 'pytest-*' -mmin +120 -exec rm -rf {{}} +`. Runtime "
                "temp is routed to disk via TMPDIR; tune this with TEATREE_TMPFS_WARN_PERCENT."
            )
    except OSError:
        return True
    return True


def _worker_floor_bytes(raw: str | None) -> int:
    """Parse ``TEATREE_WORKER_MEMORY_FLOOR_GIB`` (a positive int) into a byte floor; default on garbage."""
    gib = _DEFAULT_WORKER_FLOOR_GIB
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            value = _DEFAULT_WORKER_FLOOR_GIB
        gib = value if value > 0 else _DEFAULT_WORKER_FLOOR_GIB
    return gib * _BYTES_PER_GIB


def _read_cgroup_memory_cap(v2: Path, v1: Path) -> int | None:
    """Return the container's cgroup memory cap in bytes, or ``None`` when uncapped/unknown.

    Reads cgroup v2 ``memory.max`` first (``"max"`` = no cap → ``None``), then falls
    back to cgroup v1 ``memory.limit_in_bytes``. A near-2**63 "unlimited" sentinel or
    a non-numeric/absent value is treated as no cap (``None``), so only a REAL cap is
    ever reported.
    """
    for path in (v2, v1):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text or text == "max":
            return None
        try:
            value = int(text)
        except ValueError:
            continue
        if value <= 0 or value >= _CGROUP_UNLIMITED_MIN:
            return None
        return value
    return None


def _check_worker_memory_cap(
    *,
    role: str | None = None,
    v2: Path = _CGROUP_MEMORY_MAX_V2,
    v1: Path = _CGROUP_MEMORY_MAX_V1,
) -> bool:
    """FAIL when the WORKER container's cgroup memory cap is below the agent-workload floor.

    CRITICAL, not advisory: only the ``worker`` role runs headless agents plus their
    commit / ``ty-check`` / lint hooks, and a too-low ``mem_limit`` there OOM-kills them
    (exit 137) even on an idle host — a broken product, so this HARD-FAILs (gates the
    doctor exit code + the watchdog owner DM) rather than a soft WARN. ROLE-AWARE: the
    lean admin (Django web UI) and slack-listener are meant to be small, so this returns
    OK for them; it fires only when doctor runs inside the worker and that container's
    own cgroup cap is under the floor. Role is read from ``TEATREE_ROLE`` (the
    compose-set per-service env). No cap (a host / uncapped / cgroup files absent) is
    OK. Floor overridable via ``TEATREE_WORKER_MEMORY_FLOOR_GIB`` (positive GiB int,
    default 4). Crash-proof — any probe error degrades to OK so it never aborts the run.
    """
    try:
        resolved_role = os.environ.get("TEATREE_ROLE", "") if role is None else role
        if resolved_role != _AGENT_ROLE:
            return True
        cap = _read_cgroup_memory_cap(v2, v1)
        if cap is None:
            return True
        floor = _worker_floor_bytes(os.environ.get("TEATREE_WORKER_MEMORY_FLOOR_GIB"))
        if cap < floor:
            typer.echo(
                f"FAIL  worker container memory cap is {cap / _BYTES_PER_GIB:.2g} GiB "
                f"(< {floor / _BYTES_PER_GIB:.2g} GiB floor) — the worker runs headless agents plus "
                "their commit/ty-check/lint hooks, which OOM-kill (exit 137) under a low cap even on an "
                "idle host. Raise TEATREE_WORKER_MEM_LIMIT (or the worker `mem_limit` in "
                "deploy/docker-compose.yml) and redeploy; tune the floor with TEATREE_WORKER_MEMORY_FLOOR_GIB."
            )
            return False
    except OSError:
        return True
    return True


def _read_json_object(path: Path) -> JsonObject:
    """Load ``path`` as a JSON object, or ``{}`` when absent/unreadable/not an object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _worker_skills_registered(home: Path) -> bool:
    """True when the ``t3@souliane`` plugin is installed (resolvable path) AND enabled.

    Reads ``~/.claude/plugins/installed_plugins.json`` (a plugin entry with a
    resolvable ``installPath``) and ``~/.claude/settings.json`` (``enabledPlugins``
    flag). Mirrors the entrypoint's ``verify_agent_skills`` so the doctor gate and the
    worker's hard startup precondition agree on "skills available".
    """
    enabled = _read_json_object(home / ".claude" / "settings.json").get("enabledPlugins")
    if not (isinstance(enabled, dict) and cast("JsonObject", enabled).get(_CLAUDE_PLUGIN_ID) is True):
        return False
    plugins = _read_json_object(home / ".claude" / "plugins" / "installed_plugins.json").get("plugins")
    entries = cast("JsonObject", plugins).get(_CLAUDE_PLUGIN_ID) if isinstance(plugins, dict) else None
    if not (isinstance(entries, list) and entries):
        return False
    first = entries[0]
    install_path = cast("JsonObject", first).get("installPath") if isinstance(first, dict) else None
    return isinstance(install_path, str) and bool(install_path) and Path(install_path).is_dir()


def _check_worker_skills_present(*, role: str | None = None, home: Path | None = None) -> bool:
    """FAIL (worker role only) when the t3 skills plugin is not registered/enabled.

    CRITICAL, not advisory: a worker whose agents load ZERO skills is a broken product
    (the exact silent outage this PR refuses), so this HARD-FAILs — gating the doctor
    exit code and the watchdog owner DM — rather than a soft WARN. ROLE-AWARE: only the
    worker (the agent-running container) is gated; admin/slack-listener/watchdog and a
    roleless host invocation return OK. Mirrors the entrypoint's ``verify_agent_skills``
    startup precondition, so the running-loop gate and the boot gate stay in lockstep.
    """
    resolved_role = os.environ.get("TEATREE_ROLE", "") if role is None else role
    if resolved_role != _AGENT_ROLE:
        return True
    if _worker_skills_registered(Path.home() if home is None else home):
        return True
    typer.echo(
        f"FAIL  worker: the t3 skills plugin ({_CLAUDE_PLUGIN_ID}) is NOT registered/enabled in "
        "~/.claude — the loop's agents would run SKILL-LESS. Re-run `t3 setup` in the worker "
        "container (or redeploy); the worker entrypoint now refuses to start without it."
    )
    return False


def _check_pyright_lsp_plugin(*, home: Path | None = None, which: Callable[[str], str | None] | None = None) -> bool:
    """FAIL when the pyright-lsp plugin is enabled but its langserver is not provisioned (#3568).

    pyright-lsp gives factory agents LIVE pyright type diagnostics while coding. The
    enabled plugin execs ``pyright-langserver`` (npm ``pyright``); when that binary is
    missing the LSP silently never starts, so the enabled-but-unprovisioned state is a
    HARD FAIL (returns ``False``) under the epic #3445 "enabled but not provisioned →
    FAIL" principle — otherwise a green doctor misreports a dead LSP as healthy. The
    plugin merely being disabled is a config choice, so that stays an advisory WARN
    (returns ``True``). ``which`` is injectable for tests (defaults to
    :func:`shutil.which`). Crash-proof — any read error degrades to a silent pass so
    this diagnostic never aborts the doctor run.
    """
    resolve = shutil.which if which is None else which
    try:
        enabled = _read_json_object((home or Path.home()) / ".claude" / "settings.json").get("enabledPlugins")
    except OSError:
        return True
    if not (isinstance(enabled, dict) and cast("JsonObject", enabled).get(_PYRIGHT_PLUGIN_ID) is True):
        typer.echo(
            "WARN  pyright-lsp plugin is not enabled — factory agents get no LIVE pyright type "
            "diagnostics while coding (type errors surface only at CI). Run `t3 setup` to register "
            "+ enable it (advisory only; nothing is gated)."
        )
        return True
    if resolve(_PYRIGHT_LANGSERVER) is None:
        typer.echo(
            f"FAIL  pyright-lsp plugin is enabled but `{_PYRIGHT_LANGSERVER}` is not on PATH — the "
            "language server cannot start, so the enabled LSP silently delivers no live type "
            f"diagnostics. Install it: `{_PYRIGHT_INSTALL_CMD}` (or re-run `t3 setup`)."
        )
        return False
    return True
