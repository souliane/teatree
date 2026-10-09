"""``t3 doctor`` gate: the headless browser behind ``t3 browser`` and the visual-QA gate can launch."""

import sys

import typer

from teatree.utils.run import run_allowed_to_fail

INSTALL_ARGV = (sys.executable, "-m", "playwright", "install", "chromium-headless-shell")
INSTALL_TIMEOUT_S = 600


def _check_browser_ready(*, repair: bool) -> bool:
    try:
        from teatree.browser.keeper import (  # noqa: PLC0415 — deferred: keeps Playwright off `t3` startup
            LAUNCH_PROBE_TIMEOUT_MS,
            launch_probe,
        )
    except ImportError as exc:
        typer.echo(
            f"FAIL  Playwright is not importable in the environment running `t3` ({exc}), so `t3 browser` and "
            "the visual-QA gate cannot run. `t3 doctor check --repair` reinstalls an environment that lags its "
            "declared dependencies; run it, and once more if this line remains."
        )
        return False

    failure = launch_probe()
    if failure is not None and not failure.timed_out and repair and _install_browser():
        failure = launch_probe()
    if failure is None:
        typer.echo("OK    headless browser launches (`t3 browser`, visual QA)")
        return True
    if failure.timed_out:
        typer.echo(
            f"WARN  the headless browser did not launch within {LAUNCH_PROBE_TIMEOUT_MS // 1000}s — a saturated "
            "host and a hung browser look the same here; re-run `t3 doctor check` when the host is idle."
        )
        return True
    typer.echo(
        f"FAIL  the headless browser cannot launch, so `t3 browser` and the visual-QA gate cannot run: "
        f"{failure.reason.splitlines()[0] if failure.reason else 'unknown error'}. "
        f"Run `t3 doctor check --repair` (runs `{' '.join(INSTALL_ARGV)}`)."
    )
    return False


def _install_browser() -> bool:
    return run_allowed_to_fail(INSTALL_ARGV, expected_codes=None, timeout=INSTALL_TIMEOUT_S).returncode == 0
