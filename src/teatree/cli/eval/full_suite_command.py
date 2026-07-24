"""The bare ``t3 eval`` full-suite callback — every lane in one aggregated run."""

from pathlib import Path

import typer

from teatree.cli.eval.all import STRICT_HELP, run_full_suite
from teatree.eval.backends import TRANSCRIPT_BACKEND
from teatree.eval.parallel import DEFAULT_PARALLEL


def register_full_suite_callback(eval_app: typer.Typer) -> None:
    """Wire the ``invoke_without_command`` full-suite entry onto *eval_app*."""

    @eval_app.callback(invoke_without_command=True)
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def default(  # noqa: PLR0913, PLR0917 — typer callback: each param maps 1:1 to a public bare-``t3 eval`` flag. The arg list IS the CLI contract.
        ctx: typer.Context,
        backend: str = typer.Option(
            TRANSCRIPT_BACKEND,
            "--backend",
            help=(
                "AI-lane backend for the bare-`t3 eval` full suite: 'transcript' (default — REUSE "
                "already-recorded in-session transcripts, $0 extra), 'api' (RUN the Claude model "
                "fresh in-process via the Agent SDK, on the credential agent_harness_provider "
                "selects — default subscription OAuth, or the metered API key; the "
                "explicit opt-in), 'anthropic_api' (RUN the same Claude model fresh through the "
                "Anthropic Messages API DIRECTLY, no `claude` CLI child — the CLI-free lane, metered "
                "on ANTHROPIC_API_KEY), or 'pydantic_ai' (RUN a non-Claude model through the "
                "provider-agnostic harness seam, the OpenAI-compatible backend)."
            ),
        ),
        transcript_dir: Path | None = typer.Option(
            None,
            "--transcript-dir",
            help="Directory of <scenario>.jsonl transcripts for the AI lane (default: cwd).",
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
        parallel: int = typer.Option(
            DEFAULT_PARALLEL,
            "--parallel",
            help="Run this many AI-lane scenarios concurrently (wall-clock; default 1 = sequential).",
        ),
    ) -> None:
        """Run the WHOLE eval suite. Pass a subcommand to target one lane instead.

        Bare ``t3 eval`` runs every lane in one go and prints a single aggregated
        summary table plus a plain-language verdict — the default. Subcommands are the
        targeted path: ``run`` (a single AI scenario, the fresh-run ``--backend api``
        path), one-free-lane (``pinned-regressions`` / ``negative-control`` / …), and
        introspection (``history`` / ``list`` / ``prepare-transcript``). The process
        exits non-zero if ANY lane fails (fail-loud); ``--strict`` also fails on a
        setup-skipped lane.
        """
        if ctx.invoked_subcommand is not None:
            return
        run_full_suite(
            backend=backend,
            transcript_dir=transcript_dir,
            free_only=free_only,
            docker=docker,
            strict=strict,
            parallel=parallel,
        )
