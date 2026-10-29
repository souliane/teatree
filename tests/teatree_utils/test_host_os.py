from pathlib import Path

import pytest

from teatree.utils.host_os import proc_reads_the_host, running_in_a_container


def test_declared_linux_host_allows_proc_even_in_container(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TEATREE_HOST_OS", "Linux")
    marker = tmp_path / "container"
    marker.touch()
    assert running_in_a_container((marker,))
    assert proc_reads_the_host((marker,))


def test_undeclared_container_and_darwin_do_not_claim_host_proc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / "container"
    marker.touch()
    monkeypatch.delenv("TEATREE_HOST_OS", raising=False)
    assert not proc_reads_the_host((marker,))
    monkeypatch.setenv("TEATREE_HOST_OS", "Darwin")
    assert not proc_reads_the_host(())
