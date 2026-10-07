"""Roll the runtime stack from the serving generation N to an immutable image generation N+1.

Phase 1 is sequential: N stops claiming, drains, and stops before N+1 starts. N is what the
running worker container carries, not what the registry last recorded. A failure before N's
drain starts fails N+1 and leaves N untouched. Anything that fails — or interrupts the roll —
after it rolls back: N+1 is marked failed, N resumes, is brought back up from its own
still-present image, and must pass the same verification N+1 had to. A failure after N+1's init
moved the schema is never rolled back, because N's code would refuse every claim against it.
The promoted tag the watchdog restarts from moves only once N+1 is verified.
"""

import logging
import signal
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from types import FrameType
from typing import Protocol

from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.recorder import MigrationRecorder

from teatree.core.models import WorkerGeneration
from teatree.core.process_freshness import FreshnessVerdict, read_process_freshness
from teatree.generation import generation_image, is_generation_sha, short_sha
from teatree.loop.drain import DEFAULT_DRAIN_TIMEOUT_SECONDS, DrainPacing, drain_worker, set_worker_quiescing

logger = logging.getLogger(__name__)

CLAIMING_SERVICES = ("teatree-worker", "teatree-slack-listener")
RUNTIME_SERVICES = (*CLAIMING_SERVICES, "teatree-admin", "teatree-watchdog")
_ESSENTIAL_SERVICES = ("teatree-worker", "teatree-admin")
_TERMINATION_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


class RollError(RuntimeError):
    """A roll could not proceed, or could not be undone."""


class RollInterruptedError(BaseException):
    """A termination signal reached the roller; a BaseException so nothing on the way swallows it."""


@contextmanager
def termination_signals_interrupt() -> Iterator[None]:
    """Turn SIGTERM/SIGHUP into :class:`RollInterruptedError` so a killed roll still restores what serves."""

    def _interrupt(signum: int, _frame: FrameType | None) -> None:
        # A repeated kill must not abort the restore the first one started.
        for repeated in _TERMINATION_SIGNALS:
            signal.signal(repeated, signal.SIG_IGN)
        raise RollInterruptedError(signal.Signals(signum).name)

    previous = {signum: signal.signal(signum, _interrupt) for signum in _TERMINATION_SIGNALS}
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@dataclass(frozen=True, slots=True)
class ContainerState:
    running: bool
    revision: str
    restarts: int = 0
    started_at: str = ""
    #: A container exists for the service, running or not.
    present: bool = True


class ComposeEngine(Protocol):
    """The container operations a roll needs; ``generation`` ``""`` names the legacy source-mounted stack."""

    def image_revision(self, image: str) -> str: ...

    def run_init(self, generation: str) -> None: ...

    def up(self, generation: str, services: Sequence[str]) -> None: ...

    def stop(self, services: Sequence[str]) -> None: ...

    def inspect(self, services: Sequence[str]) -> dict[str, ContainerState]: ...

    def admin_answers(self) -> bool: ...

    def promote(self, generation: str) -> None: ...


class RollOutcome(Enum):
    ROLLED = "rolled"
    ALREADY_CURRENT = "already_current"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True, slots=True)
class RollReport:
    outcome: RollOutcome
    from_generation: str
    to_generation: str
    detail: str = ""
    still_claimed: list[int] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RollTiming:
    drain_timeout: int = DEFAULT_DRAIN_TIMEOUT_SECONDS
    verify_timeout: float = 300.0
    #: How long a service must keep one container start before it counts as up — a crash loop never does.
    stable_seconds: float = 30.0
    pacing: DrainPacing = field(default_factory=DrainPacing)


class _Stability:
    """When each service's current container start was first seen, so a restart resets its clock."""

    def __init__(self, *, window: float, clock: Callable[[], float]) -> None:
        self._window = window
        self._clock = clock
        self._first_seen: dict[str, tuple[tuple[int, str], float]] = {}

    def steady(self, service: str, state: ContainerState) -> bool:
        start = (state.restarts, state.started_at)
        now = self._clock()
        seen = self._first_seen.get(service)
        if seen is None or seen[0] != start:
            self._first_seen[service] = (start, now)
            return self._window <= 0
        return now - seen[1] >= self._window


def _registry_exists() -> bool:
    introspection = connections[DEFAULT_DB_ALIAS].introspection
    return WorkerGeneration in introspection.installed_models(introspection.table_names())


def _applied_migrations() -> frozenset[tuple[str, str]]:
    return frozenset(MigrationRecorder(connections[DEFAULT_DB_ALIAS]).applied_migrations())


def _describe(generation: str) -> str:
    return short_sha(generation) if generation else "the legacy stack"


@dataclass(slots=True)
class _Replacement:
    """How far a roll got replacing the stack, so a failure undoes exactly that."""

    drain_started: bool = False
    #: The global ``worker_quiescing`` gate was closed rather than one generation's admission.
    gate_closed: bool = False


