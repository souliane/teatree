"""Tests for ``GitHubCodeHost.create_pr`` (#3581).

The body flows through a unique per-invocation temp file the CLI owns
(``--body-file``), never a hand-named shared ``/tmp/pr-body.md`` two shippers can
race. Stub only the ``gh`` subprocess boundary (``_run_gh``) and the git remote
lookup; the real ``create_pr`` writes the temp body and passes ``--body-file``.
"""

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from teatree.backends.github import client as client_module
from teatree.backends.github.client import GitHubCodeHost
from teatree.core.backend_protocols import PullRequestSpec


def _spec(**overrides: object) -> PullRequestSpec:
    base: dict[str, object] = {
        "repo": "/tmp/repo",
        "branch": "3581-fix",
        "title": "fix(ship): own the pr-body temp file",
        "description": "fix(ship): own the pr-body temp file\n\n- body-file, not shared /tmp path\n",
    }
    base.update(overrides)
    return PullRequestSpec(**base)


class TestCreatePrBodyFile:
    def test_body_is_passed_via_body_file_never_inline_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}

        def _stub(*args: str, token: str = "", timeout: float | None = None) -> CompletedProcess[str]:
            del token, timeout
            argv = list(args)
            seen["argv"] = argv
            body_path = Path(argv[argv.index("--body-file") + 1])
            # The temp file exists AT CALL TIME (inside the context manager) and
            # holds the exact description — the CLI owns the path, not the caller.
            seen["body_at_call_time"] = body_path.read_text(encoding="utf-8")
            seen["path"] = body_path
            return CompletedProcess(args=argv, returncode=0, stdout="https://github.com/o/r/pull/7\n", stderr="")

        monkeypatch.setattr(client_module.git, "remote_slug", lambda repo: "o/r")
        monkeypatch.setattr(client_module, "_run_gh", _stub)

        spec = _spec()
        result = GitHubCodeHost(token="t").create_pr(spec)

        argv = seen["argv"]
        assert isinstance(argv, list)
        assert "--body-file" in argv
        assert "--body" not in argv
        assert seen["body_at_call_time"] == spec.description
        assert result == {"web_url": "https://github.com/o/r/pull/7"}

    def test_temp_body_file_is_cleaned_up_after_create(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Path] = {}

        def _stub(*args: str, token: str = "", timeout: float | None = None) -> CompletedProcess[str]:
            del token, timeout
            argv = list(args)
            captured["path"] = Path(argv[argv.index("--body-file") + 1])
            return CompletedProcess(args=argv, returncode=0, stdout="https://github.com/o/r/pull/8\n", stderr="")

        monkeypatch.setattr(client_module.git, "remote_slug", lambda repo: "o/r")
        monkeypatch.setattr(client_module, "_run_gh", _stub)

        GitHubCodeHost(token="t").create_pr(_spec())
        assert not captured["path"].exists()

    def test_optional_flags_compose_with_body_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, list[str]] = {}

        def _stub(*args: str, token: str = "", timeout: float | None = None) -> CompletedProcess[str]:
            del token, timeout
            seen["argv"] = list(args)
            return CompletedProcess(args=list(args), returncode=0, stdout="https://github.com/o/r/pull/9\n", stderr="")

        monkeypatch.setattr(client_module.git, "remote_slug", lambda repo: "o/r")
        monkeypatch.setattr(client_module, "_run_gh", _stub)

        GitHubCodeHost(token="t").create_pr(
            _spec(target_branch="develop", labels=["bug", "dx"], assignee="souliane", draft=True)
        )
        argv = seen["argv"]
        assert argv[argv.index("--base") + 1] == "develop"
        assert argv[argv.index("--label") + 1] == "bug,dx"
        assert argv[argv.index("--assignee") + 1] == "souliane"
        assert "--draft" in argv
        assert "--body-file" in argv
