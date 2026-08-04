"""``_check_*`` probe for the cold-hook settings store invoked by `t3 doctor check`.

The cold-hook gates do NOT read settings the way the CLI does. `t3` runs from a
uv-tool venv that can import ``teatree``; the hooks run under whatever
``hooks/scripts/run-hook.sh`` picks off ``PATH`` (the first
``python3.13|3.12|3.11|python3``), which on many hosts is a bare system interpreter
with no ``teatree`` installed. Two interpreters, one store — and only one of them
may be able to reach it.

That asymmetry produced #3499: the hook's reader could not import ``teatree`` at all,
every DB-home flag silently resolved to its compiled-in default, and nothing
surfaced it — ``t3 <overlay> config_setting get`` cheerfully reported the stored
value the hooks were not using. This probe closes that blind spot by asking the
HOOK's interpreter what it actually resolves, and comparing it against the CLI.

:func:`_check_autoload_engages_platform_skill` covers the OTHER half of the same
blind spot. A flag that reads back correctly still buys the owner nothing if the
ENGAGEMENT it is supposed to drive never materialises: every link after the read
— the engagement seam, the demand set, the canonical token, the resolver the
skill-loading gate consults — fails silently and looks exactly like "the
operator never opted in". That check walks the whole chain under the hook's own
interpreter and FAILS when a stored ``autoload = true`` produces no enforceable
platform-skill demand.
"""

import json
import shutil
import subprocess  # noqa: S404 — imported only for the TimeoutExpired/SubprocessError types caught below
from dataclasses import dataclass
from pathlib import Path

import typer

from teatree.utils.run import run_allowed_to_fail

# Imports the leaf under its BARE identity with only the scripts dir on ``sys.path`` —
# exactly how the live hook reaches it — and reports what it resolves. ``autoload`` is
# the representative flag: every cold-hook gate kill-switch goes through the same
# reader, so if this one cannot be read, none of them can.
_PROBE = """
import json, sys
sys.path.insert(0, {scripts_dir!r})
try:
    from teatree_settings import autoload_enabled, read_cold_setting_status
    _, status = read_cold_setting_status("autoload")
    print(json.dumps({{"status": status, "autoload": autoload_enabled()}}))
except Exception as exc:
    print(json.dumps({{"status": "probe_failed", "error": exc.__class__.__name__ + ": " + str(exc)}}))
"""

# Walks the engagement chain the way a live session does, under the interpreter
# ``run-hook.sh`` picks: the hook-side ``autoload`` read, the demand that read
# produces at the engagement seam, and whether the canonical token that demand
# lands in ``<session>.pending`` as still resolves for the skill-loading gate.
# Every link is a place the owner's opt-in has silently evaporated.
#
# The lane is reported from HERE rather than read in the CLI process, because the
# demand is only interpretable against the lane that produced it — and this is the
# process that produced it. A second reading in the doctor could disagree with the
# one the seam actually consulted, which is the drift ``session_lane`` exists to
# foreclose.
_ENGAGEMENT_PROBE = """
import json, sys
sys.path.insert(0, {plugin_root!r})
try:
    from hooks.scripts.engagement import autoload_skill_demand
    from hooks.scripts.hook_router import _skill_resolves, _skill_search_dirs, normalize_skill_name
    from hooks.scripts.session_lane import session_lane
    demand = autoload_skill_demand([])
    search_dirs = _skill_search_dirs()
    enforceable = [normalize_skill_name(s) for s in demand if _skill_resolves(normalize_skill_name(s), search_dirs)]
    print(json.dumps({{"status": "ok", "lane": session_lane(), "demand": demand, "enforceable": enforceable}}))
except Exception as exc:
    print(json.dumps({{"status": "probe_failed", "error": exc.__class__.__name__ + ": " + str(exc)}}))
"""

_PROBE_TIMEOUT_SECONDS = 30

# ``HookResolution.status`` vocabulary.
_STATUS_OK = "ok"
_STATUS_PROBE_FAILED = "probe_failed"

# ``hooks.scripts.session_lane.LANE_SDK``, restated rather than imported: ``hooks/``
# is a repo-root sibling of ``src/`` and ships in no teatree distribution, which is
# why every reference to it here is a resolved PATH. The two copies are bound by
# ``tests/teatree_cli/doctor/test_autoload_engagement_check.py``.
_LANE_SDK = "sdk"


@dataclass(frozen=True)
class HookResolution:
    """What the HOOK's interpreter reports for the cold-hook settings store.

    ``status`` is the hook-side read status (:data:`_STATUS_OK`, the leaf's own
    ``unreadable``, or :data:`_STATUS_PROBE_FAILED` when the probe itself blew up
    inside the hook interpreter). ``autoload`` is the flag as the HOOK resolves it,
    and is meaningful only when ``status`` is :data:`_STATUS_OK`.
    """

    status: str
    autoload: bool | None = None
    error: str = ""


