"""Shared test-infra helper: hold the t3-master slot the way the worker does (#3968).

The reactive-loop cycles run only while a live owner holds ``t3-master``, and the
production claimant is the ``t3 worker`` — so a test that drives one of those
cycles must stand in for the worker rather than patch the gate away. This wraps the
worker's own claim/release seams so the tests exercise the real lease semantics
(runner principal, pid anchor, ``loop_runner`` driver), not a stub of them.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from teatree.loops.worker import _claim_t3_master, _release_t3_master


@contextmanager
def worker_owns_t3_master() -> Iterator[None]:
    """Claim ``t3-master`` as the loop runner for the duration, then hand it back."""
    _claim_t3_master()
    try:
        yield
    finally:
        _release_t3_master()
