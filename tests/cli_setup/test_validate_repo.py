"""Tests for ``validate_repo`` — t3 setup repo validation.

Lifted verbatim from the former monolithic ``tests/test_cli_setup.py``
(souliane/teatree#443). No behavior change: same assertions, only
relocated under a focused package by concern.
"""

from pathlib import Path
from unittest.mock import patch

import click
import pytest

from teatree.cli.setup.clone import validate_repo


class TestValidateRepo:
    def test_exits_when_no_repo(self) -> None:
        with patch("teatree.cli.setup.clone.DoctorService") as mock_svc:
            mock_svc.find_teatree_repo.return_value = None
            with pytest.raises(click.exceptions.Exit):
                validate_repo(None)

    def test_exits_when_no_apm_yml(self, tmp_path: Path) -> None:
        repo = tmp_path / "teatree"
        repo.mkdir()
        (repo / ".git").mkdir()
        with pytest.raises(click.exceptions.Exit):
            validate_repo(repo)

    def test_returns_repo_when_valid(self, tmp_path: Path) -> None:
        repo = tmp_path / "teatree"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "apm.yml").touch()
        assert validate_repo(repo) == repo