def _run_hook_probe(repo_root: Path, source: str) -> dict | None:
    """Run *source* under the interpreter ``run-hook.sh`` picks; its JSON, or ``None``.

    Routes through ``run-hook.sh`` rather than :data:`sys.executable` on purpose — the
    shim's interpreter SELECTION is half the bug, so probing with the CLI's own Python
    would report a healthy read that the live hook never performs.

    ``None`` means UNASKABLE (no bash, no shim, spawn failure, empty or unparsable
    output), which every caller must degrade to a WARN rather than a FAIL.
    """
    runner = repo_root / "hooks" / "scripts" / "run-hook.sh"
    bash = shutil.which("bash")
    if bash is None or not runner.is_file():
        return None
    try:
        proc = run_allowed_to_fail(
            [bash, str(runner), "-c", source],
            # The shim exits 0 even when it finds no usable interpreter, and a crashing
            # probe still prints its JSON — so ANY exit code is informative here and the
            # stdout parse below is what decides. A raise would defeat the WARN path.
            expected_codes=None,
            timeout=_PROBE_TIMEOUT_SECONDS,
            cwd=repo_root,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    stdout = proc.stdout.strip()
    if not stdout:
        return None
    try:
        parsed = json.loads(stdout.splitlines()[-1])
    except (ValueError, IndexError):
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("status"), str):
        return None
    return parsed


def _hook_interpreter_resolution(repo_root: Path) -> HookResolution | None:
    """Ask the hook's own interpreter what it resolves for ``autoload``; ``None`` if unaskable."""
    scripts_dir = repo_root / "hooks" / "scripts"
    parsed = _run_hook_probe(repo_root, _PROBE.format(scripts_dir=str(scripts_dir)))
    if parsed is None:
        return None
    autoload = parsed.get("autoload")
    return HookResolution(
        status=parsed["status"],
        autoload=autoload if isinstance(autoload, bool) else None,
        error=str(parsed.get("error", "")),
    )


def _check_cold_hook_settings_readable() -> bool:
    """FAIL when the hook's interpreter cannot read the settings store, or disagrees with the CLI (#3499).

    Three outcomes:

    * the hook reports the store UNREADABLE — hard FAIL. Every cold-hook gate is then
        running on its built-in default rather than the operator's configuration, and
        each ``t3 <overlay> gate <name> disable/enable`` write is inert.
    * the hook reads the store but resolves ``autoload`` differently from the CLI —
        hard FAIL naming both values. This is the CLI/hook disagreement that let a
        ``True`` setting present as "never opted in".
    * agreement — silently OK.

    Crash-proof: an unaskable probe (missing shim, no interpreter, timeout, unparsable
    output) is a WARN, never a hard FAIL — an undiagnosable environment must not turn
    a doctor run red on this check alone.
    """
    import teatree  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps CLI startup light

    repo_root = Path(teatree.__file__).resolve().parents[2]
    resolution = _hook_interpreter_resolution(repo_root)
    if resolution is None:
        typer.echo(
            "WARN  Could not ask the hook's interpreter what it resolves for the cold-hook "
            "settings store (probe did not run). Cold-hook gate flags are unverified.",
        )
        return True

    if resolution.status == _STATUS_PROBE_FAILED:
        typer.echo(
            f"WARN  Cold-hook settings probe crashed inside the hook interpreter: "
            f"{resolution.error}. Cold-hook gate flags are unverified.",
        )
        return True
    if resolution.status != _STATUS_OK:
        typer.echo(
            "FAIL  The hook's interpreter CANNOT read the teatree settings store, so every "
            "cold-hook gate is running on its built-in default instead of your configuration "
            "— each `t3 <overlay> gate <name> disable/enable` write is inert. Typically the "
            "interpreter that `hooks/scripts/run-hook.sh` selects cannot import teatree. "
            "Re-run `t3 setup`, then re-run `t3 doctor check`.",
        )
        return False

    hook_autoload = resolution.autoload
    cli_autoload = get_effective_settings().autoload
    if hook_autoload != cli_autoload:
        typer.echo(
            f"FAIL  The CLI and the hooks disagree on `autoload`: the CLI resolves "
            f"{cli_autoload}, the hook's interpreter resolves {hook_autoload}. Sessions "
            f"follow the HOOK's answer, so teatree behaves opposite to what "
            f"`t3 <overlay> config_setting get autoload` reports. Re-run `t3 setup`, then "
            f"re-run `t3 doctor check`.",
        )
        return False
    return True


