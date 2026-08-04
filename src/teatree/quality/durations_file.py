"""The one reader of ``dev/.test_durations`` (#4048).

Two checks judge that file — how much of the tree it knows about
(``durations_coverage``) and how much room its recordings leave against the
per-test ceiling (``timeout_headroom``). They share this reader so a file that
one of them calls unreadable cannot read as merely empty to the other.
"""

import json
from pathlib import Path

DURATIONS_PATH = Path("dev") / ".test_durations"


class DurationsUnreadableError(RuntimeError):
    """The durations file exists but does not parse — a read failure, not an empty file.

    Degrading this to "nothing recorded" would report a parse error as a coverage
    figure or as a clean headroom verdict, which is the one reading an operator
    cannot act on.
    """


def read_durations(path: Path) -> dict[str, float]:
    """Return the recorded ``node id -> seconds`` mapping; ``{}`` when the file is absent."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    # ValueError rather than json.JSONDecodeError: a non-UTF-8 file raises UnicodeDecodeError
    # out of read_text, which is neither that nor OSError. No caller guards this call and the
    # doctor has no global except, so an unwrapped escape took down the whole run.
    except (OSError, ValueError) as exc:
        message = f"{path} exists but could not be read as durations JSON: {exc}"
        raise DurationsUnreadableError(message) from exc
    if not isinstance(data, dict):
        message = f"{path} is not a durations mapping (got {type(data).__name__})"
        raise DurationsUnreadableError(message)
    try:
        return {str(key): float(value) for key, value in data.items()}
    except (TypeError, ValueError) as exc:
        message = f"{path} records a duration that is not a number: {exc}"
        raise DurationsUnreadableError(message) from exc
