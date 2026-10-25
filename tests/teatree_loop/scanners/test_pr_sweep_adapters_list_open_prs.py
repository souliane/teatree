"""Tests for :meth:`GhPrApiClient.list_open_prs` — the GitHub arm of the sweep's PR read.

The ``gh`` subprocess is doubled at ``run_allowed_to_fail`` by a fake that honours
``--limit`` the way ``gh pr list`` does, so a read capped below the open-PR count
comes back short exactly as it would against the forge. The GitLab arm already
refuses rather than report an empty or partial pass; these pin the same contract here.
"""

import json
from dataclasses import dataclass, field

import pytest

import teatree.backends.github.api as github_api
from teatree.loop.scanners.base import ScannerError, ScannerErrorClass
from teatree.loop.scanners.pr_sweep_adapters import OPEN_PR_CEILING, GhPrApiClient

SLUG = "souliane/teatree"


@dataclass(slots=True)
class _Completed:
    returncode: int
    stdout: str
    stderr: str = ""


@dataclass
class FakeGhPrList:
    """A ``gh pr list`` double serving *open_prs* up to the argv's ``--limit`` (gh's default 30)."""

    open_prs: int = 0
    raw_stdout: str | None = None
    argv_log: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: list[str], **_kw: object) -> _Completed:
        self.argv_log.append(list(argv))
        if self.raw_stdout is not None:
            return _Completed(0, self.raw_stdout)
        limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else 30
        rows = [
            {"number": n, "headRefOid": f"{n:040x}", "url": f"https://github.com/{SLUG}/pull/{n}"}
            for n in range(1, min(self.open_prs, limit) + 1)
        ]
        return _Completed(0, json.dumps(rows))


@pytest.fixture
def gh_pr_list(monkeypatch: pytest.MonkeyPatch) -> FakeGhPrList:
    fake = FakeGhPrList()
    monkeypatch.setattr(github_api, "run_allowed_to_fail", fake)
    return fake


class TestListOpenPrs:
    def test_reads_every_open_pr_past_a_hundred(self, gh_pr_list: FakeGhPrList) -> None:
        gh_pr_list.open_prs = 150

        prs = GhPrApiClient().list_open_prs(slug=SLUG)

        assert len(prs) == 150

    def test_refuses_when_open_prs_exceed_the_ceiling(self, gh_pr_list: FakeGhPrList) -> None:
        gh_pr_list.open_prs = OPEN_PR_CEILING + 1

        with pytest.raises(ScannerError) as excinfo:
            GhPrApiClient().list_open_prs(slug=SLUG)

        assert excinfo.value.error_class == ScannerErrorClass.UNKNOWN
        assert str(OPEN_PR_CEILING) in str(excinfo.value)

    @pytest.mark.parametrize("stdout", ["not json", '{"message": "Bad credentials"}', ""])
    def test_refuses_an_unreadable_listing(self, gh_pr_list: FakeGhPrList, stdout: str) -> None:
        gh_pr_list.raw_stdout = stdout

        with pytest.raises(ScannerError) as excinfo:
            GhPrApiClient().list_open_prs(slug=SLUG)

        assert excinfo.value.error_class == ScannerErrorClass.UNKNOWN
