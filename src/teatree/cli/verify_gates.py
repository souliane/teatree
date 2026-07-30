"""t3 tool verify-gates -- the one CI-parity local gate command.

Registers onto the shared tool_app (side-effect import from cli/__init__,
mirroring comment_density_tools / test_shape_tools).

A plain ``prek run --all-files`` only fires the commit/manual-stage hooks:
``.pre-commit-config.yaml`` sets ``default_stages: [commit, manual]``. The
push-stage gates (refuse-public-push-with-leak, doc-update-gate,
comment-density, ensure-pr) carry ``stages: [push]`` and are STRUCTURALLY
skipped -- yet CI re-runs them on the PR-vs-base diff. So a builder reporting
"local prek is green" can be honest about the commit-stage hooks while blind
to the exact push-stage gate CI fails on.

This command runs BOTH stages -- ``prek run --all-files`` and
``prek run --all-files --hook-stage pre-push`` -- against the working tree and
returns the combined exit code as the single green-proof, making "local green"
== "CI green" by construction. Skills and the headless builder dispatch prompt
point at this one command instead of the bare ``prek run --all-files``.

Note the stage name: prek's ``--hook-stage`` accepts the canonical
``pre-push`` value (the config's ``stages: [push]`` is the legacy alias prek
maps onto ``pre-push``). The literal ``--hook-stage push`` is rejected by prek.
"""

import shutil

import typer

from teatree.utils.run import run_streamed

# prek's ``--hook-stage`` flag expects the canonical stage name. The config's
# ``stages: [push]`` alias resolves to this; passing ``push`` verbatim errors.
_PUSH_STAGE = "pre-push"


def _prek_available() -> bool:
    return shutil.which("prek") is not None


def verify_gates() -> None:
    """Run the FULL CI-equivalent local gate set (commit AND push stages).

    Runs ``prek run --all-files`` then ``prek run --all-files --hook-stage
    pre-push`` and exits non-zero if EITHER stage fails. The push-stage run is
    what catches the gates CI fails on but a bare ``prek run --all-files``
    cannot see (comment-density, doc-update, ensure-pr, the public-repo leak
    gate). The full test suite is NOT a push gate -- push -> CI runs it. Report
    this command's exit code as the green-proof
    before declaring a branch review-ready -- not a commit-stage-only run.
    """
    if not _prek_available():
        typer.echo(
            "verify-gates: prek not found on PATH. Install prek (the pre-commit "
            "runner) so the local gate set matches CI.",
            err=True,
        )
        raise typer.Exit(code=1)

    stages = (
        ("commit + manual", ["prek", "run", "--all-files"]),
        ("pre-push (CI-parity gates)", ["prek", "run", "--all-files", "--hook-stage", _PUSH_STAGE]),
    )
    failed: list[str] = []
    for label, cmd in stages:
        typer.echo(f"== verify-gates: {label} ==", err=True)
        # ``check=False`` inherits stdio (live per-hook output) and lets us
        # collect every failing stage in one pass instead of stopping at the first.
        if run_streamed(cmd, check=False) != 0:
            failed.append(label)

    if failed:
        typer.echo(f"verify-gates: FAILED stage(s): {', '.join(failed)}", err=True)
        raise typer.Exit(code=1)
    typer.echo("verify-gates: all gate stages green (commit + push).", err=True)


def register(app: typer.Typer) -> None:
    """Register this module's ``t3 tool`` command(s) onto *app* (called from ``cli/__init__``)."""
    app.command("verify-gates")(verify_gates)
