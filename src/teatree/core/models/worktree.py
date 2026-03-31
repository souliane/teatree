import socket
from pathlib import Path
from typing import TYPE_CHECKING, cast

from django.conf import settings
from django.db import models
from django_fsm import FSMField, transition

from teatree.config import load_config
from teatree.core.managers import WorktreeManager
from teatree.core.models.ticket import Ticket
from teatree.utils import ports as port_utils

if TYPE_CHECKING:
    from teatree.core.models.types import Ports, WorktreeExtra


def _workspace_dir() -> Path:
    configured = getattr(settings, "T3_WORKSPACE_DIR", "")
    if configured:
        return Path(str(configured)).expanduser()
    return load_config().workspace_dir


class Worktree(models.Model):
    class State(models.TextChoices):
        CREATED = "created", "Created"
        PROVISIONED = "provisioned", "Provisioned"
        SERVICES_UP = "services_up", "Services up"
        READY = "ready", "Ready"

    overlay = models.CharField(max_length=255)
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="worktrees")
    repo_path = models.CharField(max_length=500)
    branch = models.CharField(max_length=255)
    state = FSMField(max_length=32, choices=State.choices, default=State.CREATED)
    ports = models.JSONField(default=dict, blank=True)
    db_name = models.CharField(max_length=255, blank=True)
    extra = models.JSONField(default=dict, blank=True)

    objects = WorktreeManager()

    def __str__(self) -> str:
        return str(self.repo_path)

    @transition(field=state, source=State.CREATED, target=State.PROVISIONED)
    def provision(self, *, ports: "Ports | None" = None) -> None:
        self.ports = ports or self._allocate_ports()
        self.db_name = self._build_db_name()

    @transition(field=state, source=[State.PROVISIONED, State.SERVICES_UP], target=State.SERVICES_UP)
    def start_services(self, *, services: list[str] | None = None) -> None:
        if services is not None:
            extra = self._extra()
            extra["services"] = services
            self.extra = extra

    @transition(field=state, source=State.SERVICES_UP, target=State.READY)
    def verify(self) -> None:
        extra = self._extra()
        ports = self._ports()
        extra["urls"] = {
            name: f"http://localhost:{port}" for name, port in ports.items() if name not in {"postgres", "redis"}
        }
        self.extra = extra

    @transition(field=state, source=[State.PROVISIONED, State.SERVICES_UP, State.READY], target=State.PROVISIONED)
    def db_refresh(self) -> None:
        from django.utils import timezone  # noqa: PLC0415

        extra = self._extra()
        extra["db_refreshed_at"] = timezone.now().isoformat()
        self.extra = extra

    @transition(field=state, source="*", target=State.CREATED)
    def teardown(self) -> None:
        self.ports = {}
        self.db_name = ""
        self.extra = {}

    def _allocate_ports(self) -> "Ports":
        reserved_ports: port_utils.ReservedPorts = {
            "backend": set(),
            "frontend": set(),
            "postgres": set(),
        }
        for ports in Worktree.objects.exclude(pk=self.pk).values_list("ports", flat=True):
            if not isinstance(ports, dict):
                continue
            for name in reserved_ports:
                value = ports.get(name)
                if isinstance(value, int):
                    reserved_ports[name].add(value)
        backend, frontend, postgres, redis = port_utils.find_free_ports(
            str(_workspace_dir()),
            share_db_server=False,
            reserved_ports=reserved_ports,
        )
        return {
            "backend": backend,
            "frontend": frontend,
            "postgres": postgres,
            "redis": redis,
        }

    def _build_db_name(self) -> str:
        ticket = cast("Ticket", self.ticket)
        variant_suffix = f"_{ticket.variant}" if ticket.variant else ""
        return f"wt_{ticket.ticket_number}{variant_suffix}"

    @staticmethod
    def _port_available(port: int) -> bool:
        """Check if a port is available by attempting to bind to it."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return True
        except OSError:
            return False

    def refresh_ports_if_needed(self) -> bool:
        """Allocate ports if missing, but never reallocate already-assigned ports.

        A port being "in use" is *expected* when the worktree's service is
        running.  Only allocate when the port dict is incomplete (missing
        required keys).
        """
        current = self._ports()
        required = ("backend", "frontend", "postgres")
        if current and all(name in current for name in required):
            return False

        new_ports = self._allocate_ports()
        # Preserve any already-assigned ports (don't reallocate running services)
        merged = {**new_ports, **{k: v for k, v in current.items() if v}}
        if current == merged:
            return False

        self.ports = merged
        self.save(update_fields=["ports"])
        return True

    def revalidate_ports(self) -> dict[str, tuple[int, int]]:
        """Detect port conflicts and reallocate conflicting ports.

        Returns a dict of ``{service: (old_port, new_port)}`` for each port
        that was reassigned.  Empty dict means no conflicts.
        """
        current = self._ports()
        if not current:
            return {}

        conflicts: dict[str, int] = {}
        for name in ("backend", "frontend"):
            port = current.get(name)
            if isinstance(port, int) and not self._port_available(port):
                conflicts[name] = port

        if not conflicts:
            return {}

        new_ports = self._allocate_ports()
        changes: dict[str, tuple[int, int]] = {}
        for name, old_port in conflicts.items():
            new_port = new_ports[name]
            current[name] = new_port
            changes[name] = (old_port, new_port)

        self.ports = current
        self.save(update_fields=["ports"])
        return changes

    def _extra(self) -> "WorktreeExtra":
        return cast("WorktreeExtra", self.extra or {})

    def _ports(self) -> "Ports":
        return cast("Ports", self.ports or {})
