"""Box-capacity ``_check_*`` probes for `t3 doctor check` — disk + temp headroom + memory cap.

These surface RESOURCE pressure that silently wedges the box: a ROOT FILESYSTEM
filling toward full, a RAM-backed ``/tmp`` tmpfs filling toward ENOSPC or SIZED to
claim a large share of RAM, and a container memory cap set below the commit/lint-hook
floor (a too-low ``mem_limit`` OOM-kills ``ty-check``). Kept out of ``checks_environment`` (which owns
clone/install/venv hygiene) so each module stays a single concern under the
module-health LOC cap.

The root-filesystem probe is measured in PERCENT, deliberately unlike the
``resource_pressure`` scanner's absolute-GB thresholds. Both readings are valid
and answer different questions: absolute bytes say whether the next write fits
(and never misread a shared APFS container's nominal total), while percent says
whether the box is on a trajectory to full. A free-GB floor alone cannot express
the second — ``disk_crit_free_gb = 10.0`` is 95.7% of a 235 GB disk, so the first
alarm arrives long after the trajectory was obvious, which is why a host at 96%
then 97% full never fired anything (#3852).
"""

import os
from contextlib import suppress
from pathlib import Path
from typing import cast

import typer

from teatree.cli.doctor.checks_plugins import JsonObject, _check_pyright_lsp_plugin, _read_json_object
from teatree.core.retention.liveness import held_paths
from teatree.utils import disk_consumers, disk_probe
from teatree.utils.ram_probe import cgroup_file, host_total_ram_mib
from teatree.utils.ram_scope import AGENT_WORKLOAD_FLOOR_ENV, RamHeadroom, agent_workload_floor_gib

__all__ = ["_check_pyright_lsp_plugin"]

_DEFAULT_TMPFS_WARN_PERCENT = 80
# A quarter of RAM is generous for scratch on a box whose whole purpose is agent
# work; the measured default let /tmp claim 48%.
_DEFAULT_TMPFS_MAX_RAM_PERCENT = 25
_PERCENT_MAX = 100
_MIN_MOUNT_FIELDS = 3

_ROOT_MOUNT = "/"
_DEFAULT_DISK_WARN_PERCENT = 85
_DEFAULT_DISK_CRIT_PERCENT = 95

# Only the WORKER container runs headless agents + their commit/ty-check/lint hooks,
# so it is the only role whose under-sized memory cap or missing skills is a real,
# product-broken fault. The lean admin/slack-listener (web UI / socket receiver) being
# small — and not needing the loop's skills — is correct and must NOT be flagged.
_AGENT_ROLE = "worker"
# The role env var is set by whichever compose file brought the container up, and the
# name is NOT uniform across deployments: core's own stack sets TEATREE_ROLE, while a
# downstream stack commonly prefixes its own. Reading only TEATREE_ROLE made every
# role-aware check below silently INERT on such a deployment — the memory-cap check
# returned OK for fourteen hours on a worker whose cap it was written to refuse
# (#4201). So resolve GENERICALLY: the canonical name first, then any `*_ROLE` var.
_ROLE_ENV = "TEATREE_ROLE"
_ROLE_ENV_SUFFIX = "_ROLE"
# An alias is only honoured when its VALUE names a role we know. Without this an
# unrelated `*_ROLE` var (cloud IAM ones are common) would be read as a role and could
# make a non-worker host emit a worker FAIL.
_KNOWN_ROLES = frozenset({"worker", "admin", "init", "slack-listener", "watchdog"})
_CLAUDE_PLUGIN_ID = "t3@souliane"

_BYTES_PER_GIB = 1024**3
_BYTES_PER_MIB = 1024**2
# cgroup v1's "unlimited" is a near-2**63 page-aligned sentinel, and cgroup v2 uses
# the literal "max"; any cap at/above this floor is treated as no real cap.
_CGROUP_UNLIMITED_MIN = 1 << 60


def _tmpfs_warn_percent(raw: str | None, *, default: int = _DEFAULT_TMPFS_WARN_PERCENT) -> int:
    """Parse a tmpfs percent override into a 1..100 threshold; *default* on garbage."""
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if 1 <= value <= _PERCENT_MAX else default


def _disk_percent_threshold(raw: str | None, *, default: int) -> int:
    """Parse a percent-threshold env override into 1..100; fall back to *default* on garbage."""
    return disk_probe.disk_percent_threshold(raw, default=default)


def _used_percent(path: str) -> float | None:
    """Percent of *path*'s filesystem in use, or ``None`` when it cannot be measured."""
    return disk_probe.read_disk_used_percent(path)


