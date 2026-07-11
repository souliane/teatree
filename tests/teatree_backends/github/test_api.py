"""Direct anti-vacuous parser tests for :func:`gh_can_push`.

``gh_can_push`` is security-critical: :func:`teatree.backends.loader.get_code_host_for_repo`
overrides the configured PR-authoring token ONLY when the probe returns a
CERTAIN ``True``, so a parser that silently returned ``None`` (drop the
collaborator override) or ``True`` (run ``gh pr create`` under the wrong
identity) on an ambiguous payload would be a real security regression. Every
existing caller monkeypatches the whole function at the loader boundary, so the
real ``stdout -> bool | None`` parse is never exercised. These tests drive the
REAL function and stub only the ``gh`` subprocess boundary (``_run_gh``), so
each parse branch is genuinely covered rather than mocked away.
"""

from subprocess import CompletedProcess

import pytest

from teatree.backends.github import api
from teatree.utils.run import CommandFailedError


def _stub_run_gh(stdout: str) -> "object":
    """A ``_run_gh`` replacement that returns *stdout* from a zero-exit ``gh`` call."""

    def _run(*args: str, token: str = "", timeout: float | None = None) -> CompletedProcess[str]:
        return CompletedProcess(args=list(args), returncode=0, stdout=stdout, stderr="")

    return _run


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("true\n", True),  # trailing newline proves .strip() runs on real gh output
        ("false\n", False),
        ("null\n", None),  # .permissions absent → jq prints "null"
        ("", None),  # empty stdout
        ("maybe", None),  # non-bool payload
    ],
)
def test_gh_can_push_parses_permission_payload(
    monkeypatch: pytest.MonkeyPatch, payload: str, *, expected: bool | None
) -> None:
    monkeypatch.setattr(api, "_run_gh", _stub_run_gh(payload))
    assert api.gh_can_push("owner/repo") is expected


def test_gh_can_push_returns_none_when_the_probe_command_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed ``gh`` call (non-zero exit → CommandFailedError) is an uncertain answer, not push access."""

    def _raise(*args: str, token: str = "", timeout: float | None = None) -> CompletedProcess[str]:
        raise CommandFailedError(list(args), 1, "", "HTTP 404: Not Found")

    monkeypatch.setattr(api, "_run_gh", _raise)
    assert api.gh_can_push("owner/repo") is None


def test_gh_can_push_returns_none_on_empty_slug_without_probing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty slug short-circuits to None BEFORE any subprocess runs."""
    probed = False

    def _spy(*args: str, token: str = "", timeout: float | None = None) -> CompletedProcess[str]:
        nonlocal probed
        probed = True
        return CompletedProcess(args=list(args), returncode=0, stdout="true", stderr="")

    monkeypatch.setattr(api, "_run_gh", _spy)
    assert api.gh_can_push("") is None
    assert probed is False
