"""GitHub backend runner credential boundary."""

from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest

from teatree.backends.github import api


def test_unchecked_runner_refuses_empty_token(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock()
    monkeypatch.setattr(api, "run_allowed_to_fail", run)
    result = api.run_gh_unchecked(["pr", "list"], token="")
    assert result.returncode == 4
    run.assert_not_called()


def test_post_accepts_empty_success_body(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock(return_value=CompletedProcess(["gh"], 0, stdout="", stderr=""))
    monkeypatch.setattr(api, "run_checked", run)
    assert api._gh_api_post("repos/example/repo/hooks/7/pings", {}, token="test-token") is None
    argv = run.call_args.args[0]
    assert argv[-2:] == ["--input", "-"]
    assert run.call_args.kwargs["stdin_text"] == "{}"


def test_delete_sends_no_body(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock(return_value=CompletedProcess(["gh"], 0, stdout="", stderr=""))
    monkeypatch.setattr(api, "run_checked", run)
    api._gh_api_delete("repos/example/repo/hooks/7", token="test-token")
    argv = run.call_args.args[0]
    assert argv[3:5] == ["--method", "DELETE"]
    assert "--input" not in argv
    assert "stdin_text" not in run.call_args.kwargs


@pytest.mark.parametrize(
    ("stdout", "complaint"),
    [
        ('{"message":"unexpected"}', "non-list page set"),
        ('[{"id":7}]', "non-list page"),
        ("[[1]]", "non-object hook"),
    ],
)
def test_strict_paginated_read_rejects_unreadable_response(
    monkeypatch: pytest.MonkeyPatch, stdout: str, complaint: str
) -> None:
    run = Mock(return_value=CompletedProcess(["gh"], 0, stdout=stdout, stderr=""))
    monkeypatch.setattr(api, "run_checked", run)
    with pytest.raises(ValueError, match=f"{complaint}$"):
        api.gh_api_get_paginated("repos/example/repo/hooks", token="test-token", strict=True)
