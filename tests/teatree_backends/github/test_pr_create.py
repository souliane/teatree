"""Tests for ``teatree.backends.github.pr_create.create_pr`` (#3581, #3842).

The body flows through a unique per-invocation temp file the CLI owns
(``--body-file``), never a hand-named shared ``/tmp/pr-body.md`` two shippers can
race. Stub only the ``gh`` subprocess boundary (``_run_gh``) and the git remote
lookup; the real ``create_pr`` writes the temp body and passes ``--body-file``.

A redundant create — the no-orphan push-gate hook already opened a PR for this
branch before this call runs — adopts the discovered PR instead of failing the ship,
and takes ownership of its BODY too rather than shipping the hook's placeholder (#3991).
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


_ADOPTED_URL = "https://github.com/o/r/pull/42"

_PLACEHOLDER_BODY = (
    "fix(x): thing\n\n## What\n- thing\n\n## Why\n"
    "TODO — opened automatically by the no-orphan pre-push hook, which sees only "
    "the commit. Replace this line with the rationale before requesting review."
)


class _GhPrStub:
    """A ``gh`` stand-in whose PR body is real state: ``create`` loses the race, ``view``/``edit`` act on it.

    ``body_after_edit`` models the forge accepting the edit without it landing — the
    exact-0-exit case verify-by-re-read exists to catch.
    """

    def __init__(self, body: str, *, edit_fails: bool = False, body_after_edit: str | None = None) -> None:
        self.body = body
        self.calls: list[list[str]] = []
        self._edit_fails = edit_fails
        self._body_after_edit = body_after_edit

    def __call__(self, *args: str, token: str = "", timeout: float | None = None) -> CompletedProcess[str]:
        del token, timeout
        argv = list(args)
        self.calls.append(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            return CompletedProcess(args=argv, returncode=0, stdout=self.body, stderr="")
        if argv[:3] == ["gh", "pr", "edit"]:
            return self._edit(argv)
        raise CommandFailedError(
            argv,
            1,
            "",
            f'a pull request for branch "3581-fix" into branch "main" already exists:\n{_ADOPTED_URL}',
        )

    def _edit(self, argv: list[str]) -> CompletedProcess[str]:
        if self._edit_fails:
            raise CommandFailedError(argv, 1, "", "gh: could not update pull request")
        written = Path(argv[argv.index("--body-file") + 1]).read_text(encoding="utf-8")
        self.body = written if self._body_after_edit is None else self._body_after_edit
        return CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    def argv_for(self, *verb: str) -> list[list[str]]:
        return [argv for argv in self.calls if argv[: len(verb)] == list(verb)]


class TestAdoptedPrBodyReplacesTheHookPlaceholder:
    """Adoption takes ownership of the PR's BODY too, not only its URL (#3991).

    The no-orphan hook opens the PR from the commit alone, so its ``## Why`` is a
    placeholder. Adopting the URL and stopping there ships the change with no rationale
    — the reviewer cannot tell "no rationale" from "rationale written elsewhere".
    """

    def _adopting(self, monkeypatch: pytest.MonkeyPatch, stub: object) -> None:
        monkeypatch.setattr(pr_create_module.git, "remote_slug", lambda repo: "o/r")
        monkeypatch.setattr(pr_create_module, "_run_gh", stub)
        monkeypatch.setattr(
            pr_create_module, "find_open_pr_for_branch", lambda repo_dir, branch: PrProbe.found(_ADOPTED_URL)
        )

    def test_the_placeholder_body_is_replaced_with_the_ship_description(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _GhPrStub(_PLACEHOLDER_BODY)
        self._adopting(monkeypatch, stub)

        spec = _spec()
        result = create_pr(spec, token="t")

        assert result == {"web_url": _ADOPTED_URL}
        assert stub.body == spec.description
        edits = stub.argv_for("gh", "pr", "edit")
        assert len(edits) == 1
        assert "--body-file" in edits[0]
        assert "--body" not in edits[0]

    def test_two_empty_headings_are_replaced_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The second observed shape carries no marker string to grep for."""
        stub = _GhPrStub("fix(x): thing\n\n## What\n\n## Why\n")
        self._adopting(monkeypatch, stub)

        spec = _spec()
        create_pr(spec, token="t")

        assert stub.body == spec.description

    def test_an_authored_body_is_never_overwritten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Adoption also covers a redelivered ship re-finding its OWN full-bodied PR."""
        authored = "fix(x): thing\n\n## What\n- thing\n\n## Why\nThe hand-written rationale."
        stub = _GhPrStub(authored)
        self._adopting(monkeypatch, stub)

        create_pr(_spec(), token="t")

        assert stub.body == authored
        assert stub.argv_for("gh", "pr", "edit") == []

    def test_an_unwritable_placeholder_fails_the_ship(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A KNOWN placeholder we could not clear is a ship failure, not a silent adopt."""
        self._adopting(monkeypatch, _GhPrStub(_PLACEHOLDER_BODY, edit_fails=True))

        with pytest.raises(CommandFailedError, match="placeholder"):
            create_pr(_spec(), token="t")

    def test_an_unconfirmed_write_fails_the_ship(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify-by-re-read: an edit that exits 0 without landing must not report success."""
        self._adopting(monkeypatch, _GhPrStub(_PLACEHOLDER_BODY, body_after_edit=_PLACEHOLDER_BODY))

        with pytest.raises(CommandFailedError, match="placeholder"):
            create_pr(_spec(), token="t")

    def test_a_thin_ship_description_still_adopts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``ensure_standard_body`` appends a BARE ``## Why`` when the commit has none.

        Verification asks whether the write LANDED, not whether the replacement reads
        well — otherwise a ship whose own body is thin fails on a write that worked.
        """
        stub = _GhPrStub(_PLACEHOLDER_BODY)
        self._adopting(monkeypatch, stub)

        thin = "fix(x): thing\n\n## What\n\n## Why"
        assert create_pr(_spec(description=thin), token="t") == {"web_url": _ADOPTED_URL}
        assert stub.body == thin

    def test_a_body_already_equal_to_the_ship_description_still_confirms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify-by-re-read must not treat a landed idempotent write as unconfirmed.

        A redelivered ship (or a losing concurrent hook) writes back byte-identical
        content — the write lands as a no-op (#3991).
        """
        thin = "fix(x): thing\n\n## What\n\n## Why"
        stub = _GhPrStub(thin)
        self._adopting(monkeypatch, stub)

        assert create_pr(_spec(description=thin), token="t") == {"web_url": _ADOPTED_URL}
        assert stub.body == thin

    def test_an_unreadable_body_still_adopts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cannot tell placeholder from authored → leave it, never blind-overwrite."""

        def _stub(*args: str, token: str = "", timeout: float | None = None) -> CompletedProcess[str]:
            del token, timeout
            raise CommandFailedError(list(args), 1, "", f"already exists:\n{_ADOPTED_URL}")

        self._adopting(monkeypatch, _stub)

        assert create_pr(_spec(), token="t") == {"web_url": _ADOPTED_URL}


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