class Roller:
    def __init__(
        self,
        engine: ComposeEngine,
        *,
        timing: RollTiming | None = None,
        on_step: Callable[[str], None] | None = None,
        optional: frozenset[str] = frozenset(),
    ) -> None:
        if unknown := sorted(optional - set(RUNTIME_SERVICES)):
            msg = f"{', '.join(unknown)} is not a runtime service ({', '.join(RUNTIME_SERVICES)})"
            raise RollError(msg)
        if essential := sorted(optional & set(_ESSENTIAL_SERVICES)):
            msg = f"{', '.join(essential)} cannot be optional: the roll is verified through it"
            raise RollError(msg)
        self._engine = engine
        self._timing = timing or RollTiming()
        self._on_step = on_step or (lambda _step: None)
        self._required = tuple(service for service in RUNTIME_SERVICES if service not in optional)
        #: What serves once :meth:`roll` returns or raises; ``None`` when that is unknown.
        self.serving_after: str | None = None
        #: The target passed verification, so only promotion and retirement can still be missing.
        self.verified = False
        #: A failure after the drain was undone and the previous generation verified again.
        self.restored = False
        #: The roll wrote to the registry or the stack; until then an interrupt changed nothing.
        self.changed_anything = False

    def roll(self, to: str) -> RollReport:
        self._step("preflight")
        self._preflight(to)
        # A stack older than the registry is legacy; N+1's own init creates the table after the drain.
        registry = _registry_exists()
        current = self._running_generation(registry=registry)
        self.serving_after = current
        self.changed_anything = True
        if registry:
            WorkerGeneration.objects.fail_displaced(serving=current)
        live = (WorkerGeneration.State.ACTIVE, WorkerGeneration.State.DRAINING)
        if current == to and registry and WorkerGeneration.objects.state_of(to) in live:
            return self._finish_current(to)

        if registry:
            self._step("register")
            WorkerGeneration.objects.register(to)
        schema_before = _applied_migrations()
        replacement = _Replacement()
        try:
            still_claimed = self._replace(current, to, register=not registry, replacement=replacement)
        except BaseException as exc:
            reason = str(exc) or type(exc).__name__
            logger.exception("roll to %s failed — restoring %s", short_sha(to), _describe(current))
            if not replacement.drain_started:
                self._fail(to, reason)
                raise
            if current == to:
                self._abandon_restart(to, reason, reopen_gate=replacement.gate_closed)
                raise
            self.serving_after = None
            self._recover(current, to, schema_before, reason=reason, replacement=replacement)
            self.serving_after = current
            self.restored = True
            if not isinstance(exc, Exception):
                raise
            return RollReport(RollOutcome.ROLLED_BACK, current, to, detail=reason)

        self.serving_after = to
        self.verified = True
        self._step("promote")
        self._promote(to)
        self._step("retire")
        self._retire_all_but(to)
        return RollReport(RollOutcome.ROLLED, current, to, still_claimed=still_claimed)

    def _running_generation(self, *, registry: bool) -> str:
        """The generation the worker container runs; the registry goes stale after a deploy outside the roller."""
        worker = self._engine.inspect(("teatree-worker",))["teatree-worker"]
        if worker.present:
            return worker.revision
        serving = WorkerGeneration.objects.serving() if registry else None
        return serving.sha if serving is not None else ""

    @staticmethod
    def _drains_by_generation(current: str, *, registry: bool) -> bool:
        live = (WorkerGeneration.State.ACTIVE, WorkerGeneration.State.DRAINING)
        return bool(current) and registry and WorkerGeneration.objects.state_of(current) in live

    def _step(self, name: str) -> None:
        self._on_step(name)

    def _promote(self, to: str) -> None:
        """A verified generation keeps serving whatever happens to the tag; re-running the roll promotes it."""
        try:
            self._engine.promote(to)
        except RollError:
            try:
                self._engine.promote(to)
            except RollError as exc:
                msg = (
                    f"{short_sha(to)} is verified and serving, but promoting it failed ({exc}); "
                    "re-run the roll to promote it and retire what it replaced"
                )
                raise RollError(msg) from exc

    def _preflight(self, to: str) -> None:
        if not is_generation_sha(to):
            msg = f"--to takes a full 40-hex commit sha, got {to!r}"
            raise RollError(msg)
        image = generation_image(to)
        revision = self._engine.image_revision(image)
        if revision != to:
            found = f"carries revision {revision!r}" if revision else "is not built"
            msg = f"{image} {found} — build it with deploy/build-generation.sh {to}"
            raise RollError(msg)
        # deploy/roll.sh runs the roller from the target image, so this process's code is the target's.
        freshness = read_process_freshness()
        if freshness.verdict is FreshnessVerdict.BEHIND:
            msg = (
                f"{short_sha(to)}'s code is behind the applied schema ({freshness.app_label}.{freshness.applied_head}"
                f" is applied, it knows up to {freshness.loaded_head}) — it would refuse every claim. "
                "Roll to a generation that carries that migration."
            )
            raise RollError(msg)

    def _replace(self, current: str, to: str, *, register: bool, replacement: _Replacement) -> list[int]:
        by_generation = self._drains_by_generation(current, registry=not register)
        self._step("drain")
        replacement.drain_started = True
        replacement.gate_closed = not by_generation
        drained = drain_worker(
            timeout=self._timing.drain_timeout,
            generation=current if by_generation else "",
            pacing=self._timing.pacing,
        )
        self._step("stop")
        self._engine.stop(CLAIMING_SERVICES)
        self._step("init")
        self._engine.run_init(to)
        if register:
            self._step("register")
            WorkerGeneration.objects.register(to)
        if replacement.gate_closed:
            set_worker_quiescing(value=False)
        self._step("up")
        self._engine.up(to, RUNTIME_SERVICES)
        self._step("verify")
        self._verify(to)
        return drained.still_claimed

    def _verify(self, generation: str) -> None:
        pacing = self._timing.pacing
        deadline = pacing.monotonic() + self._timing.verify_timeout
        stability = _Stability(window=self._timing.stable_seconds, clock=pacing.monotonic)
        while problems := self._verification_problems(generation, stability):
            if pacing.monotonic() >= deadline:
                msg = f"{_describe(generation)} did not verify within {self._timing.verify_timeout:.0f}s: "
                raise RollError(msg + "; ".join(problems))
            pacing.sleep(pacing.poll_interval)

    def _verification_problems(self, generation: str, stability: _Stability) -> list[str]:
        problems = []
        for service, state in self._engine.inspect(self._required).items():
            if not (state.running and (state.revision == generation or not generation)):
                running = f"running {state.revision or 'a legacy image'}" if state.running else "not running"
                problems.append(f"{service} is {running}")
            elif not stability.steady(service, state):
                problems.append(f"{service} has not stayed up for {self._timing.stable_seconds:.0f}s")
        if generation and WorkerGeneration.objects.state_of(generation) != WorkerGeneration.State.ACTIVE:
            problems.append("the worker has not activated the generation")
        if not self._engine.admin_answers():
            problems.append("admin does not answer inside its container")
        return problems

    def _recover(
        self,
        current: str,
        to: str,
        schema_before: frozenset[tuple[str, str]],
        *,
        reason: str,
        replacement: _Replacement,
    ) -> None:
        if advanced := sorted(_applied_migrations() - schema_before):
            self._fail(to, reason)
            names = ", ".join(f"{app}.{name}" for app, name in advanced)
            msg = (
                f"roll to {short_sha(to)} failed ({reason}) after its init applied {names} — refusing to roll back: "
                f"{_describe(current)} would refuse every claim on that schema. Roll forward to a fixed generation."
            )
            raise RollError(msg)
        self._roll_back(current, to, reason=reason, reopen_gate=replacement.gate_closed)

    def _roll_back(self, current: str, to: str, *, reason: str, reopen_gate: bool) -> None:
        self._step("rollback")
        try:
            self._engine.stop(CLAIMING_SERVICES)
        except Exception as exc:
            msg = f"roll to {short_sha(to)} failed ({reason}) and restoring {_describe(current)} failed too ({exc})"
            raise RollError(msg) from exc
        try:
            self._fail(to, reason)
        except Exception:
            logger.exception("could not mark %s failed — restoring %s regardless", short_sha(to), _describe(current))
        try:
            if current and _registry_exists():
                WorkerGeneration.objects.reinstate(current)
            if reopen_gate:
                set_worker_quiescing(value=False)
            self._engine.up(current, RUNTIME_SERVICES)
        except Exception as exc:
            msg = f"roll to {short_sha(to)} failed ({reason}) and restoring {_describe(current)} failed too ({exc})"
            raise RollError(msg) from exc
        self._step("verify")
        try:
            self._verify(current)
        except RollError as exc:
            restored = _describe(current)
            msg = f"roll to {short_sha(to)} failed ({reason}); restored {restored} but it did not verify: {exc}"
            raise RollError(msg) from exc

    def _abandon_restart(self, to: str, reason: str, *, reopen_gate: bool) -> None:
        """A failed restart of the generation that already runs has nothing older to restore."""
        self.serving_after = None
        self._fail(to, reason)
        if reopen_gate:
            set_worker_quiescing(value=False)

    @staticmethod
    def _fail(to: str, reason: str) -> None:
        if not _registry_exists():
            return
        failed = WorkerGeneration.objects.for_sha(to).filter(state__in=WorkerGeneration.SUCCESSOR_STATES).first()
        if failed is not None:
            failed.fail(reason=reason)

    def _finish_current(self, to: str) -> RollReport:
        stranded = WorkerGeneration.objects.for_sha(to).filter(state=WorkerGeneration.State.DRAINING).first()
        if stranded is not None:
            stranded.resume()
        self._step("up")
        self._engine.up(to, RUNTIME_SERVICES)
        self._step("verify")
        self._verify(to)
        self.verified = True
        self._step("promote")
        self._promote(to)
        self._step("retire")
        self._retire_all_but(to)
        return RollReport(RollOutcome.ALREADY_CURRENT, to, to)

    @staticmethod
    def _retire_all_but(to: str) -> None:
        for row in WorkerGeneration.objects.filter(state=WorkerGeneration.State.DRAINING).exclude(sha=to):
            row.retire()
