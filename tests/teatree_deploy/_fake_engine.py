"""A ``ComposeEngine`` double that keeps the running stack in memory and boots the registry like a worker."""

import os
import signal
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.recorder import MigrationRecorder

from teatree.core.models import ConfigSetting, WorkerGeneration
from teatree.deploy.roll import ContainerState, RollError, RollInterruptedError


def quiescing() -> bool:
    return bool(ConfigSetting.objects.get_effective("worker_quiescing"))


@dataclass
class FakeEngine:
    """Records every call and keeps the running stack as ``service -> generation``."""

    images: dict[str, str]
    running: dict[str, str]
    fail_init: bool = False
    #: A migration N+1's init records before it fails or succeeds — the schema moving under N.
    init_applies: str = ""
    #: Raised from inside init, standing in for a signal delivered mid-roll.
    interrupt_init: type[BaseException] | None = None
    #: A signal init sends to this very process, as an operator's kill or an SSH hangup would.
    signal_init: int | None = None
    #: Something another process does to the control DB while init runs.
    on_init: Callable[[], None] | None = None
    #: A signal the restore's ``up`` of this generation receives, as a second kill mid-rollback would.
    signal_up_of: str | None = None
    admin_up: bool = True
    #: A generation whose admin never answers while the other generations' does.
    admin_down_for: str | None = None
    #: Whether a generation's worker registers and activates itself on boot, as a real one does.
    worker_boots: bool = True
    fail_up_of: str | None = None
    fail_promote_once: bool = False
    fail_promote_of: str | None = None
    #: A termination signal landing while the verified generation is promoted.
    interrupt_promote: bool = False
    #: A termination signal landing while preflight reads the target image.
    interrupt_preflight: bool = False
    #: Containers that exist but are stopped, as ``service -> generation``.
    stopped: dict[str, str] = field(default_factory=dict)
    #: Services whose container restarts between every two inspections, as a crash loop under a restart policy does.
    crash_looping: set[str] = field(default_factory=set)
    #: The generation whose crash-looping services loop; another generation runs them steadily.
    crash_loop_of: str = ""
    _restarts: dict[str, int] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    promoted: str = ""
    quiescing_at_stop: list[bool] = field(default_factory=list)

    def image_revision(self, image: str) -> str:
        self.calls.append(("image_revision", image))
        if self.interrupt_preflight:
            signal_name = "SIGTERM"
            raise RollInterruptedError(signal_name)
        return self.images.get(image, "")

    def run_init(self, generation: str) -> None:
        self.calls.append(("run_init", generation))
        if self.on_init is not None:
            self.on_init()
        if self.init_applies:
            MigrationRecorder(connections[DEFAULT_DB_ALIAS]).record_applied("core", self.init_applies)
        if self.interrupt_init is not None:
            raise self.interrupt_init
        if self.signal_init is not None:
            os.kill(os.getpid(), self.signal_init)
        if self.fail_init:
            msg = "teatree-init exited 1"
            raise RollError(msg)

    def up(self, generation: str, services: Sequence[str]) -> None:
        self.calls.append(("up", generation, *services))
        if self.signal_up_of is not None and generation == self.signal_up_of:
            os.kill(os.getpid(), signal.SIGTERM)
        if generation == self.fail_up_of:
            msg = f"compose up of {generation or 'legacy'} failed"
            raise RollError(msg)
        for service in services:
            self.stopped.pop(service, None)
        self.running.update(dict.fromkeys(services, generation))
        if generation and self.worker_boots and "teatree-worker" in services:
            WorkerGeneration.objects.boot(generation)

    def stop(self, services: Sequence[str]) -> None:
        self.calls.append(("stop", *services))
        self.quiescing_at_stop.append(quiescing())
        for service in services:
            if service in self.running:
                self.stopped[service] = self.running.pop(service)

    def inspect(self, services: Sequence[str]) -> dict[str, ContainerState]:
        for service in self.crash_looping & set(services):
            if self.running.get(service) == self.crash_loop_of:
                self._restarts[service] = self._restarts.get(service, 0) + 1
        return {
            service: ContainerState(
                running=service in self.running,
                revision=self.running.get(service, self.stopped.get(service, "")),
                restarts=self._restarts.get(service, 0),
                started_at=f"start-{self._restarts.get(service, 0)}",
                present=service in self.running or service in self.stopped,
            )
            for service in services
        }

    def admin_answers(self) -> bool:
        self.calls.append(("admin_answers",))
        serving = self.running.get("teatree-admin")
        return self.admin_up and serving is not None and serving != self.admin_down_for

    def promote(self, generation: str) -> None:
        self.calls.append(("promote", generation))
        if self.interrupt_promote:
            signal_name = "SIGTERM"
            raise RollInterruptedError(signal_name)
        if self.fail_promote_once or generation == self.fail_promote_of:
            self.fail_promote_once = False
            reason = "docker tag failed"
            raise RollError(reason)
        self.promoted = generation

    def verbs(self) -> list[str]:
        return [call[0] for call in self.calls if call[0] != "admin_answers"]
