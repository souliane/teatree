"""The paging ``gh api …/check-runs`` double the loop's commit-scoped CI probes share.

The double sits at the ``run_allowed_to_fail`` subprocess boundary — below every
probe's own argv builder — and pages the way GitHub itself does: ``per_page``
comes off the endpoint's query (30 when absent, GitHub's default), and only page
1 is served unless the argv asks for every page. A probe that omits
``--paginate``, or leaves ``per_page`` at the default, therefore reads a check
landing later as absent, which is the truncation
:mod:`teatree.loop.main_check_runs` exists to close.

``--jq`` is deliberately not modelled: ``gh`` refuses it alongside ``--slurp``, so
every probe asserts its absence and classifies in Python over the parsed runs.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from types import ModuleType
from urllib.parse import parse_qs, urlparse

import pytest

#: GitHub's own page size when the request names none.
GITHUB_DEFAULT_PER_PAGE = 30


def check_run(name: str, status: str = "completed", conclusion: str = "success") -> dict[str, str]:
    """One ``check_runs[]`` entry."""
    return {"name": name, "status": status, "conclusion": conclusion}


@dataclass(slots=True)
class _Completed:
    returncode: int
    stdout: str
    stderr: str = ""


@dataclass
class FakeGhCheckRuns:
    """A ``gh api`` double answering the check-runs endpoint from *runs*.

    ``runs_served`` / ``pages_served`` record what the last call's argv actually
    reached, so a test can assert the read was not truncated rather than infer it
    from the answer.
    """

    runs: list[dict[str, str]] = field(default_factory=list)
    returncode: int = 0
    raw_stdout: str | None = None
    argv_log: list[list[str]] = field(default_factory=list)
    env_log: list[dict[str, str] | None] = field(default_factory=list)
    runs_served: int = 0
    pages_served: int = 0

    def __call__(self, argv: list[str], *, env: dict[str, str] | None = None, **_kw: object) -> _Completed:
        self.argv_log.append(list(argv))
        self.env_log.append(env)
        if self.returncode != 0:
            return _Completed(self.returncode, "", "gh failed")
        if self.raw_stdout is not None:
            return _Completed(0, self.raw_stdout)
        pages = self._visible_pages(argv)
        self.pages_served = len(pages)
        self.runs_served = sum(len(page) for page in pages)
        if "--slurp" in argv:
            return _Completed(0, json.dumps([self._body(page) for page in pages]))
        return _Completed(0, json.dumps(self._body(pages[0])))

    def _body(self, page: list[dict[str, str]]) -> dict[str, object]:
        return {"total_count": len(self.runs), "check_runs": page}

    def _visible_pages(self, argv: list[str]) -> list[list[dict[str, str]]]:
        size = _per_page(argv)
        pages = [self.runs[i : i + size] for i in range(0, len(self.runs), size)] or [[]]
        return pages if "--paginate" in argv else pages[:1]


def _per_page(argv: list[str]) -> int:
    endpoint = next((arg for arg in argv if "check-runs" in arg), "")
    requested = parse_qs(urlparse(endpoint).query).get("per_page")
    return int(requested[0]) if requested else GITHUB_DEFAULT_PER_PAGE


@pytest.fixture
def gh_check_runs(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeGhCheckRuns]:
    """Install :class:`FakeGhCheckRuns` over a probe module's ``run_allowed_to_fail``."""

    def _install(
        module: ModuleType,
        *,
        runs: list[dict[str, str]] | None = None,
        returncode: int = 0,
        raw_stdout: str | None = None,
    ) -> FakeGhCheckRuns:
        fake = FakeGhCheckRuns(runs=list(runs or []), returncode=returncode, raw_stdout=raw_stdout)
        monkeypatch.setattr(module, "run_allowed_to_fail", fake)
        return fake

    return _install
