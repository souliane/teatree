"""An answered question handed back to the session that asked it, through the host-visible data dir.

The worker records the answer inside its container while the asking session's hooks run on the
host, and the bind-mounted primary data dir is the one place both can reach. One file per
answered question: posted atomically, claimed by rename, so it is delivered at most once.
"""

import errno
import json
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict

from teatree.paths import ControlDb

MAILBOX_DIRNAME = "answer-handback"

logger = logging.getLogger(__name__)


class HandedBackAnswer(TypedDict):
    id: int
    answer: str


def mailbox(session_id: str, *, env: Mapping[str, str] = os.environ) -> Path | None:
    """The session's mailbox dir, or ``None`` for an id that is not a single path segment."""
    if not session_id or session_id.startswith(".") or Path(session_id).name != session_id:
        return None
    return ControlDb(env).primary_data_dir() / MAILBOX_DIRNAME / session_id


def post(*, session_id: str, question_id: int, answer: str, env: Mapping[str, str] = os.environ) -> Path:
    """Write the answer into the asking session's mailbox; raises ``ValueError`` for an unusable id."""
    box = mailbox(session_id, env=env)
    if box is None:
        msg = f"session id {session_id!r} names no mailbox"
        raise ValueError(msg)
    box.mkdir(parents=True, exist_ok=True)
    try:
        handle, staged = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=box)
    except FileNotFoundError:
        # A second collector emptying the box again inside this retry drops the post; the answer stays in the DB.
        box.mkdir(parents=True, exist_ok=True)
        handle, staged = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=box)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump({"id": question_id, "answer": answer}, stream)
    target = box / f"{question_id}.json"
    Path(staged).replace(target)
    return target


def _claimed(posted: Path) -> HandedBackAnswer | None:
    claimed = posted.with_name(f".{posted.stem}.{os.getpid()}.claimed")
    try:
        posted.rename(claimed)
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("Could not claim the handed-back answer %s", posted, exc_info=True)
        return None
    try:
        payload = json.loads(claimed.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Could not read the handed-back answer %s", claimed, exc_info=True)
        return None
    finally:
        claimed.unlink(missing_ok=True)
    if isinstance(payload, dict) and isinstance(payload.get("id"), int) and isinstance(payload.get("answer"), str):
        return HandedBackAnswer(id=payload["id"], answer=payload["answer"])
    logger.warning("Dropped a malformed handed-back answer %s", posted)
    return None


def collect(session_id: str, *, env: Mapping[str, str] = os.environ) -> list[HandedBackAnswer]:
    """Claim and return every answer waiting for the session, oldest question first."""
    box = mailbox(session_id, env=env)
    if box is None:
        return []
    try:
        posted = [path for path in box.iterdir() if path.suffix == ".json" and not path.name.startswith(".")]
    except FileNotFoundError:
        return []
    except OSError:
        logger.warning("Could not list the answer mailbox %s", box, exc_info=True)
        return []
    claimed = (_claimed(path) for path in sorted(posted, key=lambda path: path.stem.zfill(20)))
    answers = [answer for answer in claimed if answer is not None]
    _remove_if_empty(box)
    return answers


def _remove_if_empty(box: Path) -> None:
    try:
        box.rmdir()
    except OSError as exc:
        if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT}:
            logger.warning("Could not remove the empty answer mailbox %s", box, exc_info=True)


__all__ = ["MAILBOX_DIRNAME", "HandedBackAnswer", "collect", "mailbox", "post"]
