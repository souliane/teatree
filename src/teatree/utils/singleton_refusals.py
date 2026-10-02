"""The durable refusal-streak ledger for a restart-looping singleton (#3976).

A service that loses the singleton race exits, and its supervisor restarts it. Nothing
in the process survives to notice it is the Nth identical loss, so a race it can never
win presents as an ordinary restart cycle: every liveness surface reads healthy (the
flock genuinely IS held, the loops genuinely ARE ticking) and the only signal is a
restart counter nobody watches.

This ledger is that missing memory. It is keyed on the refusal's REASON — a fingerprint
carried by :class:`~teatree.utils.singleton.AlreadyRunningError`, deliberately excluding
the holder's pid — so a streak survives the holder restarting and resets the moment the
reason genuinely changes. One file per singleton beside its lock file, so a restart in a
fresh container reads the streak its predecessor wrote.

Only the worker keeps one: it is the singleton with a supervisor that restarts it
forever. The other singletons (``slack-listener``, ``slack-drain``, ``loop-tick``, the
per-service launch locks) refuse once into a foreground caller and have no loop to break.
"""

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from teatree.paths import DATA_DIR

#: Consecutive identical refusals that turn a restart cycle into a reported failure.
#: A drain-then-deploy hand-over can legitimately lose the race once or twice while the
#: outgoing worker releases the flock; three identical refusals cannot be a hand-over.
ESCALATION_THRESHOLD = 3


@dataclass(frozen=True)
class RefusalStreak:
    """How many times running the same refusal reason has been recorded."""

    fingerprint: str
    count: int

    @property
    def escalated(self) -> bool:
        return self.count >= ESCALATION_THRESHOLD


def default_refusal_path(name: str) -> Path:
    return DATA_DIR / f"{name}.refusals.json"


def _read(path: Path) -> RefusalStreak | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    fingerprint = payload.get("fingerprint")
    count = payload.get("count")
    if not isinstance(fingerprint, str) or not isinstance(count, int):
        return None
    return RefusalStreak(fingerprint=fingerprint, count=count)


def read_streak(name: str, *, path: Path | None = None) -> RefusalStreak | None:
    """The standing streak for ``name``, or ``None`` when there is none to read."""
    return _read(path or default_refusal_path(name))


def record_refusal(name: str, *, fingerprint: str, path: Path | None = None) -> RefusalStreak:
    """Record one refusal of ``name`` for ``fingerprint`` and return the resulting streak.

    Increments an identical reason and restarts the count on a different one, so the
    number always answers "how many times running THIS refusal", never "how many
    refusals ever". Written via replace so a reader never sees a half-written ledger.
    """
    resolved = path or default_refusal_path(name)
    standing = _read(resolved)
    count = standing.count + 1 if standing is not None and standing.fingerprint == fingerprint else 1
    streak = RefusalStreak(fingerprint=fingerprint, count=count)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{resolved.name}-", dir=resolved.parent)
    tmp_path = Path(tmp_name)
    try:
        os.write(fd, json.dumps({"fingerprint": fingerprint, "count": count}).encode("utf-8"))
        os.close(fd)
        tmp_path.replace(resolved)
    finally:
        tmp_path.unlink(missing_ok=True)
    return streak


def clear_refusals(name: str, *, path: Path | None = None) -> None:
    """Forget any standing streak for ``name`` — the acquire succeeded.

    Runs on the SUCCESS path, so a bookkeeping failure is swallowed: a worker that just
    took the singleton must never die because it could not delete a ledger file.
    """
    with contextlib.suppress(OSError):
        (path or default_refusal_path(name)).unlink(missing_ok=True)
