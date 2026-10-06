"""The broken site and the isolated worktree the real-browser ``t3 browser`` lane drives."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from e2e.browser.pom import BrowserCli
from e2e.browser.site import BrokenSite
from teatree.core.invocation_cwd import INVOCATION_CWD_ENV
from teatree.utils.run import run_checked


@pytest.fixture(scope="session")
def site() -> Iterator[BrokenSite]:
    server = BrokenSite()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[BrowserCli]:
    repo = (tmp_path / "repo").resolve()
    run_checked(["git", "init", "-q", str(repo)])
    monkeypatch.chdir(repo)
    monkeypatch.delenv(INVOCATION_CWD_ENV, raising=False)
    monkeypatch.setitem(os.environ, "T3_DATA_DIR", str(tmp_path / "data"))
    cli = BrowserCli(repo)
    yield cli
    cli.close()
