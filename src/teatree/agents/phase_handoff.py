"""A fresh phase's predecessor result, rendered from the DB into a private file for one dispatch."""

import contextlib
import json
import logging
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TypedDict

from teatree.config import get_data_dir
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import Task

logger = logging.getLogger(__name__)

_HANDOFF_FILENAME = "handoff.json"
# The owner may create and enter the store but never list it, so a dispatch cannot enumerate its siblings.
_STORE_MODE = 0o300
_DELIVERY_MODE = 0o500
_HANDOFF_MODE = 0o400
_REMOVABLE_MODE = 0o700


class _HandoffPayload(TypedDict):
    task_id: int
    phase: str
    result_artifact_path: str
    attempt_id: int
    artifact_path: str
    result: object


@contextlib.contextmanager
def delivered_phase_handoff(task: Task) -> Iterator[Path | None]:
    """The handoff file for this dispatch of *task* — ``None`` unless it begins a new phase after a recorded attempt."""
    payload = _handoff_payload(task)
    if payload is None:
        yield None
        return
    store = get_data_dir("handoff")
    store.chmod(_STORE_MODE)
    delivery = Path(tempfile.mkdtemp(dir=store))
    try:
        handoff = delivery / _HANDOFF_FILENAME
        handoff.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        handoff.chmod(_HANDOFF_MODE)
        delivery.chmod(_DELIVERY_MODE)
        yield handoff
    finally:
        _remove(delivery)


def _remove(delivery: Path) -> None:
    try:
        delivery.chmod(_REMOVABLE_MODE)
        shutil.rmtree(delivery, onexc=_unlock_and_retry)
    except OSError:
        logger.exception("could not remove the handoff delivery %s", delivery)


def _unlock_and_retry(function: Callable[[str], object], path: str, _error: BaseException) -> None:
    for target in (Path(path).parent, Path(path)):
        with contextlib.suppress(OSError):
            target.chmod(_REMOVABLE_MODE)
    function(path)


def _handoff_payload(task: Task) -> _HandoffPayload | None:
    parent = task.parent_task
    # A handoff file exists to carry a phase's result ACROSS the boundary, so it stays a
    # phase question — the resume discriminator answers a different one.
    if parent is None or normalize_phase(parent.phase) == normalize_phase(task.phase):
        return None
    attempt = parent.attempts.order_by("-pk").first()
    if attempt is None:
        return None
    return _HandoffPayload(
        task_id=parent.pk,
        phase=parent.phase,
        result_artifact_path=parent.result_artifact_path,
        attempt_id=attempt.pk,
        artifact_path=attempt.artifact_path,
        result=attempt.result,
    )
