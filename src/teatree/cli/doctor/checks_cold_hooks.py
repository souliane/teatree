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

_PROBE_TIMEOUT_SECONDS = 30

# ``HookResolution.status`` vocabulary.
_STATUS_OK = "ok"
_STATUS_PROBE_FAILED = "probe_failed"


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


def _hook_interpreter_resolution(repo_root: Path) -> HookResolution | None:
    """Ask the hook's own interpreter what it resolves for ``autoload``; ``None`` if unaskable.

    Routes through ``run-hook.sh`` rather than :data:`sys.executable` on purpose — the
    shim's interpreter SELECTION is half the bug, so probing with the CLI's own Python
    would report a healthy read that the live hook never performs.
    """
    runner = repo_root / "hooks" / "scripts" / "run-hook.sh"
    scripts_dir = repo_root / "hooks" / "scripts"
    bash = shutil.which("bash")
    if bash is None or not runner.is_file():
        return None
    try:
        proc = run_allowed_to_fail(
            [bash, str(runner), "-c", _PROBE.format(scripts_dir=str(scripts_dir))],
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
