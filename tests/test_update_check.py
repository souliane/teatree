"""``teatree.update_check`` — the "new release available" notice.

The ``gh`` release lookup is the only mocked boundary; the cache is a real file
under ``tmp_path`` via the module's own ``DATA_DIR``.
"""

import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from teatree.update_check import run_update_check


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr("teatree.update_check.DATA_DIR", data_dir)
    return data_dir


def _served_tag(monkeypatch: pytest.MonkeyPatch, tag: str) -> None:
    monkeypatch.setattr(
        "teatree.update_check.run_allowed_to_fail",
        lambda cmd, **_kwargs: CompletedProcess(cmd, 0, f"{tag}\n", ""),
    )


def _installed(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    monkeypatch.setattr("teatree.update_check.importlib.metadata.version", lambda _name: version)


class TestOnlyANewerReleaseIsAnnounced:
    def test_a_newer_tag_is_announced(self, cache_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _installed(monkeypatch, "0.4.0")
        _served_tag(monkeypatch, "v0.5.0")
        message = run_update_check(check_updates=True)
        assert message is not None
        assert "v0.5.0" in message

    def test_an_older_tag_is_not_announced(self, cache_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # An installation ahead of the latest release must not be told to "upgrade" to it.
        _installed(monkeypatch, "0.5.0")
        _served_tag(monkeypatch, "v0.4.0")
        assert run_update_check(check_updates=True) is None

    def test_the_same_tag_is_not_announced(self, cache_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _installed(monkeypatch, "0.4.0")
        _served_tag(monkeypatch, "v0.4.0")
        assert run_update_check(check_updates=True) is None

    def test_an_unrankable_installed_version_is_not_announced(
        self, cache_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _installed(monkeypatch, "0.5.0.dev3")
        _served_tag(monkeypatch, "v0.4.0")
        assert run_update_check(check_updates=True) is None

    def test_the_up_to_date_verdict_is_cached(self, cache_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _installed(monkeypatch, "0.5.0")
        _served_tag(monkeypatch, "v0.4.0")
        run_update_check(check_updates=True)
        cached = json.loads((cache_dir / "update-check.json").read_text(encoding="utf-8"))
        assert cached["message"] == ""
