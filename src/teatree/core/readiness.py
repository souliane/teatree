"""Runtime readiness probes — verify a started worktree is actually serving.

A ``Probe`` wraps a ``check_fn`` that returns a ``ProbeResult``. Overlays
return a list of probes from ``OverlayBase.get_readiness_probes()``; the
``worktree ready`` and ``workspace ready`` CLI commands run them and exit
nonzero if any fails.

Two factory helpers cover the common cases — ``http_probe`` for HTTP
endpoints (with optional body and response-header assertions) and
``command_probe`` for shell-out checks. Overlays compose ad-hoc probes
inline by passing their own ``check_fn`` to ``Probe(...)`` directly.
"""

import shlex
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from teatree.utils.run import CommandFailedError, TimeoutExpired, run_allowed_to_fail

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Outcome of a single probe."""

    name: str
    passed: bool
    reason: str = ""
    evidence: str = ""

    def format(self) -> str:
        marker = "OK" if self.passed else "FAIL"
        body = self.reason or self.evidence
        return f"[{marker}] {self.name} — {body}" if body else f"[{marker}] {self.name}"


@dataclass(frozen=True, slots=True)
class Probe:
    """Runtime readiness probe.

    ``check_fn`` returns a ``ProbeResult``. ``Probe.check()`` calls it and
    converts any uncaught exception into a failed result so probe authors
    don't have to wrap each ``check_fn`` body in try/except.
    """

    name: str
    description: str
    check_fn: "Callable[[], ProbeResult]"

    def check(self) -> ProbeResult:
        try:
            return self.check_fn()
        except Exception as exc:  # noqa: BLE001 — probe authors shouldn't have to wrap
            return ProbeResult(
                name=self.name,
                passed=False,
                reason=f"{type(exc).__name__}: {exc}",
            )


@dataclass(frozen=True, slots=True)
class HTTPProbeSpec:
    """Configuration for an ``http_probe``.

    ``response_header_equals`` enables CORS round-trip assertions
    (``Access-Control-Allow-Origin`` reflected with the configured origin).
    """

    url: str
    expected_status: int = 200
    body_contains: str = ""
    response_header_equals: "Mapping[str, str] | None" = None
    request_headers: "Mapping[str, str] | None" = None
    timeout_seconds: float = 5.0


@dataclass(frozen=True, slots=True)
class CommandProbeSpec:
    """Configuration for a ``command_probe``."""

    args: list[str] = field(default_factory=list)
    expected_exit_code: int = 0
    stdout_contains: str = ""
    timeout_seconds: float = 10.0
    env: "Mapping[str, str] | None" = None
    cwd: str = ""


def http_probe(*, name: str, description: str, spec: HTTPProbeSpec) -> Probe:
    """Build a probe that GETs ``spec.url`` and asserts the response."""

    def _check() -> ProbeResult:
        return _check_http(name, spec)

    return Probe(name=name, description=description, check_fn=_check)


def command_probe(*, name: str, description: str, spec: CommandProbeSpec) -> Probe:
    """Build a probe that runs ``spec.args`` and asserts exit code (and optional stdout)."""

    def _check() -> ProbeResult:
        return _check_command(name, spec)

    return Probe(name=name, description=description, check_fn=_check)


def run_probes(probes: "Iterable[Probe]", *, max_workers: int = 8) -> list[ProbeResult]:
    """Run probes concurrently. Result order matches input order."""
    probes_list = list(probes)
    if not probes_list:
        return []
    results: dict[int, ProbeResult] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(p.check): i for i, p in enumerate(probes_list)}
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()
    return [results[i] for i in range(len(probes_list))]


@dataclass(frozen=True, slots=True)
class ProbeRunSummary:
    """Aggregate counts after running a probe batch."""

    total: int
    failures: int


def run_and_report_probes(
    probes: "Iterable[Probe]",
    *,
    write_line: "Callable[[str], None]",
    indent: str = "",
) -> ProbeRunSummary:
    """Run probes, format each result via ``write_line``, return a summary.

    Empty input returns ``ProbeRunSummary(0, 0)`` and writes nothing — the
    caller decides whether to print a "no probes" line or stay silent. The
    failure-count message and ``SystemExit`` belong to the caller too;
    different commands word the message and exit-condition differently.
    """
    results = run_probes(probes)
    for result in results:
        write_line(f"{indent}{result.format()}")
    failures = sum(1 for r in results if not r.passed)
    return ProbeRunSummary(total=len(results), failures=failures)


def _check_http(name: str, spec: HTTPProbeSpec) -> ProbeResult:
    evidence = f"GET {spec.url}"
    headers = dict(spec.request_headers or {})
    expected_headers = dict(spec.response_header_equals or {})
    try:
        response = httpx.get(spec.url, headers=headers, timeout=spec.timeout_seconds)
    except httpx.HTTPError as exc:
        return ProbeResult(
            name=name,
            passed=False,
            reason=f"{type(exc).__name__}: {exc}",
            evidence=evidence,
        )
    status = response.status_code
    response_headers = response.headers  # httpx.Headers — case-insensitive lookup
    body = response.text

    if status != spec.expected_status:
        return ProbeResult(
            name=name,
            passed=False,
            reason=f"got status {status}, expected {spec.expected_status}",
            evidence=evidence,
        )
    if spec.body_contains and spec.body_contains not in body:
        return ProbeResult(
            name=name,
            passed=False,
            reason=f"body does not contain {spec.body_contains!r}",
            evidence=evidence,
        )
    for header, expected in expected_headers.items():
        actual = response_headers.get(header)
        if actual != expected:
            return ProbeResult(
                name=name,
                passed=False,
                reason=f"{header}={actual!r}, expected {expected!r}",
                evidence=evidence,
            )
    return ProbeResult(
        name=name,
        passed=True,
        reason=f"status {status}",
        evidence=evidence,
    )


def _check_command(name: str, spec: CommandProbeSpec) -> ProbeResult:
    if not spec.args:
        return ProbeResult(name=name, passed=False, reason="no command args")
    evidence = shlex.join(spec.args)
    env = dict(spec.env) if spec.env is not None else None
    try:
        result = run_allowed_to_fail(
            spec.args,
            expected_codes=None,
            env=env,
            cwd=spec.cwd or None,
            timeout=spec.timeout_seconds,
        )
    except TimeoutExpired:
        return ProbeResult(
            name=name,
            passed=False,
            reason=f"timed out after {spec.timeout_seconds}s",
            evidence=evidence,
        )
    except (CommandFailedError, OSError, ValueError) as exc:
        return ProbeResult(
            name=name,
            passed=False,
            reason=f"{type(exc).__name__}: {exc}",
            evidence=evidence,
        )
    if result.returncode != spec.expected_exit_code:
        return ProbeResult(
            name=name,
            passed=False,
            reason=f"exit {result.returncode}, expected {spec.expected_exit_code}",
            evidence=evidence,
        )
    if spec.stdout_contains and spec.stdout_contains not in result.stdout:
        return ProbeResult(
            name=name,
            passed=False,
            reason=f"stdout does not contain {spec.stdout_contains!r}",
            evidence=evidence,
        )
    return ProbeResult(
        name=name,
        passed=True,
        reason=f"exit {result.returncode}",
        evidence=evidence,
    )


__all__ = [
    "CommandProbeSpec",
    "HTTPProbeSpec",
    "Probe",
    "ProbeResult",
    "ProbeRunSummary",
    "command_probe",
    "http_probe",
    "run_and_report_probes",
    "run_probes",
]
