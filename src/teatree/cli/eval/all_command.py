"""``t3 eval all`` — the explicit named form of the bare-``t3 eval`` full suite.

Split out of :mod:`teatree.cli.eval.app` so the command module stays under the
module-health LOC cap. Both this command and the bare-``t3 eval`` callback in
``app.py`` forward the same seven flags to :func:`teatree.cli.eval.all.run_full_suite`,
so they run byte-for-byte the same suite; this module is just the named-subcommand
surface for scripts/CI that spell the full run out.
"""

from pathlib import Path

import typer

from teatree.cli.eval.all import STRICT_HELP, run_full_suite
from teatree.eval.backends import SUBSCRIPTION_BACKEND
from teatree.eval.parallel import DEFAULT_PARALLEL


def all_lanes(  # noqa: PLR0913, PLR0917 — typer command: each param maps 1:1 to a public `t3 eval all` flag. The arg list IS the CLI contract.
    backend: str = typer.Option(
        SUBSCRIPTION_BACKEND,
        "--backend",
        help=(
            "AI-lane backend: 'subscription' (default — grade in-session transcripts, no API spend) "
            "or 'sdk' (the metered in-process Agent-SDK runner, authed by CLAUDE_CODE_OAUTH_TOKEN; "
            "the explicit CI opt-in via the standalone eval.yml job; ANTHROPIC_API_KEY also honored "
            "as a legacy alternative)."
        ),
    ),
    transcript_dir: Path | None = typer.Option(
        None,
        "--transcript-dir",
        help="Directory of <scenario>.jsonl subscription transcripts for the AI lane (default: cwd).",
    ),
    free_only: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--free-only",
        help="Run only the free deterministic lanes (drop the AI lane) — the fast pre-push gate.",
    ),
    strict: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--strict",
        help=STRICT_HELP,
    ),
    docker: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--docker",
        help="Run inside the exact CI image (dev/Dockerfile.test) for parity; host-run is the default.",
    ),
    local: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--local",
        help=(
            "Run a metered `--backend sdk` suite on the HOST instead of the default CI container — a "
            "quick local check only, NOT the reproducible gate (use Docker/CI for that)."
        ),
    ),
    parallel: int = typer.Option(
        DEFAULT_PARALLEL,
        "--parallel",
        help="Run this many AI-lane scenarios concurrently (wall-clock; default 1 = sequential).",
    ),
    html: Path | None = typer.Option(
        None,
        "--html",
        help="Write a self-contained whole-suite HTML report to this path (CI artifact).",
    ),
) -> None:
    """Run every eval lane in sequence and render one unified summary table + verdict.

    The explicit form of the bare-``t3 eval`` default — both call
    :func:`run_full_suite`, so they run byte-for-byte the same suite (see that
    callback for the flag semantics, including ``--html``). Kept as a named
    subcommand for scripts/CI that spell the full run out.
    """
    run_full_suite(
        backend=backend,
        transcript_dir=transcript_dir,
        free_only=free_only,
        docker=docker,
        strict=strict,
        local=local,
        parallel=parallel,
        html_path=html,
    )
