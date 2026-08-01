"""Tests for ``teatree.backends.github.pr_create.create_pr`` (#3581, #3842).

The body flows through a unique per-invocation temp file the CLI owns
(``--body-file``), never a hand-named shared ``/tmp/pr-body.md`` two shippers can
race. Stub only the ``gh`` subprocess boundary (``_run_gh``) and the git remote
lookup; the real ``create_pr`` writes the temp body and passes ``--body-file``.

A redundant create — the no-orphan push-gate hook already opened a PR for this
branch before this call runs — adopts the discovered PR instead of failing the ship.
"""

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from teatree.backends.github import pr_create as pr_create_module
from teatree.backends.github.pr_create import create_pr
from teatree.core.backend_protocols import PullRequestSpec
from teatree.core.forge_pr_probe import PrProbe
from teatree.utils.run import CommandFailedError


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

        monkeypatch.setattr(pr_create_module.git, "remote_slug", lambda repo: "o/r")
        monkeypatch.setattr(pr_create_module, "_run_gh", _stub)

        spec = _spec()
        result = create_pr(spec, token="t")

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

        monkeypatch.setattr(pr_create_module.git, "remote_slug", lambda repo: "o/r")
        monkeypatch.setattr(pr_create_module, "_run_gh", _stub)

        create_pr(_spec(), token="t")
        assert not captured["path"].exists()

    def test_optional_flags_compose_with_body_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, list[str]] = {}

        def _stub(*args: str, token: str = "", timeout: float | None = None) -> CompletedProcess[str]:
            del token, timeout
            seen["argv"] = list(args)
            return CompletedProcess(args=list(args), returncode=0, stdout="https://github.com/o/r/pull/9\n", stderr="")

        monkeypatch.setattr(pr_create_module.git, "remote_slug", lambda repo: "o/r")
        monkeypatch.setattr(pr_create_module, "_run_gh", _stub)

        create_pr(_spec(target_branch="develop", labels=["bug", "dx"], assignee="souliane", draft=True), token="t")
        argv = seen["argv"]
        assert argv[argv.index("--base") + 1] == "develop"
        assert argv[argv.index("--label") + 1] == "bug,dx"
        assert argv[argv.index("--assignee") + 1] == "souliane"
        assert "--draft" in argv
        assert "--body-file" in argv


class TestCreatePrAdoptsAnAlreadyExistingPr:
    """``gh pr create`` fails when the no-orphan push-gate already opened a PR for the branch.

    A ticket ship must not fail permanently on that race: the branch is real, the PR is
    real, only the create call is redundant. Adopting the discovered URL matches the
    idempotency the sibling comment on ``execute_ship`` already documents for a
    redelivered job re-finding its OWN recorded PR — this is the same shape for a PR this
    ticket never recorded because a DIFFERENT actor (the push-gate) created it first.
    """

    def test_a_redundant_create_adopts_the_discovered_open_pr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _stub(*args: str, token: str = "", timeout: float | None = None) -> CompletedProcess[str]:
            del token, timeout
            raise CommandFailedError(
                list(args),
                1,
                "",
                'a pull request for branch "3581-fix" into branch "main" already exists:\n'
                "https://github.com/o/r/pull/42",
            )

        monkeypatch.setattr(pr_create_module.git, "remote_slug", lambda repo: "o/r")
        monkeypatch.setattr(pr_create_module, "_run_gh", _stub)
        monkeypatch.setattr(
            pr_create_module,
            "find_open_pr_for_branch",
            lambda repo_dir, branch: PrProbe.found("https://github.com/o/r/pull/42"),
        )

        result = create_pr(_spec(), token="t")

        assert result == {"web_url": "https://github.com/o/r/pull/42"}

    def test_a_genuine_failure_still_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-'already exists' failure (auth, network, rate limit) must not be swallowed."""

        def _stub(*args: str, token: str = "", timeout: float | None = None) -> CompletedProcess[str]:
            del token, timeout
            raise CommandFailedError(list(args), 1, "", "gh: authentication required")

        monkeypatch.setattr(pr_create_module.git, "remote_slug", lambda repo: "o/r")
        monkeypatch.setattr(pr_create_module, "_run_gh", _stub)

        with pytest.raises(CommandFailedError, match="authentication required"):
            create_pr(_spec(), token="t")

    def test_already_exists_but_the_probe_cannot_confirm_still_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail-closed: an UNKNOWN probe must not be read as 'safe to proceed' (mirrors the probe's own doctrine)."""

        def _stub(*args: str, token: str = "", timeout: float | None = None) -> CompletedProcess[str]:
            del token, timeout
            raise CommandFailedError(
                list(args),
                1,
                "",
                'a pull request for branch "3581-fix" into branch "main" already exists:\n'
                "https://github.com/o/r/pull/42",
            )

        monkeypatch.setattr(pr_create_module.git, "remote_slug", lambda repo: "o/r")
        monkeypatch.setattr(pr_create_module, "_run_gh", _stub)
        monkeypatch.setattr(pr_create_module, "find_open_pr_for_branch", lambda repo_dir, branch: PrProbe.unknown())

        with pytest.raises(CommandFailedError, match="already exists"):
            create_pr(_spec(), token="t")


