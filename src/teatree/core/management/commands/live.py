"""``t3 <overlay> live`` — find, inspect (passive) and steer (active) sessions running now on this host's workers."""

import uuid
from collections.abc import Mapping
from typing import IO, Annotated, NoReturn, cast

import typer
from django.db import DatabaseError
from django_typer.management import TyperCommand, command

from teatree.agents.live_client import (
    LiveClient,
    LiveDeliveryUnknownError,
    LiveOfflineError,
    LiveRefusedError,
    WorkerSessionFacts,
)
from teatree.agents.live_control import DEFAULT_STEER_WAIT_SECONDS, ReceiptPayload
from teatree.core.machine_output import emit
from teatree.core.models import Task
from teatree.core.table_output import print_table

EXIT_REJECTED = 3
EXIT_UNKNOWN_DELIVERY = 4
EXIT_OFFLINE = 5
PASSIVE_LABEL = "PASSIVE — read-only; the agent is not contacted"
ACTIVE_LABEL = "ACTIVE — enters the agent's context"
_EXIT_BY_OUTCOME = {"accepted_current_turn": 0, "rejected": EXIT_REJECTED, "unknown_delivery": EXIT_UNKNOWN_DELIVERY}

_JsonOption = Annotated[bool, typer.Option("--json", help="Emit JSON on stdout instead of the human view.")]
_TaskArgument = Annotated[int, typer.Argument(help="The task id, as `live list` shows it.")]


def _task_list_status(task: int) -> str:
    try:
        status = Task.objects.filter(pk=task).values_list("status", flat=True).first()
    except DatabaseError as exc:
        return f"unreadable: {type(exc).__name__}"
    return status or "no such task"


def _offline_message(task: int, reason: str) -> str:
    return (
        f"offline: {reason} (task {task} in the task list: {_task_list_status(task)}). Live control reaches only "
        "running sessions; hand work over durably instead — answer a parked owner question in its "
        "Slack thread, or queue work through `t3 <overlay> tasks create`.\n"
    )


def _render_rows(rows: list[WorkerSessionFacts], unreachable: dict[str, str], stream: IO[str]) -> None:
    print_table(
        ["Task", "Ticket", "Phase", "Harness", "State", "Steerable", "Elapsed s", "Tools", "Open tool", "Worker"],
        [
            [
                row["task"],
                row["ticket"],
                row["phase"],
                row["harness"],
                row["state"],
                "yes" if row["steerable"] else "no",
                row["elapsed_seconds"],
                row["tool_calls"],
                row["open_tool"] or "",
                row["worker"],
            ]
            for row in rows
        ],
        title="Live sessions",
        stream=stream,
    )
    for name, reason in unreachable.items():
        stream.write(f"unreachable worker {name}: {reason}\n")


def _render_fields(facts: Mapping[str, object], stream: IO[str]) -> None:
    for key, value in facts.items():
        stream.write(f"{key}: {value}\n")


class Command(TyperCommand):
    """Live sessions on this host's workers: ``list`` and ``inspect`` are passive, ``steer`` is active."""

    def _out(self) -> IO[str]:
        return cast("IO[str]", self.stdout)

    def _err(self) -> IO[str]:
        return cast("IO[str]", self.stderr)

    def _exit(self, message: str, code: int) -> NoReturn:
        self._err().write(message)
        raise SystemExit(code)

    @command(name="list")
    def list_sessions(self, *, json_output: _JsonOption = False) -> None:
        """List every live session on this host's workers (passive)."""
        self.print_result = False
        client = LiveClient()
        try:
            rows = client.sessions()
        except LiveOfflineError as exc:
            self._exit(f"offline: {exc}\n", EXIT_OFFLINE)
        except LiveRefusedError as exc:
            self._exit(f"rejected ({exc.code}): {exc}\n", EXIT_REJECTED)
        emit(
            rows,
            json_output=json_output,
            out=self._out(),
            err=self._err(),
            human=lambda stream: _render_rows(rows, client.unreachable, stream),
        )

    @command()
    def inspect(self, task: _TaskArgument, *, json_output: _JsonOption = False) -> None:
        """Show one live session's state, tool and progress without contacting the agent."""
        self.print_result = False
        self._err().write(f"{PASSIVE_LABEL}\n")
        try:
            facts = LiveClient().inspect(task)
        except LiveOfflineError as exc:
            self._exit(_offline_message(task, str(exc)), EXIT_OFFLINE)
        except LiveRefusedError as exc:
            self._exit(f"rejected ({exc.code}): {exc}\n", EXIT_REJECTED)
        emit(
            {"mode": "passive", **facts},
            json_output=json_output,
            out=self._out(),
            err=self._err(),
            human=lambda stream: _render_fields(facts, stream),
        )

    @command()
    def steer(
        self,
        task: _TaskArgument,
        *,
        text: Annotated[str, typer.Option(help="The input, at most 16 KiB, delivered into the running turn.")] = "",
        command_id: Annotated[
            str, typer.Option(help="Idempotency key; resending it with the same text returns the first receipt.")
        ] = "",
        wait: Annotated[
            float, typer.Option(help="Seconds to wait for the agent's next safe boundary (at most 900).")
        ] = DEFAULT_STEER_WAIT_SECONDS,
        json_output: _JsonOption = False,
    ) -> None:
        """Send input into a running agent's current turn and print its receipt (active)."""
        self.print_result = False
        if not text:
            self._exit("--text is required\n", 2)
        self._err().write(f"{ACTIVE_LABEL}\n")
        command_id = command_id or uuid.uuid4().hex
        try:
            receipt = LiveClient().steer(task, text, command_id=command_id, wait=wait)
        except LiveOfflineError as exc:
            self._exit(_offline_message(task, str(exc)), EXIT_OFFLINE)
        except LiveRefusedError as exc:
            receipt = self._local_receipt(task, command_id, "rejected", exc.code or "invalid_request")
            self._err().write(f"{exc}\n")
        except LiveDeliveryUnknownError as exc:
            receipt = self._local_receipt(task, command_id, "unknown_delivery", None)
            self._err().write(
                f"unknown_delivery for command {command_id}: {exc}. It may or may not have reached the agent; "
                f"never resend blindly — `live inspect {task}` shows whether the agent is still running.\n"
            )
        emit(
            receipt,
            json_output=json_output,
            out=self._out(),
            err=self._err(),
            human=lambda stream: _render_fields(receipt, stream),
        )
        code = _EXIT_BY_OUTCOME.get(receipt["outcome"], EXIT_UNKNOWN_DELIVERY)
        if code:
            raise SystemExit(code)

    @staticmethod
    def _local_receipt(task: int, command_id: str, outcome: str, code: str | None) -> ReceiptPayload:
        return {
            "command_id": command_id,
            "task": task,
            "outcome": outcome,
            "code": code,
            "mode": "active",
            "accepted_at": None,
        }
