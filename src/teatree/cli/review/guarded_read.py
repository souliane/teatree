"""Fail-loud external reads for the review CLI (#3509).

Four sites in this package caught a bare ``Exception`` and returned a neutral value
with NO log, so "the read failed" and "there is nothing there" were indistinguishable
— the case ``/t3:rules`` § "External Read Failure Must Fail Loud" exists to forbid. In
three of them the neutral value was ALSO the permissive one, so a failed read reported
no finding, which reads as approval.

Two shapes, because the right answer differs per site:

* :func:`guarded_read` — the caller HAS a documented safe neutral (an empty author, a
    zero draft count) and deliberately proceeds on it, but the failure is LOGGED and the
    returned :class:`ReadOutcome` carries ``failed`` so the caller can branch on it. The
    orchestration-layer fail-open decision stays with the caller; only the silence goes.
* :func:`read_or_refuse` — there IS no safe neutral, so a failed read RAISES. Guessing
    is worse than refusing: the review base URL is the case that motivated this, where a
    silent fallback could point an outbound review post at a different GitLab instance.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class ReadRefusedError(RuntimeError):
    """An external read failed and there is no safe value to substitute."""


@dataclass(frozen=True, slots=True)
class ReadOutcome[T]:
    """One read's result, with the failure kept distinct from a genuine empty."""

    value: T
    failed: bool
    error: Exception | None = None


def guarded_read[T](what: str, read: Callable[[], T], *, neutral: T) -> ReadOutcome[T]:
    """Run *read*, degrading to *neutral* on failure — loudly, and distinguishably.

    *what* is a short human phrase naming the read ("mr author", "file diff"); it is
    what the operator greps for after the fact.
    """
    try:
        return ReadOutcome(value=read(), failed=False)
    except Exception as exc:  # noqa: BLE001 — the whole point: catch broadly, then report loudly.
        logger.warning("review: could not read %s (%s) — degrading to the neutral value", what, exc)
        return ReadOutcome(value=neutral, failed=True, error=exc)


def read_or_refuse[T](what: str, read: Callable[[], T]) -> T:
    """Run *read*, raising :class:`ReadRefusedError` on failure.

    For reads whose neutral value would be a GUESS with outbound consequences.
    """
    try:
        return read()
    except Exception as exc:
        msg = f"review: could not resolve {what} ({exc}) — refusing rather than guessing"
        raise ReadRefusedError(msg) from exc