class TestCreatePrUrlAndSlugHandling:
    def test_raises_when_gh_returns_no_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """#1226: an empty/non-URL ``gh pr create`` stdout must surface as a failure.

        ``gh pr create`` can exit 0 while printing a non-URL line (e.g. the
        "no commits between" pre-push race). The producer MUST refuse to claim success
        with an empty URL; the ship runner relies on this invariant to flip ``ok=False``
        instead of advancing the FSM with an empty ``pr_urls`` entry.
        """

        def _stub(*args: str, token: str = "", timeout: float | None = None) -> CompletedProcess[str]:
            del token, timeout
            return CompletedProcess(args=list(args), returncode=0, stdout="\n", stderr="")

        monkeypatch.setattr(pr_create_module.git, "remote_slug", lambda repo: "org/repo")
        monkeypatch.setattr(pr_create_module, "_run_gh", _stub)

        with pytest.raises(CommandFailedError):
            create_pr(_spec(repo="org/repo"), token="tok")

    def test_resolves_a_local_path_to_the_owner_repo_slug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``gh pr create --repo`` requires ``owner/repo`` — local paths must be resolved first."""
        seen: dict[str, list[str]] = {}

        def _stub(*args: str, token: str = "", timeout: float | None = None) -> CompletedProcess[str]:
            del token, timeout
            seen["argv"] = list(args)
            return CompletedProcess(
                args=list(args), returncode=0, stdout="https://github.com/souliane/teatree/pull/3\n", stderr=""
            )

        calls: list[str] = []

        def _slug(repo: str) -> str:
            calls.append(repo)
            return "souliane/teatree"

        monkeypatch.setattr(pr_create_module.git, "remote_slug", _slug)
        monkeypatch.setattr(pr_create_module, "_run_gh", _stub)

        create_pr(_spec(repo="/tmp/workspace/ticket/teatree"), token="")

        assert calls == ["/tmp/workspace/ticket/teatree"]
        argv = seen["argv"]
        assert argv[argv.index("--repo") + 1] == "souliane/teatree"

    def test_passes_through_an_existing_slug_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the caller already provides ``owner/repo``, no resolution is needed."""
        seen: dict[str, list[str]] = {}

        def _stub(*args: str, token: str = "", timeout: float | None = None) -> CompletedProcess[str]:
            del token, timeout
            seen["argv"] = list(args)
            return CompletedProcess(
                args=list(args), returncode=0, stdout="https://github.com/org/repo/pull/4\n", stderr=""
            )

        monkeypatch.setattr(pr_create_module.git, "remote_slug", lambda repo: repo)
        monkeypatch.setattr(pr_create_module, "_run_gh", _stub)

        create_pr(_spec(repo="org/repo"), token="")

        argv = seen["argv"]
        assert argv[argv.index("--repo") + 1] == "org/repo"
