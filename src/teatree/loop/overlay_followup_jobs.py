"""Bounded overlay follow-up hook selection and preview exclusion."""

import logging
from collections.abc import Generator, Iterable
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from threading import Lock, Thread

from teatree.core.backend_factory import OverlayBackends
from teatree.core.overlay import OverlayReview
from teatree.loop.job_identity import SWEEP_SCANNER_NAMES, _ScannerJob
from teatree.loop.scanners.base import Scanner, ScanSignal
from teatree.utils.thread_db import close_thread_db_connections

logger = logging.getLogger(__name__)
HOOK_DEADLINE_SECONDS = 5.0
_SKIPPED_HOOKS: ContextVar[list[str] | None] = ContextVar("skipped_overlay_followup_hooks", default=None)
_INFLIGHT_HOOKS: dict[str, Future[list[Scanner]]] = {}
_HOOK_LOCK = Lock()


def reset_inflight_hooks() -> None:
    with _HOOK_LOCK:
        _INFLIGHT_HOOKS.clear()


@contextmanager
def skip_overlay_followup_hooks() -> Generator[list[str]]:
    skipped: list[str] = []
    token = _SKIPPED_HOOKS.set(skipped)
    try:
        yield skipped
    finally:
        _SKIPPED_HOOKS.reset(token)


class _RefusedOverlayFollowupScanners:
    name = "overlay_followup_scanners"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def scan(self) -> list[ScanSignal]:
        raise OverlayHookRefusalError(self.reason)


class OverlayHookRefusalError(Exception):
    """A rejected hook result is reported as a scanner failure."""


class _HookCallError(Exception):
    """Keep the hook's original exception available across the selection thread."""

    def __init__(self, original: Exception) -> None:
        self.original = original


def _hook_result(backend: OverlayBackends, review: OverlayReview) -> list[Scanner]:
    with _HOOK_LOCK:
        future = _INFLIGHT_HOOKS.get(backend.name)
        if future is None or future.done():
            future = Future()
            _INFLIGHT_HOOKS[backend.name] = future
            context = copy_context()

            def invoke() -> None:
                try:
                    future.set_result(list(review.get_followup_scanners(backend)))
                except Exception as exc:
                    logger.exception("Overlay %s follow-up scanner hook raised", backend.name)
                    future.set_exception(_HookCallError(exc))
                finally:
                    close_thread_db_connections()
                    with _HOOK_LOCK:
                        if _INFLIGHT_HOOKS.get(backend.name) is future:
                            del _INFLIGHT_HOOKS[backend.name]

            Thread(target=context.run, args=(invoke,), name=f"followup-hook-{backend.name}", daemon=True).start()
    return future.result(timeout=HOOK_DEADLINE_SECONDS)


def _validated_scanner_jobs(scanners: list[Scanner], tag: str, reserved_names: Iterable[str]) -> list[_ScannerJob]:
    used_names = {*reserved_names, *SWEEP_SCANNER_NAMES, _RefusedOverlayFollowupScanners.name}
    jobs: list[_ScannerJob] = []
    refusals: list[str] = []
    for scanner in scanners:
        if isinstance(scanner, type) or not isinstance(scanner, Scanner) or not callable(scanner.scan):
            refusals.append(f"Non-Scanner item: {type(scanner).__name__}")
            continue
        name = scanner.name
        if not isinstance(name, str) or not name:
            refusals.append(f"Invalid scanner name: {name!r}")
        elif name in used_names:
            refusals.append(f"duplicate or reserved scanner name: {name}")
        else:
            jobs.append(_ScannerJob(scanner=scanner, overlay=tag, overlay_hook=True))
            used_names.add(name)
    if refusals:
        jobs.append(_ScannerJob(scanner=_RefusedOverlayFollowupScanners("; ".join(refusals)), overlay=tag))
    return jobs


def overlay_followup_jobs(backend: OverlayBackends, reserved_names: Iterable[str] = ()) -> list[_ScannerJob]:
    if backend.overlay is None:
        return []
    review = backend.overlay.review
    hook = getattr(type(review), "get_followup_scanners", None)
    if hook is None or hook is OverlayReview.get_followup_scanners:
        return []
    tag = backend.name
    if (skipped := _SKIPPED_HOOKS.get()) is not None:
        # A custom hook may write to the forge before it returns scanners.
        skipped.append(f"overlay_followup_scanners[{tag}]")
        return []

    try:
        scanners = _hook_result(backend, review)
    except TimeoutError:
        reason = f"hook timed out after {HOOK_DEADLINE_SECONDS}s"
        return [_ScannerJob(scanner=_RefusedOverlayFollowupScanners(reason), overlay=tag)]
    except _HookCallError as raised:
        exc = raised.original
        reason = f"{type(exc).__name__}: {exc}"
        return [_ScannerJob(scanner=_RefusedOverlayFollowupScanners(reason), overlay=tag)]

    return _validated_scanner_jobs(scanners, tag, reserved_names)