def _check_config_override_tier_healthy() -> bool:
    """FAIL when the ``ConfigSetting`` override tier degraded recently (#3873).

    The fault this surfaces is invisible by construction: a runtime read failure resolves
    every DB-home setting to a shipped default, and two of those defaults (``autonomy``,
    ``mode``) are the MOST permissive value the setting has. The resolver now fails those
    keys closed and records the degradation beside the control DB; without this check the
    record would sit there unread, which is the state #3873 was filed about — the only
    signal was a worker log line nobody tails.

    Crash-proof and fail-open: an unreadable/absent marker is "healthy". A health check
    that reddens because it could not read its own evidence teaches operators to ignore it.
    """
    from teatree.config.override_read_health import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        MARKER_TTL_SECONDS,
        degraded_read_report,
        marker_path,
    )

    try:
        report = degraded_read_report()
    except Exception as exc:  # noqa: BLE001 — a doctor probe never raises out of a doctor run
        typer.echo(f"WARN  Could not read the config-tier health marker: {exc}. Tier health unverified.")
        return True
    if report is None:
        return True
    # The caller is what makes the record actionable (#3980): the deterministic fault this tier
    # actually hit in production — a sync ORM read from inside an event loop — is settled by WHERE
    # the read was made, and the traceback at the read holds only the ORM frames.
    called_from = f" Called from: {'; '.join(report.callers)}." if report.callers else ""
    typer.echo(
        f"FAIL  The ConfigSetting override tier FAILED to read {report.occurrences} time(s) "
        f"(scopes: {', '.join(report.scopes)}; most recent {int(report.age_seconds)}s ago). While "
        "degraded, the autonomy/approval gates resolve to their most RESTRICTIVE value rather "
        "than your stored configuration, so the factory is running more conservatively than you "
        f"configured it to.{called_from} Typical causes are a config read reached from an async "
        "frame (deterministic — fix the caller, not the DB), SQLite lock contention against a "
        "large control DB, an exhausted file-handle budget, or a full disk. Fix the underlying "
        "fault; the record clears itself once no further read fails for "
        f"{MARKER_TTL_SECONDS // 3600}h, or delete {marker_path()} to acknowledge it now."
    )
    return False


def _check_autoload_engages_platform_skill() -> bool:
    """FAIL when a stored ``autoload = true`` yields no ENFORCEABLE platform-skill demand.

    Reading the flag correctly is not the contract the owner cares about —
    "teatree is loaded on every new session" is. Between the stored ``True`` and
    that outcome sit the engagement seam, the demand set, the canonical token
    the demand is written as, and the resolver the skill-loading gate consults.
    Every one of them degrades SILENTLY and to the same observable state as a
    session where autoload was never switched on, which is why this failure has
    to be hand-diagnosed each time it recurs.

    So the check does not compare a flag to a flag. It reads ``autoload`` from
    the DB (the CLI's answer), asks the LIVE hook path — the same modules, under
    the interpreter ``run-hook.sh`` selects — what an engaged session would
    actually be made to load, and FAILS when the two disagree.

    Off when ``autoload`` is off: nothing was claimed, so there is nothing to
    contradict. Off too when the probe reports a POSITIVELY SDK lane, where the
    seam withholds the skill on purpose — the demand is enforced by a
    ``PreToolUse`` gate that refuses every ``Edit``/``Write``/``Bash``, so
    engaging a headless worker would block the factory this check protects. An
    empty demand there is the contract being honoured, not the chain degrading.
    Only a positively SDK lane is excused: an UNKNOWN lane (a doctor run from a
    plain shell carries no Claude markers) keeps the FAIL, mirroring the seam's
    own resolution of an unreadable signal toward the attended reading.

    An UNASKABLE probe (no bash, no shim, spawn failure, unparsable output) is a
    WARN — an undiagnosable environment must not turn a doctor run red on this
    check alone. A probe that RAN and crashed is a FAIL, not a WARN: the live
    hook path could not compute the demand, which settles the question rather
    than leaving it open.
    """
    import teatree  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps CLI startup light

    if not get_effective_settings().autoload:
        return True

    repo_root = Path(teatree.__file__).resolve().parents[2]
    parsed = _run_hook_probe(repo_root, _ENGAGEMENT_PROBE.format(plugin_root=str(repo_root)))
    if parsed is None:
        typer.echo(
            "WARN  Could not ask the hook's interpreter what an autoloaded session engages "
            "(probe did not run). Autoload engagement is unverified.",
        )
        return True
    if parsed["status"] != _STATUS_OK:
        typer.echo(
            f"FAIL  `autoload` is true in the settings store, but the LIVE hook path cannot "
            f"even compute what an engaged session must load: {parsed.get('error', '')}. Every "
            f"new session therefore starts without the teatree skill and behaves exactly as if "
            f"you had never opted in. Re-run `t3 setup`, then re-run `t3 doctor check`.",
        )
        return False

    enforceable = parsed.get("enforceable")
    if isinstance(enforceable, list) and enforceable:
        return True
    if parsed.get("lane") == _LANE_SDK:
        typer.echo(
            "WARN  `autoload` is true, but this doctor run sits in the SDK lane, where the "
            "platform skill is withheld by design — the gate enforcing it would refuse every "
            "edit the factory's own workers make. Attended-session engagement is unverified "
            "from here; re-run `t3 doctor check` from an interactive session to check it.",
        )
        return True
    typer.echo(
        f"FAIL  `autoload` is true in the settings store, but the LIVE hook path engages no "
        f"platform skill: it resolved demand={parsed.get('demand')!r} and "
        f"enforceable={enforceable!r}. Every new session therefore starts without the teatree "
        f"skill and behaves exactly as if you had never opted in — the symptom is that you "
        f"keep loading it by hand. Re-run `t3 setup`, then re-run `t3 doctor check`.",
    )
    return False
