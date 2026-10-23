"""GitHub backend runner credential boundary."""

from unittest.mock import Mock

import pytest

from teatree.backends.github import api


def test_unchecked_runner_refuses_empty_token(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock()
    monkeypatch.setattr(api, "run_allowed_to_fail", run)
    result = api.run_gh_unchecked(["pr", "list"], token="")
    assert result.returncode == 4
    run.assert_not_called()