def _check_root_disk_headroom(*, mount_point: str = _ROOT_MOUNT) -> bool:
    """FAIL when the root filesystem is critically full, WARN when it is merely filling.

    The probe that was missing entirely while the box climbed past 96%: the sibling
    checks cover the ``/tmp`` tmpfs and the cgroup memory cap, and neither looks at
    the disk everything else is written to. A full root filesystem is a broken
    product — builds, docker, the control DB and every agent worktree stop — so the
    CRITICAL band HARD-FAILs (gating the doctor exit code and the watchdog owner
    DM) rather than joining the advisory WARNs.

    Bands are PERCENT (see the module docstring for why, next to the absolute-bytes
    scanner): WARN at ``TEATREE_DISK_WARN_PERCENT`` (default 85), FAIL at
    ``TEATREE_DISK_CRIT_PERCENT`` (default 95). Crash-proof — any probe error
    degrades to OK so this diagnostic never aborts the doctor run.
    """
    try:
        used_pct = _used_percent(mount_point)
    except OSError:
        return True
    if used_pct is None:
        return True
    crit = disk_probe.disk_crit_percent()
    warn = disk_probe.disk_warn_percent()
    if used_pct >= crit:
        consumers = disk_consumers.summary()
        typer.echo(
            f"FAIL  {mount_point} is {used_pct}% used (>= {crit}% critical) — a full root filesystem stops "
            f"builds, docker, the control DB and every agent worktree. Top consumers: {consumers}. Reclaim now: "
            "`t3 <overlay> workspace reclaim-disk`, then `t3 <overlay> workspace clean-all` and "
            "`t3 <overlay> retention prune --apply`. Tune with TEATREE_DISK_CRIT_PERCENT."
        )
        return False
    if used_pct >= warn:
        consumers = disk_consumers.summary()
        typer.echo(
            f"WARN  {mount_point} is {used_pct}% used (>= {warn}% threshold) — reclaim before it bites: "
            f"top consumers: {consumers}. "
            "`t3 <overlay> workspace reclaim-disk`, `t3 <overlay> workspace clean-all`, "
            "`t3 <overlay> retention prune --apply`. Tune with TEATREE_DISK_WARN_PERCENT."
        )
    return True


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
    tmp_dir: str | None = None,
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

    Probes the SAME target as its sizing sibling (:func:`_tmpfs_probe_target`) —
    a hard-coded ``/tmp`` is silently inert inside the container this check runs
    in, where the host's tmpfs is reachable only through the ``/host-tmp`` bind.
    """
    try:
        target = _tmpfs_probe_target(tmp_dir)
        if not mounts_path.is_file():
            return True
        if _tmp_mount_fstype(mounts_path.read_text(encoding="utf-8"), target) != "tmpfs":
            return True
        threshold = _tmpfs_warn_percent(os.environ.get("TEATREE_TMPFS_WARN_PERCENT"))
        stats = os.statvfs(target)
        total = stats.f_blocks * stats.f_frsize
        if total <= 0:
            return True
        used_pct = round((total - stats.f_bavail * stats.f_frsize) / total * 100)
        if used_pct >= threshold:
            typer.echo(
                f"WARN  {target} is a RAM-backed tmpfs at {used_pct}% used (>= {threshold}% threshold) — "
                f"agent/pytest/uv scratch can fill it to ENOSPC and wedge the box. Trim it: "
                f"`find {target} -maxdepth 1 -name 'pytest-*' -mmin +120 -exec rm -rf {{}} +`. Runtime "
                "temp is routed to disk via TMPDIR; tune this with TEATREE_TMPFS_WARN_PERCENT."
            )
    except OSError:
        return True
    return True


_HOST_TMP_MOUNT = Path("/host-tmp")


def _tmpfs_probe_target(tmp_dir: str | None) -> str:
    """Which path BOTH tmpfs checks inspect: the caller's choice, else the host bind, else ``/tmp``.

    ``t3 doctor check`` runs INSIDE the container (#4165), where the container's
    own ``/tmp`` is the image's overlay layer, not the host's tmpfs — so a check
    hard-coded to ``/tmp`` is silently inert there. ``/host-tmp`` is a BIND mount
    of the host's real temp root (the scratch sweep's own mount, wired in this
    same ticket), and a bind mount reports its SOURCE's fstype and size in the
    mount table — so probing it from inside the container answers the question
    about the HOST's tmpfs, which is what this check exists to surface.
    """
    if tmp_dir is not None:
        return tmp_dir
    return str(_HOST_TMP_MOUNT) if _HOST_TMP_MOUNT.is_dir() else "/tmp"  # noqa: S108 — auditing, not creating


def _check_tmp_tmpfs_sizing(
    *,
    mounts_path: Path = Path("/proc/mounts"),
    tmp_dir: str | None = None,
    total_ram_mib: int | None = None,
) -> bool:
    """WARN when a RAM-backed ``/tmp`` is SIZED to claim a large share of machine RAM.

    The sibling headroom check above measures how FULL the tmpfs is; this one
    measures how big it is allowed to get, which is the standing defect. The
    measured box shipped a 15.1 GB ``/tmp`` on 31 GB of RAM — 48% of the machine
    claimable by scratch alone, and 8.8 GB of week-old agent scratch was sitting
    in it. Teatree does NOT move ``/tmp`` to disk to fix that: the root
    filesystem was 84% full so a 15 GB disk-backed ``/tmp`` trades a degrading
    failure for a filesystem-full one that breaks the control DB, and tmpfs is
    wiped on reboot, which is what bounded the observed leak to a week's worth.
    Capping the mount keeps both properties. Pairs with the
    ``scratch_retention_days`` sweep, which keeps it from filling even at the cap.

    Threshold overridable via ``TEATREE_TMPFS_MAX_RAM_PERCENT`` (1..100, default
    25). Surfacing-only, like its sibling. Crash-proof — any probe error degrades
    to a silent pass so this diagnostic never aborts the doctor run.
    """
    try:
        target = _tmpfs_probe_target(tmp_dir)
        if not mounts_path.is_file():
            return True
        if _tmp_mount_fstype(mounts_path.read_text(encoding="utf-8"), target) != "tmpfs":
            return True
        ram_mib = host_total_ram_mib() if total_ram_mib is None else total_ram_mib
        stats = os.statvfs(target)
        size_mib = stats.f_blocks * stats.f_frsize // (1024 * 1024)
        if ram_mib <= 0 or size_mib <= 0:
            return True
        threshold = _tmpfs_warn_percent(
            os.environ.get("TEATREE_TMPFS_MAX_RAM_PERCENT"),
            default=_DEFAULT_TMPFS_MAX_RAM_PERCENT,
        )
        share = round(size_mib / ram_mib * 100)
        if share >= threshold:
            typer.echo(
                f"WARN  {target} is a tmpfs sized {size_mib / 1024:.1f} GB on {ram_mib / 1024:.1f} GB of RAM "
                f"({share}% >= {threshold}%) — scratch written there is memory, not disk, so it can claim "
                f"that share of the working pool permanently. Cap it (host, not the container): "
                f"`systemctl edit tmp.mount` with `[Mount]` / `Options=mode=1777,strictatime,nosuid,nodev,size=4G`, "
                f"then reboot. Keep it a tmpfs — a disk-backed /tmp trades RAM pressure for a full root "
                f"filesystem and gives up the reboot wipe. Sweep it with `t3 <overlay> retention scratch --apply`; "
                f"tune this with TEATREE_TMPFS_MAX_RAM_PERCENT."
            )
    except OSError:
        return True
    return True


def _check_scratch_sweep_probe(*, retention_days: int | None = None, proc_root: Path | None = None) -> bool:
    """WARN when the scratch sweep is ARMED but its open-file probe cannot see the process table.

    ``scratch_retention_days > 0`` with an unsighted probe is the worst of both
    states: the lane looks configured, refuses on every tick, and reclaims
    nothing. Silent otherwise — a disabled lane has nothing to report. Surfacing-
    only, like its tmpfs siblings, and crash-proof: any probe error degrades to a
    silent pass rather than aborting the doctor run.
    """
    try:
        if retention_days is None or proc_root is None:
            from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: DB read at call time
            from teatree.core.retention.scratch import resolve_scratch_sweep  # noqa: PLC0415 — deferred: ORM import

            settings = get_effective_settings()
            retention_days = settings.scratch_retention_days if retention_days is None else retention_days
            if proc_root is None:
                proc_root = resolve_scratch_sweep(settings.scratch_sweep_root).proc_root
        if retention_days <= 0:
            return True
        view = held_paths(proc_root)
        if not view.sighted:
            typer.echo(
                f"WARN  scratch sweep is armed (scratch_retention_days={retention_days}) but its open-file "
                f"probe is unsighted at {proc_root}: {view.unknowable_reason} — every sweep REFUSES and "
                "reclaims nothing. Either arm the probe (a venue that can resolve that table's fds — the "
                "container reading the host process table with ptrace access, or a host-side run) or set "
                "`t3 <overlay> config_setting set scratch_retention_days 0`."
            )
    except Exception:  # noqa: BLE001 — a doctor check must never crash the run
        return True
    return True


def _worker_floor_bytes(raw: str | None) -> int:
    """Parse ``TEATREE_WORKER_MEMORY_FLOOR_GIB`` (a positive int) into a byte floor; default on garbage.

    The GiB figure is :func:`~teatree.utils.ram_scope.agent_workload_floor_gib`, shared with
    the scope test that decides whether a cgroup's memory reading describes the box at all
    — this FAIL and that test must name the same floor (#4217).
    """
    return agent_workload_floor_gib(raw) * _BYTES_PER_GIB


def _read_cgroup_memory_cap(v2: Path | None, v1: Path | None) -> int | None:
    """Return the container's cgroup memory cap in bytes, or ``None`` when uncapped/unknown.

    Reads cgroup v2 ``memory.max`` first (``"max"`` = no cap → ``None``), then falls
    back to cgroup v1 ``memory.limit_in_bytes``. A near-2**63 "unlimited" sentinel or
    a non-numeric/absent value is treated as no cap (``None``), so only a REAL cap is
    ever reported.
    """
    for path in (v2, v1):
        if path is None:
            continue
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


def _worker_cgroup_paths(v2: Path | None, v1: Path | None) -> tuple[Path | None, Path | None]:
    """Resolve this worker's own v2/v1 membership, never the mount root by guess."""
    if v2 is None:
        with suppress(OSError):
            v2 = cgroup_file("memory.max")
    if v1 is None:
        with suppress(OSError):
            v1 = cgroup_file("memory.limit_in_bytes", version=1, controller="memory")
    return v2, v1


def _check_worker_memory_cap(
    *,
    role: str | None = None,
    v2: Path | None = None,
    v1: Path | None = None,
) -> bool:
    """FAIL when the WORKER container's cgroup memory cap is below the agent-workload floor.

    CRITICAL, not advisory: only the ``worker`` role runs headless agents plus their
    commit / ``ty-check`` / lint hooks, and a too-low ``mem_limit`` there OOM-kills them
    (exit 137) even on an idle host — a broken product, so this HARD-FAILs (gates the
    doctor exit code + the watchdog owner DM) rather than a soft WARN. ROLE-AWARE: the
    lean admin (Django web UI) and slack-listener are meant to be small, so this returns
    OK for them; it fires only when doctor runs inside the worker and that container's
    own cgroup cap is under the floor. Role comes from :func:`_resolve_agent_role`.
    An unknown or uncapped worker cannot prove the floor and also FAILs; its
    stable doctor finding reaches the owner through the watchdog. Floor is
    overridable via ``TEATREE_WORKER_MEMORY_FLOOR_GIB`` (positive GiB int, default 4).
    """
    try:
        if _resolve_agent_role(role) != _AGENT_ROLE:
            return True
        cap = _read_cgroup_memory_cap(*_worker_cgroup_paths(v2, v1))
        if cap is None:
            typer.echo(
                "FAIL  worker cgroup memory cap is absent, uncapped or unreadable — cannot verify "
                "the agent-workload floor. Inspect /proc/self/cgroup and the worker mem_limit; "
                "this worker may be reading host scope or running without a usable cap."
            )
            return False
        floor = _worker_floor_bytes(os.environ.get(AGENT_WORKLOAD_FLOOR_ENV))
        if cap < floor:
            typer.echo(
                f"FAIL  worker container memory cap is {cap / _BYTES_PER_GIB:.2g} GiB "
                f"(< {floor / _BYTES_PER_GIB:.2g} GiB floor) — the worker runs headless agents plus "
                "their commit/ty-check/lint hooks, which OOM-kill (exit 137) under a low cap even on an "
                "idle host. Raise TEATREE_WORKER_MEM_LIMIT (or the worker `mem_limit` in "
                "deploy/docker-compose.yml) and redeploy; tune the floor with TEATREE_WORKER_MEMORY_FLOOR_GIB."
            )
            return False
    except AmbiguousAgentRoleError as exc:
        typer.echo(f"FAIL  {exc}")
        return False
    except OSError:
        return True
    return True


class AmbiguousAgentRoleError(RuntimeError):
    """Two or more ``*_ROLE`` aliases name DIFFERENT known roles (#4201).

    Carries the operator-facing sentence naming every conflicting variable and the
    one-line fix, so each gate can report it verbatim instead of re-composing it.
    """


def _role_aliases() -> dict[str, str]:
    """Every ``*_ROLE`` env var (excluding :data:`_ROLE_ENV`) whose value is a known role."""
    return {
        name: os.environ[name].strip()
        for name in sorted(os.environ)
        if name != _ROLE_ENV and name.endswith(_ROLE_ENV_SUFFIX) and os.environ[name].strip() in _KNOWN_ROLES
    }


def _resolve_agent_role(role: str | None) -> str:
    """The container's role, from an explicit *role*, ``TEATREE_ROLE``, or a ``*_ROLE`` alias.

    Deployment-agnostic on purpose: core cannot know what a downstream stack names its
    role variable, and guessing wrong makes every role-aware check silently inert
    rather than loud. Aliases are only accepted when the value is one of
    :data:`_KNOWN_ROLES`.

    Returns ``""`` when nothing names a role. A blank role is NOT the worker, so a
    check keyed on it stays OK — the conservative direction for a host or a shell
    where no compose file has spoken.

    Raises :class:`AmbiguousAgentRoleError` when two aliases name DIFFERENT roles.
    Picking one of them (this used to take the first in sorted order) turns an
    unreadable environment into a confident wrong answer: ``IAM_ROLE=admin`` sorts
    before ``STACK_ROLE=worker``, so a worker resolved as "admin" and every worker
    hard-gate went inert — the exact silent-inertness this resolver exists to end.
    "I cannot tell" must not return the same value as "I read it, and it is not the
    worker". The canonical :data:`_ROLE_ENV` and an explicit *role* both settle a
    conflict, which is also the fix the message names.
    """
    if role is not None:
        return role
    direct = os.environ.get(_ROLE_ENV, "").strip()
    if direct:
        return direct
    aliases = _role_aliases()
    distinct = set(aliases.values())
    if len(distinct) > 1:
        named = ", ".join(f"{name}={value}" for name, value in aliases.items())
        message = (
            f"conflicting container role: {named} name different roles, so no role-aware check can "
            f"tell what this container is. Set {_ROLE_ENV} to the authoritative role (it wins over "
            "every alias) — until then this gate refuses rather than guessing and going inert."
        )
        raise AmbiguousAgentRoleError(message)
    return next(iter(distinct), "")


def _check_resume_ceiling_reachable(
    *,
    role: str | None = None,
    v2: Path | None = None,
    v1: Path | None = None,
) -> bool:
    """FAIL when this worker's memory cap sits at/below the governor's RESUME floor.

    A braked admission governor only re-admits above ``RAM_RESUME_FLOOR_GB``. Capped
    at or below that floor the condition is unsatisfiable with the container EMPTY, so
    the first brake is permanent and every headless task queues forever. Unlike
    ``_check_worker_memory_cap`` (which asks "is there room to work?") this asks "can
    the brake ever release?" — a different, unrecoverable failure that no amount of
    waiting or idling fixes, which is why it is a HARD FAIL rather than a WARN.

    Crash-proof: any probe error degrades to OK so it never aborts the doctor run.
    """
    from teatree.core.admission_governor import resume_ceiling_conflict  # noqa: PLC0415 — deferred: call-time import

    try:
        if _resolve_agent_role(role) != _AGENT_ROLE:
            return True
        cap = _read_cgroup_memory_cap(*_worker_cgroup_paths(v2, v1))
        if cap is None:
            return True
        # SCOPE-QUALIFIED, exactly as the governor's own reader does it: a cap too small to
        # be box-scoped is not this check's to judge — it bounds a reading the watermarks
        # never see, and "is there room to work at all?" is `_check_worker_memory_cap`'s
        # question. Handing over the RAW cap made this fire on caps the governor drops.
        scoped = RamHeadroom(
            available_mib=None,
            cgroup_limit_mib=round(cap / _BYTES_PER_MIB),
            host_available_mib=None,
        )
        conflict = resume_ceiling_conflict(scoped.box_watermark_cap_gb)
        if conflict is not None:
            typer.echo(f"FAIL  worker {conflict}")
            return False
    except AmbiguousAgentRoleError as exc:
        typer.echo(f"FAIL  {exc}")
        return False
    except OSError:
        return True
    return True


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

    Role comes from :func:`_resolve_agent_role`, the same resolver the two memory gates
    use — reading ``TEATREE_ROLE`` alone left this gate inert on any deployment that
    names its role variable differently, which is the same inertness the resolver was
    introduced to end. An ambiguous role FAILs loudly rather than returning OK.
    """
    try:
        resolved_role = _resolve_agent_role(role)
    except AmbiguousAgentRoleError as exc:
        typer.echo(f"FAIL  {exc}")
        return False
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
