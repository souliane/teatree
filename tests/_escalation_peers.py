"""A live coordinator session that takes every pushed message, for tests that deliver over the real mailbox."""

from teatree.agents.live_control import LiveTask, RejectCode, SteerRejectedError
from teatree.core.models import Task


class AcceptingSession:
    def __init__(self) -> None:
        self.delivered: list[str] = []
        self.reject = False

    async def steer(self, text: str, *, input_id: str, wait: float) -> None:
        del input_id, wait
        if self.reject:
            raise SteerRejectedError(RejectCode.NOT_ACCEPTED_IN_TIME)
        self.delivered.append(text)

    async def interrupt(self) -> None:
        return None


def live_task_of(task: Task, *, harness: str = "claude_sdk") -> LiveTask:
    return LiveTask(pk=task.pk, ticket=task.ticket_id, phase=task.phase, harness=harness, model="m")
