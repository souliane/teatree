"""Roll the runtime stack through Django, where the generation registry is available."""

import threading
import time
from types import TracebackType
from typing import IO, Annotated, Self, cast

import typer
from django_typer.management import TyperCommand

from teatree.generation import short_sha

ROLL_PROGRESS_INTERVAL_SECONDS = 30.0
_ROLLED_BACK_EXIT = 3


class RollProgress:
    """Announce each roll step and repeat it until the next step."""

    def __init__(self, output: IO[str], *, interval: float = ROLL_PROGRESS_INTERVAL_SECONDS) -> None:
        self.output = output
        self.interval = interval
        self._step = "starting"
        self._started = time.monotonic()
        self._stopped = threading.Event()
        self._ticker = threading.Thread(target=self._repeat, name="roll-progress", daemon=True)

    def __call__(self, step: str) -> None:
        self._step = step
        self._echo()

    def __enter__(self) -> Self:
        self._ticker.start()
        return self

    def __exit__(
        self, _exc_type: type[BaseException] | None, _exc: BaseException | None, _tb: TracebackType | None
    ) -> None:
        self._stopped.set()
        self._ticker.join()

    def _repeat(self) -> None:
        while not self._stopped.wait(self.interval):
            self._echo()

    def _echo(self) -> None:
        self.output.write(f"roll: {self._step} ({time.monotonic() - self._started:.0f}s)\n")
        self.output.flush()


class Command(TyperCommand):
    help = "Roll the runtime stack to an immutable image generation."

    def handle(
        self,
        *,
        to: Annotated[str, typer.Option("--to")] = "",
        drain_timeout: Annotated[int, typer.Option("--drain-timeout")] = 1800,
        verify_timeout: Annotated[float, typer.Option("--verify-timeout")] = 300.0,
        stable_seconds: Annotated[float, typer.Option("--stable-seconds")] = 30.0,
        optional_service: Annotated[list[str] | None, typer.Option("--optional-service")] = None,
    ) -> None:
        from teatree.deploy.compose_engine import DockerComposeEngine  # noqa: PLC0415 — deferred: Django-heavy
        from teatree.deploy.roll import (  # noqa: PLC0415 — deferred: Django-heavy
            Roller,
            RollError,
            RollInterruptedError,
            RollOutcome,
            RollTiming,
            termination_signals_interrupt,
        )

        timing = RollTiming(drain_timeout=drain_timeout, verify_timeout=verify_timeout, stable_seconds=stable_seconds)
        roller: Roller | None = None
        with RollProgress(cast("IO[str]", self.stderr)) as progress, termination_signals_interrupt():
            try:
                roller = Roller(
                    DockerComposeEngine.from_environment(),
                    timing=timing,
                    on_step=progress,
                    optional=frozenset(optional_service or []),
                )
                report = roller.roll(to)
            except RollError as exc:
                self.stderr.write(f"roll: FAILED {exc}")
                raise SystemExit(1) from exc
            except RollInterruptedError as exc:
                if roller is None or not roller.changed_anything:
                    self.stderr.write(f"roll: interrupted by {exc} before anything moved — the stack is as it was")
                    raise SystemExit(1) from exc
                code = self._interrupted(
                    to, exc, verified=roller.verified, restored=roller.restored, serving=roller.serving_after
                )
                raise SystemExit(code) from exc

        serving = short_sha(report.from_generation) if report.from_generation else "the legacy stack"
        if report.outcome is RollOutcome.ROLLED_BACK:
            self.stderr.write(f"rolled back: {report.detail} — {serving} is serving")
            raise SystemExit(_ROLLED_BACK_EXIT)
        if report.outcome is RollOutcome.ALREADY_CURRENT:
            self.stdout.write(f"already serving {short_sha(report.to_generation)}")
            return
        self.stdout.write(f"rolled {serving} -> {short_sha(report.to_generation)}")
        if report.still_claimed:
            pks = ", ".join(str(pk) for pk in report.still_claimed)
            self.stdout.write(f"note: task(s) {pks} outlived the drain grace and re-queue through their lease lapse")

    def _interrupted(self, to: str, exc: BaseException, *, verified: bool, restored: bool, serving: str | None) -> int:
        if verified:
            self.stderr.write(
                f"roll: interrupted by {exc} after {short_sha(to)} verified — it serves, but promoting it or retiring "
                f"what it replaced did not finish; re-run deploy/roll.sh {to}"
            )
            return 1
        if serving is None:
            self.stderr.write(f"roll: FAILED interrupted by {exc} — nothing is known to serve")
            return 1
        what = "rolled back" if restored else "roll stopped before the drain"
        serving_name = short_sha(serving) if serving else "the legacy stack"
        self.stderr.write(f"{what}: interrupted by {exc} — {serving_name} is serving")
        return _ROLLED_BACK_EXIT
