"""``t3 doctor`` gate: the headless browser behind ``t3 browser`` and the visual-QA gate can launch."""

import sys

import typer

from teatree.utils.run import run_allowed_to_fail

INSTALL_ARGV = (sys.executable, "-m", "playwright", "install", "chromium-headless-shell")
INSTALL_TIMEOUT_S = 600


def _check_browser_ready(*, repair: bool) -> bool:
    from teatree.browser.keeper import launch_probe  # noqa: PLC0415 — deferred: keeps Playwright off `t3` startup

    failure = launch_probe()
    if failure is not None and repair and _install_browser():
        failure = launch_probe()
    if failure is None:
        typer.echo("OK    headless browser launches (`t3 browser`, visual QA)")
        return True
    typer.echo(
        f"FAIL  the headless browser cannot launch, so `t3 browser` and the visual-QA gate cannot run: "
        f"{failure.splitlines()[0] if failure else 'unknown error'}. "
        f"Run `t3 doctor check --repair` (runs `{' '.join(INSTALL_ARGV)}`)."
    )
    return False


def _install_browser() -> bool:
    return run_allowed_to_fail(INSTALL_ARGV, expected_codes=None, timeout=INSTALL_TIMEOUT_S).returncode == 0
