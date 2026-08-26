"""``t3 tool reap-orphan-groups`` — reclaim the capacity a leaderless group is holding (#4580).

The command the doctor's WARN names. It is DRY-RUN by default, because the plan is the part
an operator reads and the signal is the part they authorise.

Only a group numbered in THIS namespace is ever signalled. A pid read from ``/host-proc``
belongs to the host's namespace, where the same integer names a different process here, so
those are refused with the host command printed instead — sending a signal by that number
from inside a container reaches whatever local process happens to hold it.

SIGTERM only, with no escalation to SIGKILL: all 37 processes of the recorded incident
exited on TERM. A survivor is reported rather than force-killed, because a group that
ignores TERM is doing something the operator should look at.
"""

import os
import signal
import time

import typer

from teatree.core.cleanup.orphan_process_groups import (
    GroupMember,
    OrphanGroup,
    min_age_seconds_setting,
    survey_orphan_groups,
    venue_ancestry_pgids,
)

#: Never reaped, as ``(program, required-argument-substring)``. Matched against the
#: PROGRAM WORD — never the whole command line, which carries the directory the process
#: happened to run from: on a box whose checkouts all sit under a path containing the
#: product name, a substring test over the command line protects every process on the
#: machine and the reaper silently becomes a no-op. An empty needle means the program
#: alone is enough. A fixed list on purpose — an operator-emptiable safety list is a
#: footgun with no upside.
_PROTECTED: tuple[tuple[str, str], ...] = (
    ("t3", ""),
    ("claude", ""),
    ("node", ""),
    ("docker-init", ""),
    ("python", "teatree"),
    ("python3", "teatree"),
    ("uv", "teatree"),
)

#: How long TERM is given before survivors are counted.
_TERM_GRACE_SECONDS = 1.0
_INIT_PGID = 1


def refusal_for(group: OrphanGroup, *, protected_pgids: set[int]) -> str:
    """Why this group may not be signalled from here, or ``""`` when it may."""
    if not group.signalable:
        return f"pids are numbered in another namespace — run: {group.remedy()}"
    if group.pgid <= _INIT_PGID:
        return f"pgid {group.pgid} is the init group"
    if group.pgid in protected_pgids:
        return "this process's own group or an ancestor's — reaping it would kill the reaper"
    for member in group.members:
        match = _protected_match(member)
        if match:
            return f"member pid {member.pid} ({member.program}) matches the never-reap rule {match!r}"
    return ""


def _protected_match(member: GroupMember) -> str:
    """The never-reap rule this member trips, or ``""``."""
    program = member.program
    arguments = " ".join(member.argv[1:])
    for name, needle in _PROTECTED:
        if program.startswith(name) and (not needle or needle in arguments):
            return f"{name} {needle}".strip()
    return ""


def _term(pgid: int) -> str:
    """SIGTERM the group; an already-dead group is a no-op, not an error."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "already gone"
    except OSError as exc:
        return f"could not signal: {exc}"
    return ""


def _survivors(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    return True


def reap_orphan_groups(
    pgid: list[int] | None = typer.Option(None, "--pgid", help="Only this group (repeatable)."),
    *,
    apply_now: bool = typer.Option(False, "--apply", help="Actually SIGTERM; default is a dry run."),
) -> None:
    """Report — and with ``--apply`` reclaim — process groups whose leader is gone."""
    requested = set(pgid or ())
    survey = survey_orphan_groups(min_age_seconds=min_age_seconds_setting())
    for gap in survey.gaps:
        typer.echo(f"BLIND  {gap}")
    groups = [group for group in survey.groups if not requested or group.pgid in requested]
    if not groups:
        typer.echo("No leaderless process group is holding capacity on this box.")
        return
    protected_pgids = venue_ancestry_pgids()
    refused = _plan(groups, protected_pgids=protected_pgids, apply_now=apply_now)
    if refused and requested:
        raise typer.Exit(code=1)


def _plan(groups: list[OrphanGroup], *, protected_pgids: set[int], apply_now: bool) -> bool:
    """Print (and optionally execute) one line per group; True iff any was refused."""
    reapable: list[OrphanGroup] = []
    refused = False
    for group in groups:
        refusal = refusal_for(group, protected_pgids=protected_pgids)
        if refusal:
            refused = True
            typer.echo(f"REFUSED  {group.report()}\n         {refusal}")
            continue
        reapable.append(group)
        typer.echo(f"{'REAPING   ' if apply_now else 'WOULD REAP'}  {group.report()}")
    if not apply_now:
        if reapable:
            typer.echo(f"Dry run — re-run with --apply to SIGTERM {len(reapable)} group(s).")
        return refused
    _execute(reapable)
    return refused


def _execute(groups: list[OrphanGroup]) -> None:
    outcomes = {group.pgid: _term(group.pgid) for group in groups}
    if groups:
        time.sleep(_TERM_GRACE_SECONDS)
    for group in groups:
        problem = outcomes[group.pgid]
        if problem:
            typer.echo(f"         pgid {group.pgid}: {problem}")
        elif _survivors(group.pgid):
            typer.echo(f"         pgid {group.pgid}: SURVIVED SIGTERM — inspect it before forcing it down")
        else:
            typer.echo(f"         pgid {group.pgid}: reclaimed ({group.cpu_seconds / 3600:.1f} CPU-hours stopped)")


def register(app: typer.Typer) -> None:
    """Register this module's ``t3 tool`` command(s) onto *app* (called from ``cli/__init__``)."""
    app.command("reap-orphan-groups")(reap_orphan_groups)
