"""GitHub backend runner credential boundary."""

from unittest.mock import Mock

import pytest

from teatree.backends.github import api
from teatree.utils.run import CommandFailedError


def test_unchecked_runner_refuses_empty_token(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock()
    monkeypatch.setattr(api, "run_allowed_to_fail", run)
    result = api.run_gh_unchecked(["pr", "list"], token="")
    assert result.returncode == 4
    run.assert_not_called()


def test_is_not_found_on_http_404() -> None:
    assert api.is_not_found(CommandFailedError(["gh"], 1, "", "HTTP 404")) is True


def test_is_not_found_rejects_other_errors() -> None:
    assert api.is_not_found(CommandFailedError(["gh"], 1, "", "HTTP 500")) is False
