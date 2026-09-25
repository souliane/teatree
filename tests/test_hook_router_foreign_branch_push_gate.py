# test-path: cross-cutting
# Exercises the hooks/scripts/foreign_branch_push_gate.py PreToolUse handler wired
# into hook_router.py (no src/teatree mirror), so it spans packages.
"""A push onto a branch another author owns must be refused.

An orchestrator dispatched a coder with "commit and push your branch" without
establishing who owned the branch: a 28-file commit landed on a colleague's open
draft MR, and the remediation FORCE-PUSHED their branch. Prose rules covering
that existed and were not followed.

The gate is FAIL-CLOSED, which inverts every sibling in ``hooks/scripts``, so
both polarities are pinned here: it allows a branch that is ours (absent from
the remote, backed by our own MR, or carrying our commits) and refuses
everything it cannot positively establish as ours — including when the probe
itself could not answer.

Git is REAL throughout (a bare origin plus working clones under ``tmp_path``);
only the forge CLI is faked, at the one seam the gate reaches it through.
"""

import ast
import json
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Final
from unittest.mock import patch

import pytest

import hooks.scripts.credential_redaction as redaction
import hooks.scripts.foreign_branch_push_argv as argv_parse
import hooks.scripts.foreign_branch_push_gate as gate
import hooks.scripts.foreign_branch_push_git as git_probes
import hooks.scripts.foreign_branch_push_text as refusal_text
import hooks.scripts.hook_router as router
from teatree.hooks._repo_visibility import ForgeProbe

OUR_EMAIL = "us@example.com"
THEIR_EMAIL = "colleague@example.com"
SLUG = "gitlab.com/acme/app"
# Deliberately low-entropy and self-describing: a realistic-looking token value
# here is what the repo's own secret scanner exists to refuse.
_FAKE_TOKEN_VALUE = "not-a-real-token"
# The fixed refusal prose (~700 chars) plus ONE `bounded()` quote of 400, with room
# for the branch and remote it names. A refusal past this is quoting something whole.
_BOUNDED_REFUSAL_CHARS = 1400
_TRUNCATED = f"<truncated-at-{redaction._MAX_SCANNED_CHARS}-chars>"
# One scan-cap's worth of work either way once the bound is the cap, with room for
# the 8 MB slice and a contended box.
_ONE_LINE_GROWTH_CEILING = 8.0


# The parts a collapsed refusal loses first: the shared cause opener, and the
# two remedy sentences `refusal` chooses between.
_UNOBTAINABLE_CAUSE = "Ownership could not be established"
_UNOBTAINABLE_BODY = refusal_text.unobtainable_body("`git remote get-url origin`")
_REMEDIES = ("Do this instead", "Re-issue the push")


def _carries_a_remedy(text: str) -> bool:
    return any(remedy in text for remedy in _REMEDIES)


def _refusal_for(body: str) -> str:
    return refusal_text.refusal("origin", "ac/x", body, force=False)


def _failed_probe(err: str) -> git_probes.GitProbe:
    return git_probes.GitProbe(ok=False, out="", code=128, err=err)


def _unparsable_ls_remote_probe(branch: str) -> git_probes.GitProbe:
    """The one `err` production builds itself rather than quoting from git.

    `remote_branch_oid` phrases an exit-0 answer it could not parse around the ref
    the push named, so *branch* reaches a refusal without passing through the clip
    `git_probe` applies to everything git says.
    """
    answered = git_probes.GitProbe(ok=True, out="not-an-oid line", code=0)
    with patch.object(git_probes, "git_probe", return_value=answered):
        return git_probes.remote_branch_oid("/w", "origin", branch)


def _no_repository_refusal(
    *,
    work_dir: str = "/w",
    remote: str = "origin",
    target: str = "ac/x",
    probe: git_probes.GitProbe | None = None,
) -> str:
    body = refusal_text.no_repository_body(
        work_dir, remote, target, probe if probe is not None else _failed_probe("not a git repository")
    )
    return refusal_text.refusal(remote, refusal_text.UNPINNED, body, force=False)


def _assert_undetermined_not_absent(reason: str, work_dir: Path) -> None:
    """Git established no absence at *work_dir*, so the refusal may not report one."""
    assert f"could not determine whether `{work_dir}` is a git repository" in reason
    assert "there is no git repository" not in reason, "the headline asserts absence it did not establish"
    assert "git -C" not in reason, "the name-the-repository remedy reproduces the very cause it answers"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        # `git` from PATH deliberately: the fixture must drive the same git the gate under
        # test resolves, against a real repository under tmp_path.
        ["git", "-C", str(cwd), *args],  # noqa: S607 — see above
        check=True,
        capture_output=True,
        text=True,
    )


def _git_text(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],  # noqa: S607 -- same real-git fixture seam as ``_git``.
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(cwd: Path, name: str, *, email: str, author: str) -> None:
    path = cwd / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name, encoding="utf-8")
    _git(cwd, "add", name)
    _git(
        cwd,
        "-c",
        f"user.email={email}",
        "-c",
        f"user.name={author}",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        f"add {name}",
    )


def _new_work(tmp_path: Path, name: str) -> Path:
    """Create a working clone of a real bare origin, on ``main``, authored by us."""
    origin = tmp_path / f"{name}-origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main", ".")
    checkout = tmp_path / name
    checkout.mkdir()
    _git(checkout, "init", "--initial-branch=main", ".")
    _git(checkout, "config", "user.email", OUR_EMAIL)
    _git(checkout, "config", "user.name", "Us")
    _git(checkout, "remote", "add", "origin", str(origin))
    _commit(checkout, "README.md", email=OUR_EMAIL, author="Us")
    _git(checkout, "push", "-q", "origin", "main")
    _git(checkout, "remote", "set-head", "origin", "main")
    return checkout


@pytest.fixture
def work(tmp_path: Path) -> Path:
    return _new_work(tmp_path, "work")


def _branch_pushed_by(work_dir: Path, branch: str, *, email: str, author: str) -> None:
    """Create *branch* on the remote carrying one commit by *author*, then fetch it."""
    _git(work_dir, "checkout", "-q", "-b", branch)
    _commit(work_dir, f"{branch}.txt", email=email, author=author)
    _git(work_dir, "push", "-q", "origin", branch)
    _git(work_dir, "checkout", "-q", "main")
    _git(work_dir, "branch", "-D", branch)
    _git(work_dir, "fetch", "-q", "origin")


def _advertising_push_options(work: Path) -> Path:
    """The fixture's own bare origin, made to accept `-o`; the file transport rejects it otherwise."""
    origin = work.parent / f"{work.name}-origin.git"
    _git(origin, "config", "receive.advertisePushOptions", "true")
    return origin


def _diverged_victim(work: Path) -> str:
    """`victim` on the remote at a colleague's OID, and locally at an unrelated one."""
    _branch_pushed_by(work, "victim", email=THEIR_EMAIL, author="Colleague")
    landed = _remote_victim(work)
    _git(work, "checkout", "-q", "-b", "victim", "main")
    _commit(work, "ours.txt", email=OUR_EMAIL, author="Us")
    assert _git_text(work, "rev-parse", "HEAD") != landed
    return landed


def _remote_victim(work: Path) -> str:
    return _git_text(work, "ls-remote", "--heads", "origin", "refs/heads/victim").split()[0]


def _really_run(command: str, cwd: Path) -> subprocess.CompletedProcess:
    """Run the very command the handler was just given, where it was told it would run."""
    return subprocess.run(shlex.split(command), cwd=cwd, capture_output=True, text=True, check=False)


def _event(command: str, cwd: Path) -> dict:
    return {"session_id": "sess-foreign-push", "tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


def _run(command: str, cwd: Path) -> tuple[bool, dict | None]:
    buf = StringIO()
    with patch("sys.stdout", buf):
        blocked = router.handle_block_foreign_branch_push(_event(command, cwd))
    raw = buf.getvalue().strip()
    return blocked, (json.loads(raw) if raw else None)


def _deny_route(command: str, cwd: Path) -> str:
    """Which path the handler took: the no-escape deny, the shared fail-open chain, or neither."""
    taken: list[str] = []

    def no_escape(_reason: str, *, gate_id: str | None = None) -> bool:
        assert gate_id == gate.GATE_ID
        taken.append("emit_pretooluse_deny")
        return True

    def shared_chain(_data: dict, _reason: str) -> bool:
        taken.append("_fail_open_or_deny")
        return True

    with (
        patch.object(router, "emit_pretooluse_deny", no_escape),
        patch.object(router, "_fail_open_or_deny", shared_chain),
    ):
        router.handle_block_foreign_branch_push(_event(command, cwd))
    return taken[0] if taken else "allowed"


def _refusal_reason(command: str, cwd: Path) -> str:
    blocked, payload = _run(command, cwd)
    assert blocked is True
    assert payload is not None
    return payload["permissionDecisionReason"]


def _forge(*, mr_rows: list | None, our_login: str = "us", unreachable: bool = False) -> SimpleNamespace:
    """A stand-in for the forge seam.

    Returns the real :class:`ForgeProbe`, not a bare string: a fake with a
    looser return type than production is a contract drift no test can see —
    the gate would keep passing here while raising on the real seam.
    """

    def run_forge_tool(_tool: str, argv: list[str]) -> ForgeProbe:
        if unreachable:
            return ForgeProbe(stdout=None, unresolved="exit-nonzero")
        if argv[:2] == ["api", "user"]:
            return ForgeProbe(stdout=json.dumps({"username": our_login, "login": our_login}))
        return ForgeProbe(stdout=json.dumps(mr_rows or []))

    return SimpleNamespace(
        GITHUB="github",
        FORGE_TOOL={"github": "gh", "gitlab": "glab"},
        slug_for_remote_url=lambda _url: SLUG,
        forge_and_repo_path=lambda _slug: ("gitlab", "acme/app"),
        host_of_slug=lambda _slug: "gitlab.com",
        run_forge_tool=run_forge_tool,
        declared_self_identities=lambda _host: frozenset(),
    )


def _mr(author: str) -> list[dict]:
    url = "https://gitlab.com/acme/app/-/merge_requests/6921"
    return [{"iid": 6921, "web_url": url, "author": {"username": author}}]


class TestABranchThatIsOursIsAllowed:
    def test_a_branch_absent_from_the_remote_is_ours_to_create(self, work: Path) -> None:
        _git(work, "checkout", "-q", "-b", "ac/brand-new")
        blocked, payload = _run("git push origin ac/brand-new", work)
        assert blocked is False
        assert payload is None

    def test_a_new_branch_needs_no_forge_call(self, work: Path) -> None:
        _git(work, "checkout", "-q", "-b", "ac/brand-new")
        with patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for a new branch")):
            blocked, _payload = _run("git push -u origin ac/brand-new", work)
        assert blocked is False

    def test_our_own_open_mr_on_an_existing_branch_is_allowed(self, work: Path) -> None:
        _branch_pushed_by(work, "ac/ours", email=OUR_EMAIL, author="Us")
        _git(work, "checkout", "-q", "-b", "ac/ours", "origin/ac/ours")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("us"), our_login="us")):
            blocked, payload = _run("git push origin ac/ours", work)
        assert blocked is False
        assert payload is None

    def test_our_own_commits_on_an_un_mr_d_branch_are_allowed(self, work: Path) -> None:
        _branch_pushed_by(work, "ac/ours", email=OUR_EMAIL, author="Us")
        _git(work, "checkout", "-q", "-b", "ac/ours", "origin/ac/ours")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run("git push origin ac/ours", work)
        assert blocked is False
        assert payload is None


class TestABranchThatIsNotOursIsRefused:
    def test_an_open_mr_authored_by_a_colleague_refuses(self, work: Path) -> None:
        _branch_pushed_by(work, "1234-feat-search", email=THEIR_EMAIL, author="colleague-login")
        _git(work, "checkout", "-q", "-b", "1234-feat-search", "origin/1234-feat-search")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("colleague-login"))):
            blocked, payload = _run("git push origin 1234-feat-search", work)
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"
        reason = payload["permissionDecisionReason"]
        assert "1234-feat-search" in reason
        assert "colleague-login" in reason
        assert "merge_requests/6921" in reason
        assert "our OWN branch" in reason

    def test_an_un_mr_d_branch_carrying_only_another_authors_commits_refuses(self, work: Path) -> None:
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run("git push origin theirs", work)
        assert blocked is True
        assert payload is not None
        assert THEIR_EMAIL in payload["permissionDecisionReason"]

    def test_a_colleagues_force_rewrite_cannot_hide_behind_our_stale_tracking_ref(self, work: Path) -> None:
        _branch_pushed_by(work, "shared", email=OUR_EMAIL, author="Us")
        _git(work, "checkout", "-q", "-b", "shared", "origin/shared")
        stale_oid = _git_text(work, "rev-parse", "origin/shared")

        colleague = work.parent / "colleague"
        _git(work.parent, "clone", "-q", str(work.parent / "work-origin.git"), str(colleague))
        _git(colleague, "checkout", "-q", "-B", "shared", "origin/main")
        _commit(colleague, "colleague-rewrite.txt", email=THEIR_EMAIL, author="Colleague")
        _git(colleague, "push", "-q", "--force", "origin", "shared")

        assert _git_text(work, "rev-parse", "origin/shared") == stale_oid
        assert _git_text(work, "ls-remote", "--heads", "origin", "refs/heads/shared").split()[0] != stale_oid
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run("git push origin shared", work)
        assert blocked is True
        assert payload is not None
        assert THEIR_EMAIL in payload["permissionDecisionReason"]

    def test_git_dir_and_work_tree_probe_the_repo_the_push_writes(self, work: Path, tmp_path: Path) -> None:
        target = _new_work(tmp_path, "target")
        _branch_pushed_by(target, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(target, "checkout", "-q", "-b", "theirs", "origin/theirs")

        command = f"git --git-dir={target / '.git'} --work-tree={target} push origin theirs"
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run(command, work)
        assert blocked is True
        assert payload is not None
        assert THEIR_EMAIL in payload["permissionDecisionReason"]

    def test_a_push_with_no_refspec_resolves_the_upstream_branch(self, work: Path) -> None:
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("colleague-login"))):
            blocked, _payload = _run("git push", work)
        assert blocked is True

    @pytest.mark.parametrize(
        "command",
        [
            "cd {cwd} && git push origin theirs",
            "git -C {cwd} push origin theirs",
            'bash -c "git push origin theirs"',
            "git status && git push origin theirs",
            "git push origin HEAD:theirs",
            "git push --delete origin theirs",
        ],
    )
    def test_the_push_is_seen_through_wrappers_and_chains(self, work: Path, command: str) -> None:
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("colleague-login"))):
            blocked, _payload = _run(command.format(cwd=work), work)
        assert blocked is True


class TestAForcePushHasNoEscape:
    @pytest.mark.parametrize(
        "command",
        [
            "git push --force origin theirs",
            "git push -f origin theirs",
            "git push --force-with-lease origin theirs",
            "git push origin +theirs",
        ],
    )
    def test_every_force_spelling_refuses(self, work: Path, command: str) -> None:
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("colleague-login"))):
            blocked, payload = _run(command, work)
        assert blocked is True
        assert payload is not None
        assert "--force" in payload["permissionDecisionReason"]

    def test_neither_a_sibling_escape_marker_nor_the_master_fail_open_releases_it(self, work: Path) -> None:
        """A force push past the shared allowlist + master fail-open switch."""
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        command = "git push --force origin theirs  # [scope-push-ok: vetted] [fp-confirmed: seen it] [add-all-ok: x]"
        with (
            patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("colleague-login"))),
            patch.object(router, "_danger_gate_fail_open_enabled", return_value=True),
        ):
            blocked, payload = _run(command, work)
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"

    def test_an_fp_confirmed_token_cannot_grant_it_through_the_live_breaker(
        self, work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        command = "git push --force origin HEAD:theirs  # [fp-confirmed: seen it]"
        monkeypatch.setattr(router, "STATE_DIR", tmp_path / "hook-state")
        monkeypatch.setattr(router, "_CURRENT_EVENT", "PreToolUse")
        monkeypatch.setattr(router, "_CURRENT_DATA", _event(command, work))
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("colleague-login"))):
            blocked, payload = _run(command, work)
        assert blocked is True
        assert payload is not None
        assert payload["hookSpecificOutput"]["gate_id"] == "foreign_branch_push"

    def test_a_non_force_push_still_routes_through_the_shared_fail_open_chain(self, work: Path) -> None:
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        with (
            patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("colleague-login"))),
            patch.object(router, "_danger_gate_fail_open_enabled", return_value=True),
        ):
            blocked, payload = _run("git push origin theirs", work)
        assert blocked is False
        assert payload is None


class TestAnUnansweredProbeRefuses:
    def test_an_unreachable_forge_refuses_and_names_the_probe(self, work: Path) -> None:
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=None, unreachable=True)):
            blocked, payload = _run("git push origin theirs", work)
        assert blocked is True
        assert payload is not None
        reason = payload["permissionDecisionReason"]
        assert "glab" in reason
        assert "fails closed" in reason

    def test_an_unimportable_forge_seam_refuses(self, work: Path) -> None:
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        with patch.object(gate, "_forge_seam", return_value=None):
            blocked, payload = _run("git push origin theirs", work)
        assert blocked is True
        assert payload is not None
        assert "foreign_mr_cli" in payload["permissionDecisionReason"]

    def test_an_unresolvable_login_refuses_rather_than_guessing_the_mr_is_ours(self, work: Path) -> None:
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        forge = _forge(mr_rows=_mr("colleague-login"), our_login="")
        with patch.object(gate, "_forge_seam", return_value=forge):
            blocked, payload = _run("git push origin theirs", work)
        assert blocked is True
        assert payload is not None
        assert "who we are on this forge" in payload["permissionDecisionReason"]

    @pytest.mark.parametrize(
        "command", ["git push --all origin", "git push --mirror origin", "git push origin $BRANCH"]
    )
    def test_a_push_naming_no_pinnable_branch_refuses(self, work: Path, command: str) -> None:
        blocked, payload = _run(command, work)
        assert blocked is True
        assert payload is not None
        assert "fails closed" in payload["permissionDecisionReason"]


class TestNarrowlyScoped:
    @pytest.mark.parametrize(
        "command",
        [
            "git status --short",
            "git log --oneline -5",
            "git fetch origin",
            "git commit -m 'never git push --force onto someone else branch'",
            "grep -rn 'git push --force' docs/",
            "echo git push --force origin main",
            "git push --dry-run origin theirs",
            "git push -n origin theirs",
        ],
    )
    def test_a_non_writing_command_is_untouched_and_never_probes_the_forge(self, work: Path, command: str) -> None:
        with patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for a non-push")):
            blocked, payload = _run(command, work)
        assert blocked is False
        assert payload is None

    def test_a_heredoc_body_mentioning_a_force_push_is_not_an_invocation(self, work: Path) -> None:
        command = "gh issue comment 1 --body-file - <<'EOF'\nthe fix: stop running git push --force\nEOF\n"
        with patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for a heredoc body")):
            blocked, payload = _run(command, work)
        assert blocked is False
        assert payload is None

    def test_a_non_bash_tool_is_ignored(self) -> None:
        event = {"tool_name": "Edit", "tool_input": {"file_path": "/x", "new_string": "git push --force origin theirs"}}
        assert router.handle_block_foreign_branch_push(event) is False


class TestTheOwnersConfigFlipIsTheOnlyWayOff:
    def test_the_kill_switch_disables_the_gate(self, work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        monkeypatch.setattr(
            router,
            "_teatree_bool_setting",
            lambda key, default=True: False if key == gate.GATE_SETTING else default,
        )
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("colleague-login"))):
            blocked, payload = _run("git push --force origin theirs", work)
        assert blocked is False
        assert payload is None

    def test_the_refusal_teaches_the_config_flip_and_offers_no_per_call_token(self, work: Path) -> None:
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("colleague-login"))):
            _blocked, payload = _run("git push origin theirs", work)
        assert payload is not None
        reason = payload["permissionDecisionReason"]
        assert "gate foreign-push disable" in reason
        assert "no per-call override" in reason
        assert "-ok:" not in reason


class TestTheRealForgeSeamResolves:
    """Every other test fakes the seam, so only this one can catch it breaking.

    The gate REFUSES on an unimportable seam, so a rename in
    ``teatree.hooks.foreign_mr_cli`` does not soften this gate — it turns it into
    a refuse-EVERY-push gate, and a suite that patches the seam everywhere stays
    green through it.
    """

    def test_the_seam_imports_outside_a_patch(self) -> None:
        assert gate._forge_seam() is not None

    @pytest.mark.parametrize(
        "attribute",
        [
            "slug_for_remote_url",
            "forge_and_repo_path",
            "host_of_slug",
            "FORGE_TOOL",
            "GITHUB",
            "run_forge_tool",
            "ForgeProbe",
            "declared_self_identities",
        ],
    )
    def test_the_seam_carries_every_symbol_the_gate_reads(self, attribute: str) -> None:
        assert hasattr(gate._forge_seam(), attribute)


class TestAntiVacuityControl:
    """Without this gate registered, nothing else in the chain refuses the foreign push."""

    def _chain_denies(self, command: str, cwd: Path, *, handlers: list) -> bool:
        buf = StringIO()
        with patch("sys.stdout", buf):
            return any(handler(_event(command, cwd)) for handler in handlers)

    def test_the_registered_chain_refuses_the_foreign_push(self, work: Path) -> None:
        _branch_pushed_by(work, "1234-feat-search", email=THEIR_EMAIL, author="colleague-login")
        _git(work, "checkout", "-q", "-b", "1234-feat-search", "origin/1234-feat-search")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("colleague-login"))):
            denied = self._chain_denies(
                "git push --force origin 1234-feat-search", work, handlers=router._HANDLERS["PreToolUse"]
            )
        assert denied is True

    def test_with_the_gate_removed_the_same_push_is_allowed(self, work: Path) -> None:
        _branch_pushed_by(work, "1234-feat-search", email=THEIR_EMAIL, author="colleague-login")
        _git(work, "checkout", "-q", "-b", "1234-feat-search", "origin/1234-feat-search")
        without_gate = [h for h in router._HANDLERS["PreToolUse"] if h is not router.handle_block_foreign_branch_push]
        assert len(without_gate) == len(router._HANDLERS["PreToolUse"]) - 1
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("colleague-login"))):
            denied = self._chain_denies("git push --force origin 1234-feat-search", work, handlers=without_gate)
        assert denied is False


def _colleague_branch(work_dir: Path) -> None:
    """Check out the colleague's branch itself, so a bare push targets it."""
    _branch_pushed_by(work_dir, "theirs", email=THEIR_EMAIL, author="Colleague")
    _git(work_dir, "checkout", "-q", "-b", "theirs", "origin/theirs")


def _ours_checked_out_theirs_on_the_remote(work_dir: Path) -> None:
    """Our own branch is checked out; the colleague's branch exists only on the remote.

    A shape the gate cannot read therefore judges OUR branch (or nothing) and
    allows — so every row below fails for the right reason on the pre-fix gate.
    """
    _branch_pushed_by(work_dir, "theirs", email=THEIR_EMAIL, author="Colleague")
    _branch_pushed_by(work_dir, "ac/ours", email=OUR_EMAIL, author="Us")
    _git(work_dir, "checkout", "-q", "-b", "ac/ours", "origin/ac/ours")


_SHELL_SHAPES = [
    pytest.param("cd {cwd}\ngit push origin theirs", id="newline"),
    pytest.param("git fetch origin\ngit push origin theirs && echo ok", id="newline-then-and"),
    pytest.param("git push \\\n  origin theirs", id="continuation-indented"),
    pytest.param("git push \\\norigin theirs", id="continuation-unindented"),
    pytest.param("git \\\n  push origin theirs", id="git-continuation-indented"),
    pytest.param("git \\\npush origin theirs", id="git-continuation-unindented"),
    pytest.param("{ git push origin theirs; }", id="brace-group"),
    pytest.param("( git push origin theirs )", id="subshell"),
    pytest.param('bash -lc "git push origin theirs"', id="bash-lc"),
    pytest.param('bash -ec "git push origin theirs"', id="bash-ec-cluster"),
    pytest.param("timeout 60 git push origin theirs", id="timeout-60"),
    pytest.param("timeout -k 5 60 git push origin theirs", id="timeout-k-5-60"),
    pytest.param("timeout --signal=TERM 60 git push origin theirs", id="timeout-signal-equals"),
    pytest.param("git push origin theirs 2>&1 | tail -20", id="redirect-2>&1"),
    pytest.param("if true; then git push origin theirs; fi", id="if-then-fi"),
]


class TestAShellShapeCannotHideThePush:
    """Each row is one shell command writing a colleague's branch while ours is checked out."""

    @pytest.mark.parametrize("command", _SHELL_SHAPES)
    def test_the_shape_still_reaches_the_ownership_ladder(self, work: Path, command: str) -> None:
        _ours_checked_out_theirs_on_the_remote(work)
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run(command.replace("{cwd}", str(work)), work)
        assert blocked is True
        assert payload is not None
        assert THEIR_EMAIL in payload["permissionDecisionReason"]

    def test_a_bare_push_survives_a_redirection(self, work: Path) -> None:
        _colleague_branch(work)
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run("git push 2>&1 | tail", work)
        assert blocked is True
        assert payload is not None
        assert THEIR_EMAIL in payload["permissionDecisionReason"]

    def test_a_loop_body_substitution_fails_closed_naming_the_refspec(self, work: Path) -> None:
        _ours_checked_out_theirs_on_the_remote(work)
        blocked, payload = _run("for b in theirs; do git push origin $b; done", work)
        assert blocked is True
        assert payload is not None
        reason = payload["permissionDecisionReason"]
        assert "$b" in reason
        assert "fails closed" in reason


class TestARedirectionIsNotARefspec:
    """A redirection's fd and its target are not push operands."""

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            pytest.param("git push origin theirs 2>&1 | tail -20", [("origin", ("theirs",))], id="fd-dup"),
            pytest.param("git push 2>&1 | tail", [("origin", ())], id="fd-dup-bare"),
            pytest.param("git push origin theirs > out.log", [("origin", ("theirs",))], id="stdout-file"),
            pytest.param("git push origin theirs 2> err.log", [("origin", ("theirs",))], id="stderr-file"),
            pytest.param("git push origin 123 > out.log", [("origin", ("123",))], id="numeric-branch-stdout"),
            pytest.param("git push origin 123 2> err.log", [("origin", ("123",))], id="numeric-branch-then-fd-dup"),
            pytest.param("git push origin 2 > out.log", [("origin", ("2",))], id="single-digit-branch"),
            pytest.param("git push origin 2> out.log", [("origin", ())], id="fd-redirect-no-branch"),
        ],
    )
    def test_the_redirection_tokens_never_become_operands(self, command: str, expected: list) -> None:
        parsed = gate.push_specs(command, "/repo")
        assert [(spec.remote, spec.refspecs) for spec in parsed.specs] == expected


class TestThePushedNameIsTheLocalOneNotTheUpstream:
    """`git push origin HEAD` writes the LOCAL branch name; judging the upstream is a bypass."""

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push origin HEAD", id="head-refspec"),
            pytest.param("git push origin @", id="at-refspec"),
            pytest.param("git -c push.default=current push", id="bare-push-default-current"),
        ],
    )
    def test_the_local_branch_name_is_the_branch_that_is_judged(self, work: Path, command: str) -> None:
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _branch_pushed_by(work, "ac/ours", email=OUR_EMAIL, author="Us")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/ac/ours")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run(command, work)
        assert blocked is True
        assert payload is not None
        assert THEIR_EMAIL in payload["permissionDecisionReason"]


_UNREADABLE_SHAPES = [
    pytest.param("bash <<'EOF'\ngit push origin theirs\nEOF\n", id="bash-heredoc"),
    pytest.param("sh <<'EOF'\ngit push origin theirs\nEOF\n", id="sh-heredoc"),
    pytest.param("bash -s -- git push origin theirs", id="shell-with-no-c-payload"),
    pytest.param("sudo -u other git push origin theirs", id="sudo-u"),
    pytest.param("nice -n 10 git push origin theirs", id="nice-n"),
    pytest.param("env -i git push origin theirs", id="env-i"),
    pytest.param("echo theirs | xargs -I{} git push origin {}", id="xargs"),
    pytest.param('eval "git push origin theirs"', id="eval"),
    pytest.param("ssh build-host 'git push origin theirs'", id="ssh"),
    pytest.param("git push origin theirs '", id="unbalanced-quote"),
]


class TestAnUnreadableCommandIsRefusedNotIgnored:
    """A push this gate cannot read is refused, and no forge is probed to decide it."""

    @pytest.mark.parametrize("command", _UNREADABLE_SHAPES)
    def test_the_refusal_names_the_single_line_re_issue_path(self, work: Path, command: str) -> None:
        with patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for unreadable material")):
            blocked, payload = _run(command, work)
        assert blocked is True
        assert payload is not None
        reason = payload["permissionDecisionReason"]
        assert "could not read" in reason
        assert "single-line" in reason

    def test_a_force_token_in_unreadable_material_has_no_escape(self, work: Path) -> None:
        with (
            patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for unreadable material")),
            patch.object(router, "_danger_gate_fail_open_enabled", return_value=True),
        ):
            blocked, payload = _run("sudo -u other git push --force origin theirs", work)
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"

    def test_a_non_force_unreadable_push_still_routes_through_the_shared_fail_open(self, work: Path) -> None:
        with (
            patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for unreadable material")),
            patch.object(router, "_danger_gate_fail_open_enabled", return_value=True),
        ):
            blocked, payload = _run("sudo -u other git push origin theirs", work)
        assert blocked is False
        assert payload is None

    def test_the_kill_switch_still_releases_unreadable_material(
        self, work: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            router,
            "_teatree_bool_setting",
            lambda key, default=True: False if key == gate.GATE_SETTING else default,
        )
        blocked, payload = _run("sudo -u other git push --force origin theirs", work)
        assert blocked is False
        assert payload is None


class TestTheResidueRuleStaysNarrow:
    """Text that merely MENTIONS a push, and wrappers that run no push, stay allowed."""

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("cat <<EOF\nnever run git push --force\nEOF\n", id="cat-heredoc"),
            pytest.param("git commit -F - <<'EOF'\nrevert the git push\nEOF\n", id="git-commit-heredoc"),
            pytest.param('python3 - <<\'EOF\'\nrun(["git", "push"])\nEOF\n', id="python-heredoc"),
            pytest.param("tee notes.txt <<'EOF'\ngit push --force origin main\nEOF\n", id="tee-heredoc"),
            pytest.param("bash <<'EOF'\ngrep -rn 'git push' notes.txt\nEOF\n", id="grep-in-an-executed-heredoc"),
            pytest.param("bash <<'EOF'\ngit config --get push.default\nEOF\n", id="push-default-in-a-heredoc"),
            pytest.param("git commit -m 'git push'", id="commit-message"),
            pytest.param("printf 'git push %s' x", id="printf"),
            pytest.param("grep -rn 'git push' docs/", id="grep"),
            pytest.param("bash -l", id="bash-login-no-c"),
            pytest.param("timeout 60 pytest -q", id="timeout-pytest"),
            pytest.param("git stash push", id="git-stash-push"),
            pytest.param("cd /tmp\nls -la", id="multi-line-no-push"),
            pytest.param("docker push registry/img", id="docker-push"),
            pytest.param("cat notes.md | grep push", id="pipe-no-git"),
            pytest.param("source ./deploy.sh", id="source-script-file"),
        ],
    )
    def test_a_command_that_pushes_nothing_is_untouched(self, work: Path, command: str) -> None:
        with patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for a non-push")):
            blocked, payload = _run(command, work)
        assert blocked is False
        assert payload is None


class TestAnEmptyRemoteBranchIsNotPresumedOurs:
    """A remote branch carrying no commit beyond the default says nothing about who owns it."""

    def _unowned_remote_branch(self, work_dir: Path, branch: str) -> None:
        _git(work_dir, "push", "-q", "origin", f"main:refs/heads/{branch}")
        _git(work_dir, "fetch", "-q", "origin")
        _git(work_dir, "checkout", "-q", "-b", branch, f"origin/{branch}")
        _commit(work_dir, f"{branch}.txt", email=OUR_EMAIL, author="Us")

    def test_a_branch_with_no_commit_beyond_the_default_refuses(self, work: Path) -> None:
        self._unowned_remote_branch(work, "unowned")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run("git push origin unowned", work)
        assert blocked is True
        assert payload is not None
        reason = payload["permissionDecisionReason"]
        assert "no commit beyond" in reason
        assert "ensure-pr" in reason

    def test_our_own_open_mr_on_it_still_allows(self, work: Path) -> None:
        self._unowned_remote_branch(work, "unowned")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("us"), our_login="us")):
            blocked, payload = _run("git push origin unowned", work)
        assert blocked is False
        assert payload is None


class TestTheDefaultBranchIsNotAnUnownedBranch:
    """`<oid>..<oid>` is empty for EVERY default-branch push — that is arithmetic, not evidence.

    ``live_remote_range`` resolves the base to the remote's default branch, so a
    push OF that branch compares it with itself. Reading the empty range as
    "no commit says whose this is" refuses `git push origin main` in every repo.
    """

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push origin main", id="named"),
            pytest.param("git push origin HEAD", id="head"),
            pytest.param("git push origin @", id="at"),
            pytest.param("git push", id="bare"),
            pytest.param("git push origin main:main", id="src-colon-dst"),
            pytest.param("git push origin HEAD:main", id="head-colon-main"),
        ],
    )
    def test_pushing_our_own_commit_onto_the_default_branch_is_allowed(self, work: Path, command: str) -> None:
        _commit(work, "second.txt", email=OUR_EMAIL, author="Us")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run(command, work)
        assert blocked is False
        assert payload is None

    def test_a_sibling_branch_with_no_commit_beyond_the_default_still_refuses(self, work: Path) -> None:
        """The guard is scoped to the default branch itself, not to every empty range."""
        _git(work, "push", "-q", "origin", "main:refs/heads/unowned")
        _git(work, "fetch", "-q", "origin")
        _git(work, "checkout", "-q", "-b", "unowned", "origin/unowned")
        _commit(work, "unowned.txt", email=OUR_EMAIL, author="Us")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run("git push origin unowned", work)
        assert blocked is True
        assert payload is not None
        assert "no commit beyond" in payload["permissionDecisionReason"]


class TestAHashIsAShellCommentNotAPushEraser:
    """One bug with two faces: `shlex.commenters` reads a comment with `readline()`.

    That call takes the NEWLINE with it — the separator this gate reads — and it
    ends a WORD at a `#` the shell keeps inside one.
    """

    def test_a_comment_does_not_swallow_the_separator_before_the_next_command(self, work: Path) -> None:
        _ours_checked_out_theirs_on_the_remote(work)
        command = f"cd {work} # note\ngit push --force origin theirs"
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run(command, work)
        assert blocked is True
        assert payload is not None
        assert THEIR_EMAIL in payload["permissionDecisionReason"]

    def test_a_hash_inside_a_branch_name_is_part_of_the_name(self) -> None:
        parsed = gate.push_specs("git push origin feature#123", "/repo")
        assert [spec.refspecs for spec in parsed.specs] == [("feature#123",)]

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            pytest.param("git push origin main  # rebased onto origin/main", [("main",)], id="trailing-comment"),
            pytest.param("git commit -m '#204 fix' && git push origin theirs", [("theirs",)], id="quoted-hash"),
            pytest.param('git push origin "#204-wip"', [("#204-wip",)], id="quoted-hash-refspec"),
        ],
    )
    def test_only_the_shells_own_comments_are_dropped(self, command: str, expected: list) -> None:
        parsed = gate.push_specs(command, "/repo")
        assert [spec.refspecs for spec in parsed.specs] == expected

    def test_a_comment_mentioning_a_push_is_not_a_push(self, work: Path) -> None:
        with patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for a comment")):
            blocked, payload = _run("pytest -q  # covers git push --force parsing", work)
        assert blocked is False
        assert payload is None


_QUOTED_SCRIPT_SHAPES = [
    pytest.param("xargs -I{} sh -c 'git push --force origin theirs' </dev/null", id="xargs-sh-c"),
    pytest.param("docker exec box bash -lc 'cd /app && git push origin theirs'", id="docker-exec"),
    pytest.param("setsid /bin/sh -c 'git push origin theirs'", id="setsid-sh-c"),
    pytest.param("setsid -f /bin/sh -c 'git push origin theirs'", id="setsid-f-sh-c"),
    pytest.param("kubectl exec pod -- sh -c 'git push origin theirs'", id="kubectl-exec"),
    pytest.param("kubectl exec pod -c app -- sh -c 'git push origin theirs'", id="kubectl-exec-container-flag"),
    pytest.param("echo 'git push origin theirs' | sh", id="echo-into-sh"),
    pytest.param("printf 'git push --force origin theirs\\n' | bash", id="printf-into-bash"),
]


class TestAQuotedScriptInsideAWrapperIsRefusedNotAllowed:
    """A word-level scan sees one opaque token where the shell sees a command.

    `names_git_push` matches TOKENS, so a wrapper carrying its push as a single
    quoted operand shows it nothing — and a gate that cannot read a command must
    not conclude "no push here".
    """

    @pytest.mark.parametrize("command", _QUOTED_SCRIPT_SHAPES)
    def test_the_refusal_names_the_single_line_re_issue_path(self, work: Path, command: str) -> None:
        with patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for unreadable material")):
            blocked, payload = _run(command, work)
        assert blocked is True
        assert payload is not None
        reason = payload["permissionDecisionReason"]
        assert "could not read" in reason
        assert "single-line" in reason

    def test_a_force_inside_the_quoted_script_has_no_escape(self, work: Path) -> None:
        with (
            patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for unreadable material")),
            patch.object(router, "_danger_gate_fail_open_enabled", return_value=True),
        ):
            blocked, payload = _run("xargs -I{} sh -c 'git push --force origin theirs'", work)
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("grep -rn 'git push' docs/", id="grep-pattern"),
            pytest.param("rg 'git push --force' src/", id="rg-pattern"),
            pytest.param("docker exec box bash -lc 'pytest -q'", id="docker-exec-no-push"),
            pytest.param("kubectl exec pod -c app -- sh -c 'pytest -q'", id="kubectl-container-flag-no-push"),
            pytest.param(
                "docker exec box sh -c 'ps -eo cmd | grep -E \"prek|git push\" | head -3'", id="grep-inside-a-script"
            ),
            pytest.param(
                "docker exec box bash -lc 'git -C /repo config --get push.default'", id="push-default-inside-a-script"
            ),
            pytest.param("echo 'deploy done' | sh", id="echo-no-push-into-sh"),
            pytest.param("cat deploy.sh | sh", id="script-file-into-sh"),
        ],
    )
    def test_a_quoted_operand_that_is_not_a_script_stays_allowed(self, work: Path, command: str) -> None:
        with patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for a non-push")):
            blocked, payload = _run(command, work)
        assert blocked is False
        assert payload is None


class TestTheForceScanIsScopedToThePush:
    """A force refusal bypasses the shared fail-open chain, so what escalates it matters.

    Scanning the whole unread text let any unrelated `+`-prefixed token — a diff
    body in an executed heredoc — escalate a plain push past the self-rescue
    allowlist and the master fail-open switch.
    """

    _DIFF_HEREDOC = (
        "bash <<'EOF'\n"
        "git apply <<'PATCH'\n"
        "--- a/notes.txt\n"
        "+++ b/notes.txt\n"
        "+added line\n"
        "PATCH\n"
        "git push origin theirs\n"
        "EOF\n"
    )

    def test_a_diff_body_does_not_escalate_a_plain_push_past_the_fail_open(self, work: Path) -> None:
        with patch.object(router, "_danger_gate_fail_open_enabled", return_value=True):
            blocked, payload = _run(self._DIFF_HEREDOC, work)
        assert blocked is False
        assert payload is None

    def test_the_same_body_still_refuses_when_the_fail_open_is_off(self, work: Path) -> None:
        blocked, payload = _run(self._DIFF_HEREDOC, work)
        assert blocked is True
        assert payload is not None
        assert "could not read" in payload["permissionDecisionReason"]

    def test_a_force_on_another_segment_does_not_escalate_the_unreadable_one(self, work: Path) -> None:
        command = "git push --force origin ac/ours && bash -s -- git push origin theirs"
        with patch.object(router, "_danger_gate_fail_open_enabled", return_value=True):
            blocked, payload = _run(command, work)
        assert blocked is False
        assert payload is None

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(_DIFF_HEREDOC.replace("git push origin", "git push --force origin"), id="heredoc-force"),
            pytest.param('eval "git push --force origin theirs"', id="quoted-eval-operand"),
            pytest.param("xargs -I{} sh -c 'git push --force origin theirs'", id="quoted-shell-script"),
        ],
    )
    def test_a_real_force_beside_the_push_still_bypasses_the_fail_open(self, work: Path, command: str) -> None:
        with patch.object(router, "_danger_gate_fail_open_enabled", return_value=True):
            blocked, payload = _run(command, work)
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"


class TestAForceCannotHideBehindAnotherRefusal:
    """The fail-open bypass is decided over EVERY refusing item, not the first read.

    Returning the FIRST refusal's force flag released a real `--force` onto a
    colleague's branch: any non-force `Unread` anywhere in the command was
    preferred over it, and among the read pushes the first one won. Every
    wrapper in the recorded corpus's unread traffic is non-force, so one
    anywhere in a command downgraded the force beside it.
    """

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push --force origin theirs", id="alone"),
            pytest.param("bash -s -- git push origin ac/ours ; git push --force origin theirs", id="behind-an-unread"),
            pytest.param("git push origin theirs && git push --force origin theirs", id="behind-a-plain-push"),
        ],
    )
    def test_the_force_still_bypasses_the_fail_open(self, work: Path, command: str) -> None:
        _ours_checked_out_theirs_on_the_remote(work)
        with (
            patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])),
            patch.object(router, "_danger_gate_fail_open_enabled", return_value=True),
        ):
            blocked, payload = _run(command, work)
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"
        assert "--force" in payload["permissionDecisionReason"]

    def test_a_force_push_that_is_ours_to_make_escalates_nothing(self, work: Path) -> None:
        """Only a REFUSING item's force flag escalates; an allowed force is not one."""
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "ac/ours")
        command = "git push --force origin ac/ours && bash -s -- git push origin theirs"
        with (
            patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])),
            patch.object(router, "_danger_gate_fail_open_enabled", return_value=True),
        ):
            blocked, payload = _run(command, work)
        assert blocked is False
        assert payload is None


_WRAPPED_HEREDOC_SHAPES = [
    pytest.param("docker exec -i box sh <<'EOF'\ngit push --force origin theirs\nEOF\n", id="docker-exec-sh"),
    pytest.param("docker compose exec -T worker bash <<'EOF'\ngit push origin theirs\nEOF\n", id="compose-exec-bash"),
    pytest.param("kubectl exec -i pod -- sh <<'EOF'\ngit push origin theirs\nEOF\n", id="kubectl-exec-sh"),
    pytest.param("timeout 60 bash <<'EOF'\ngit push origin theirs\nEOF\n", id="timeout-bash"),
]


class TestAHeredocReachedThroughAWrapperIsStillExecuted:
    """The consumer of a heredoc is resolved THROUGH the wrapper that hands it over.

    Keying on the first command name of the heredoc's own line reads
    `docker exec -i box sh <<'EOF'` as `docker`, which executes nothing — so the
    push inside was neither read nor refused, the fourth outcome the
    three-outcome contract forbids. The `-c` form of the same wrapper family is
    all of the corpus's unread traffic.
    """

    @pytest.mark.parametrize("command", _WRAPPED_HEREDOC_SHAPES)
    def test_the_refusal_names_the_single_line_re_issue_path(self, work: Path, command: str) -> None:
        with patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for unreadable material")):
            blocked, payload = _run(command, work)
        assert blocked is True
        assert payload is not None
        reason = payload["permissionDecisionReason"]
        assert "could not read" in reason
        assert "single-line" in reason

    def test_a_force_inside_the_wrapped_heredoc_has_no_escape(self, work: Path) -> None:
        command = "docker exec -i box sh <<'EOF'\ngit push --force origin theirs\nEOF\n"
        with (
            patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for unreadable material")),
            patch.object(router, "_danger_gate_fail_open_enabled", return_value=True),
        ):
            blocked, payload = _run(command, work)
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("docker exec -i box cat <<'EOF'\ngit push --force origin theirs\nEOF\n", id="wrapped-cat"),
            pytest.param("docker cp - box:/tmp <<'EOF'\ngit push origin theirs\nEOF\n", id="wrapped-cp"),
        ],
    )
    def test_a_wrapper_handing_the_body_to_a_non_shell_stays_data(self, work: Path, command: str) -> None:
        with patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for a heredoc body")):
            blocked, payload = _run(command, work)
        assert blocked is False
        assert payload is None


class TestNestingDeeperThanTheGateReadsIsRefusedNotDropped:
    """The recursion cutoff must not read as "no push here".

    `_MAX_WRAPPER_DEPTH` bounds the work; returning bare at the cutoff made a
    push one level past it yield zero specs AND zero unread, so it was allowed.
    """

    @staticmethod
    def _nested(levels: int) -> str:
        script = "git push --force origin theirs"
        for _level in range(levels):
            script = f"bash -c {shlex.quote(script)}"
        return script

    def test_a_nest_within_reach_is_read_and_judged(self, work: Path) -> None:
        _ours_checked_out_theirs_on_the_remote(work)
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run(self._nested(3), work)
        assert blocked is True
        assert payload is not None
        assert THEIR_EMAIL in payload["permissionDecisionReason"]

    def test_a_nest_past_the_cutoff_is_refused_as_unreadable(self, work: Path) -> None:
        with patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for unreadable material")):
            blocked, payload = _run(self._nested(4), work)
        assert blocked is True
        assert payload is not None
        reason = payload["permissionDecisionReason"]
        assert "could not read" in reason
        assert "single-line" in reason

    def test_a_nest_past_the_cutoff_that_pushes_nothing_stays_allowed(self, work: Path) -> None:
        script = "pytest -q"
        for _level in range(4):
            script = f"bash -c {shlex.quote(script)}"
        with patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for a non-push")):
            blocked, payload = _run(script, work)
        assert blocked is False
        assert payload is None


class TestForcingTheDefaultBranchIsRefusedWhoeverOwnsIt:
    """A force rewrite of the default branch is refused here or nowhere.

    `handle_protect_default_branch` is scoped to `Edit|Write|Read` with a
    `file_path`, so it never sees a Bash push at all.
    """

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push --force origin main", id="named"),
            pytest.param("git push -f origin main", id="short-flag"),
            pytest.param("git push origin +main", id="plus-refspec"),
            pytest.param("git push --force-with-lease origin main", id="with-lease"),
            pytest.param("git push --force", id="bare"),
        ],
    )
    def test_a_force_rewrite_of_the_default_branch_bypasses_the_fail_open(self, work: Path, command: str) -> None:
        _commit(work, "second.txt", email=OUR_EMAIL, author="Us")
        with (
            patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])),
            patch.object(router, "_danger_gate_fail_open_enabled", return_value=True),
        ):
            blocked, payload = _run(command, work)
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"
        reason = payload["permissionDecisionReason"]
        assert "DEFAULT branch" in reason
        assert "no commit beyond" not in reason


class TestOnlyThePushsOwnFlagForcesIt:
    """A wrapper's own `-f` is not the push's `--force`.

    Scoping the scan to the unread SEGMENT still reads the wrapper's argv, and
    `-f` means `--file` to `docker compose` and "go to background" to `ssh`. A
    force refusal bypasses the self-rescue allowlist and the master fail-open
    switch, so a wrapper flag must not raise one — the recorded corpus carries a
    `docker compose -f <file> … sh -c 'git push -u origin <branch>'` that did.
    """

    _COMPOSE = "docker compose -f deploy/docker-compose.yml exec -T worker sh -c 'git push {flags}origin theirs'"
    _SSH = "ssh -f build-host 'git push {flags}origin theirs'"

    @pytest.mark.parametrize("wrapper", [pytest.param(_COMPOSE, id="compose-f"), pytest.param(_SSH, id="ssh-f")])
    def test_a_plain_push_under_it_still_routes_through_the_fail_open(self, work: Path, wrapper: str) -> None:
        with patch.object(router, "_danger_gate_fail_open_enabled", return_value=True):
            blocked, payload = _run(wrapper.format(flags=""), work)
        assert blocked is False
        assert payload is None

    @pytest.mark.parametrize("wrapper", [pytest.param(_COMPOSE, id="compose-f"), pytest.param(_SSH, id="ssh-f")])
    def test_the_pushs_own_force_under_it_still_bypasses_the_fail_open(self, work: Path, wrapper: str) -> None:
        with patch.object(router, "_danger_gate_fail_open_enabled", return_value=True):
            blocked, payload = _run(wrapper.format(flags="--force "), work)
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"


_HEREDOC_OPENER_SHAPES = [
    pytest.param("cat <<'EOF' && git push origin theirs\nnote\nEOF\n", id="and"),
    pytest.param("false <<'EOF' || git push origin theirs\nnote\nEOF\n", id="or"),
    pytest.param("cat <<'EOF' ; git push origin theirs\nnote\nEOF\n", id="semicolon"),
    pytest.param("cat <<'EOF' | git push origin theirs\nnote\nEOF\n", id="pipe"),
    pytest.param("cat <<-EOF && git push origin theirs\n\tnote\n\tEOF\n", id="tab-stripped"),
]


class TestTheTailOfAHeredocOpenerIsStillShell:
    """A heredoc span runs to its terminator, so the opener LINE's tail went too.

    `cat <<'EOF' && git push --force origin theirs` yielded zero specs AND zero
    unread — the fourth outcome the three-outcome contract forbids, and the one
    under which the colleague's ref actually moves.
    """

    @pytest.mark.parametrize("command", _HEREDOC_OPENER_SHAPES)
    def test_the_push_after_the_redirect_is_read_and_judged(self, work: Path, command: str) -> None:
        _ours_checked_out_theirs_on_the_remote(work)
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run(command, work)
        assert blocked is True
        assert payload is not None
        assert THEIR_EMAIL in payload["permissionDecisionReason"]

    def test_the_body_itself_stays_data(self, work: Path) -> None:
        _ours_checked_out_theirs_on_the_remote(work)
        command = "cat <<'EOF' && echo done\ngit push --force origin theirs\nEOF\n"
        with patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for a heredoc body")):
            blocked, payload = _run(command, work)
        assert blocked is False
        assert payload is None


class TestAShellsOwnFlagsEndAtTheOptionMarker:
    """`sh -s -- -c foo` runs STDIN: past `--` a `-c` is positional, not a payload.

    Read as the shell's own payload it made an EXECUTED heredoc look like data,
    so the push inside was neither read nor refused.
    """

    def test_a_heredoc_past_the_marker_is_refused_as_unreadable(self, work: Path) -> None:
        command = "sh -s -- -c foo <<'EOF'\ngit push --force origin theirs\nEOF\n"
        with patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for unreadable material")):
            blocked, payload = _run(command, work)
        assert blocked is True
        assert payload is not None
        assert "could not read" in payload["permissionDecisionReason"]

    def test_a_real_payload_before_the_marker_still_displaces_stdin(self, work: Path) -> None:
        command = "bash -c 'cat > f' -- <<'EOF'\ngit push --force origin theirs\nEOF\n"
        with patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for a heredoc body")):
            blocked, payload = _run(command, work)
        assert blocked is False
        assert payload is None


class TestProcessSubstitutionCannotHideThePush:
    """`<(` glues a redirect char to `(` via the same punctuation-run grouping.

    Read as an ordinary redirection, the token AFTER it — the subshell's own
    first word, ``git`` — was skipped as "the redirection target", so the
    segment left behind read as ``diff push origin theirs`` and never matched
    ``names_git_push`` at all: the push was invisible, not merely unread.
    """

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("diff <(git push --force origin theirs) /dev/null", id="input-process-substitution"),
            pytest.param("tee >(git push --force origin theirs) </dev/null", id="output-process-substitution"),
        ],
    )
    def test_the_push_inside_is_read_and_refused(self, work: Path, command: str) -> None:
        _colleague_branch(work)
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run(command, work)
        assert blocked is True
        assert payload is not None
        assert THEIR_EMAIL in payload["permissionDecisionReason"]

    def test_a_push_inside_it_that_is_ours_is_still_allowed(self, work: Path) -> None:
        """Over-block evidence: legitimate work through the same shape must pass."""
        _branch_pushed_by(work, "ac/ours", email=OUR_EMAIL, author="Us")
        _git(work, "checkout", "-q", "-b", "ac/ours", "origin/ac/ours")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run("diff <(git push origin ac/ours) /dev/null", work)
        assert blocked is False
        assert payload is None

    def test_a_process_substitution_that_pushes_nothing_stays_untouched(self, work: Path) -> None:
        """Over-block evidence: a bare process substitution is not a push."""
        with patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for a non-push")):
            blocked, payload = _run("diff <(git log --oneline) /dev/null", work)
        assert blocked is False
        assert payload is None


class TestANumericBranchOperandSurvivesARedirect:
    """A branch named with digits, immediately before a redirect, is still an operand.

    The old code popped any digit-only token preceding a redirection SEPARATOR
    unconditionally — so ``git push origin 123 > out.log`` silently dropped its
    own refspec and fell back to bare-push semantics judging whatever branch we
    happened to have checked out, while the real ``git push`` still wrote `123`.
    """

    def test_a_numeric_branch_that_is_foreign_is_still_refused(self, work: Path) -> None:
        _branch_pushed_by(work, "123", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "ac/ours")  # our OWN current branch, not the actual target
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run("git push origin 123 > out.log", work)
        assert blocked is True
        assert payload is not None
        assert THEIR_EMAIL in payload["permissionDecisionReason"]

    def test_a_numeric_branch_that_is_ours_is_still_allowed(self, work: Path) -> None:
        """Over-block evidence: a genuinely-numeric branch of ours must pass."""
        _branch_pushed_by(work, "123", email=OUR_EMAIL, author="Us")
        _git(work, "checkout", "-q", "-b", "123", "origin/123")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run("git push origin 123 > out.log", work)
        assert blocked is False
        assert payload is None

    def test_a_genuine_fd_duplication_still_strips_no_operand(self, work: Path) -> None:
        """Over-block evidence: the pre-existing `2>&1` shape is unaffected."""
        _colleague_branch(work)
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run("git push origin theirs 2>&1 | tail -20", work)
        assert blocked is True
        assert payload is not None
        assert THEIR_EMAIL in payload["permissionDecisionReason"]


def _push_spec(refspec: str, *, force: bool) -> gate.PushSpec:
    return gate.PushSpec(
        work_dir="/repo", remote="origin", refspecs=(refspec,), force=force, destructive=force, unpinnable=""
    )


class TestEagerEvaluationCannotLoseAnEstablishedDenial:
    """Probing every push regardless of an already-decided verdict risks the hook's own timeout.

    A `--force` refusal is decisive — nothing beside it changes the verdict —
    so evaluating a slower non-force item's forge probe after finding it is
    wasted work that, on the real hook's 30s ceiling, can cost the whole
    process its chance to ever emit the denial it already computed.
    """

    def test_a_force_refusal_short_circuits_before_a_later_non_force_probe(self) -> None:
        force_spec = _push_spec("theirs", force=True)
        other_spec = _push_spec("mine", force=False)
        parsed = gate.ParsedPushes(specs=(force_spec, other_spec), unread=())
        probed: list[gate.PushSpec] = []

        def fake_push_refusal(spec: gate.PushSpec) -> str | None:
            probed.append(spec)
            return "REFUSED: force" if spec.force else None

        with patch.object(gate, "push_refusal", side_effect=fake_push_refusal):
            verdict = gate._refusal_verdict(parsed)

        assert verdict == ("REFUSED: force", True)
        assert probed == [force_spec]

    def test_a_non_force_item_ahead_of_a_force_refusal_is_never_probed(self) -> None:
        """Force items are evaluated FIRST regardless of their position in the command."""
        non_force_spec = _push_spec("theirs", force=False)
        force_spec = _push_spec("mine", force=True)
        parsed = gate.ParsedPushes(specs=(non_force_spec, force_spec), unread=())
        probed: list[gate.PushSpec] = []

        def fake_push_refusal(spec: gate.PushSpec) -> str | None:
            probed.append(spec)
            return "REFUSED: force" if spec.force else "REFUSED: plain"

        with patch.object(gate, "push_refusal", side_effect=fake_push_refusal):
            verdict = gate._refusal_verdict(parsed)

        assert verdict == ("REFUSED: force", True)
        assert probed == [force_spec]

    def test_every_item_still_allowed_reaches_none(self) -> None:
        """Over-block evidence: when nothing refuses, every item is still checked and it allows."""
        first = _push_spec("a", force=False)
        second = _push_spec("b", force=True)
        parsed = gate.ParsedPushes(specs=(first, second), unread=())
        probed: list[gate.PushSpec] = []

        def fake_push_refusal(spec: gate.PushSpec) -> str | None:
            probed.append(spec)
            return None

        with patch.object(gate, "push_refusal", side_effect=fake_push_refusal):
            verdict = gate._refusal_verdict(parsed)

        assert verdict is None
        assert set(probed) == {first, second}


class TestAnOwnedMrFromTheDefaultBranchDoesNotBypassForceProtection:
    """Ownership does not enter into a default-branch rewrite — checked before rung 2 can.

    ``_open_mr_owner`` returning "ours" short-circuited `_branch_refusal` before
    the default-branch force check (then reachable only through
    `_foreign_commit_refusal`, itself reachable only on a "none" verdict) ever
    ran — so an open MR whose SOURCE is the default branch, authored by us,
    let a force rewrite of that branch straight through.
    """

    def test_a_force_rewrite_of_main_is_refused_despite_our_own_mr_sourced_from_it(self, work: Path) -> None:
        _commit(work, "second.txt", email=OUR_EMAIL, author="Us")
        with (
            patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("us"), our_login="us")),
            patch.object(router, "_danger_gate_fail_open_enabled", return_value=True),
        ):
            blocked, payload = _run("git push --force origin main", work)
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"
        reason = payload["permissionDecisionReason"]
        assert "DEFAULT branch" in reason

    def test_a_force_push_onto_our_own_non_default_branch_is_still_allowed(self, work: Path) -> None:
        """Over-block evidence: the new pre-check must not swallow a legitimate force."""
        _branch_pushed_by(work, "ac/ours", email=OUR_EMAIL, author="Us")
        _git(work, "checkout", "-q", "-b", "ac/ours", "origin/ac/ours")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("us"), our_login="us")):
            blocked, payload = _run("git push --force origin ac/ours", work)
        assert blocked is False
        assert payload is None

    def test_a_non_force_push_onto_main_with_our_own_mr_sourced_from_it_is_still_allowed(self, work: Path) -> None:
        """Over-block evidence: the new pre-check is scoped to FORCE, not to every push of main."""
        _commit(work, "second.txt", email=OUR_EMAIL, author="Us")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("us"), our_login="us")):
            blocked, payload = _run("git push origin main", work)
        assert blocked is False
        assert payload is None


class TestAPayloadlessShellIsNotFedUnrelatedPrecedingOutput:
    """`_printed_script` treated ANY preceding segment as this shell's stdin.

    ``echo '...' ; sh`` merely SEQUENCES two commands — the echo prints to the
    terminal, not into the following shell's stdin — yet the old code read the
    echoed text as if it had been piped in, so an unrelated mention of a push
    in the echoed text refused a harmless bare ``sh``.
    """

    def test_a_sequenced_not_piped_echo_does_not_poison_a_following_bare_shell(self, work: Path) -> None:
        command = "echo 'reminder: run git push --force before eod' ; sh"
        with patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for unrelated output")):
            blocked, payload = _run(command, work)
        assert blocked is False
        assert payload is None

    def test_a_genuinely_piped_echo_is_still_read_as_executed(self, work: Path) -> None:
        """The `|` shape this rule protects must still be refused."""
        command = "echo 'git push --force origin theirs' | sh"
        with patch.object(gate, "_forge_seam", side_effect=AssertionError("forge probed for a wrapper's own args")):
            blocked, payload = _run(command, work)
        assert blocked is True
        assert payload is not None
        reason = payload["permissionDecisionReason"]
        assert "could not read" in reason


class TestAnUnanswerableProbeNamesWhatWentWrong:
    """A fail-closed refusal must carry the cause, not send the reader hunting.

    ``git_probe`` collapsed four different failures into one opaque
    ``GitProbe(ok=False, out="")`` — no repository at the probed dir, the remote
    unreachable, ``git`` unrunnable, and a timeout all reported as "`git
    ls-remote …` did not answer". The reader then pasted that argv into a shell
    where it SUCCEEDED, because the gate had run it against a different
    directory. These pin the cause into the refusal.
    """

    def test_a_work_dir_outside_any_repository_names_the_dir_the_cause_and_the_remedy(self, tmp_path: Path) -> None:
        outside = tmp_path / "no-repo-here"
        outside.mkdir()
        blocked, payload = _run("git push origin ac/brand-new", outside)
        assert blocked is True
        assert payload is not None
        reason = payload["permissionDecisionReason"]
        assert str(outside) in reason, "the refusal must name the directory it probed"
        assert "no git repository" in reason
        assert "exit 128" in reason, "git's own exit code must be quoted"
        assert "not a git repository" in reason, "git's own stderr must be quoted"
        assert "git -C" in reason, "the refusal must name the remedy that makes it answerable"
        assert "ls-remote" not in reason, "naming an argv that succeeds when pasted is the defect"

    def test_the_remedy_the_refusal_advertises_allows_our_own_new_branch(self, work: Path, tmp_path: Path) -> None:
        """The `-C <repo>` form the no-repo refusal prescribes must actually push."""
        outside = tmp_path / "no-repo-here"
        outside.mkdir()
        _git(work, "checkout", "-q", "-b", "ac/brand-new")
        blocked, payload = _run(f"git -C {work} push origin ac/brand-new", outside)
        assert blocked is False
        assert payload is None

    def test_an_unreachable_remote_quotes_gits_return_code_and_stderr(self, work: Path, tmp_path: Path) -> None:
        _git(work, "checkout", "-q", "-b", "ac/brand-new")
        _git(work, "remote", "set-url", "origin", str(tmp_path / "vanished-origin.git"))
        blocked, payload = _run("git push origin ac/brand-new", work)
        assert blocked is True
        assert payload is not None
        reason = payload["permissionDecisionReason"]
        assert "exit 128" in reason
        assert "does not appear to be a git repository" in reason

    def test_a_timeout_is_distinguished_from_a_probe_that_ran_and_failed(self, work: Path) -> None:
        _git(work, "checkout", "-q", "-b", "ac/brand-new")
        expired = subprocess.TimeoutExpired(cmd=["git", "ls-remote"], timeout=12.0)
        with patch.object(git_probes.subprocess, "run", side_effect=expired):
            blocked, payload = _run("git push origin ac/brand-new", work)
        assert blocked is True
        assert payload is not None
        reason = payload["permissionDecisionReason"]
        assert "timed out" in reason
        _assert_undetermined_not_absent(reason, work)

    def test_an_unparsable_remote_answer_is_quoted_under_the_same_bound_as_stderr(self, work: Path) -> None:
        """`remote_branch_oid` quotes git's STDOUT; an unbounded quote buries the refusal it explains."""
        _git(work, "checkout", "-q", "-b", "ac/brand-new")
        flood = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef refs/heads/noise\n" * 400

        def answered(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(argv, 0, ".git" if "rev-parse" in argv else flood, "")

        with patch.object(git_probes.subprocess, "run", side_effect=answered):
            reason = _refusal_reason("git push origin ac/brand-new", work)
        assert "advertised no parsable" in reason, "the control: the refusal really is quoting git's stdout"
        assert "…" in reason, "an oversized answer is truncated, not quoted whole"
        assert len(reason) < len(flood), "the refusal must not grow with the size of git's answer"

    def test_an_unparsable_remote_answer_does_not_offer_a_zero_exit_as_the_cause(self, work: Path) -> None:
        """This branch is reachable only when `ls-remote` EXITED 0, so quoting its rc says nothing.

        `probe_cause` would otherwise render `It failed with exit 0: …` — a success code
        offered as the cause of the refusal it is there to explain.
        """
        _git(work, "checkout", "-q", "-b", "ac/brand-new")

        def answered(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            answer = ".git" if "rev-parse" in argv else "this is not a ref advertisement"
            return subprocess.CompletedProcess(argv, 0, answer, "")

        with patch.object(git_probes.subprocess, "run", side_effect=answered):
            reason = _refusal_reason("git push origin ac/brand-new", work)
        assert "advertised no parsable" in reason, "the control: this really is the unparsable-answer branch"
        assert "exit 0" not in reason, "a zero exit is not a cause; beside a refusal it reads as a success"
        assert "exited 0" in reason, "the reader still needs to know git itself did not fail"

    def test_a_flood_of_commit_authors_is_quoted_under_the_same_bound(self, work: Path) -> None:
        """`_foreign_commits_body` joins the author SET, whose size the pushed branch decides.

        The stderr and stdout quotes were bounded and this one was not: 500 distinct
        authors produced a 15k-char refusal, burying the one sentence that says what
        to do about it.
        """
        _git(work, "checkout", "-q", "-b", "theirs")
        for index in range(20):
            _commit(
                work, f"theirs-{index}.txt", email=f"colleague-{index:03d}@example.com", author=f"Colleague {index}"
            )
        _git(work, "push", "-q", "origin", "theirs")
        _git(work, "checkout", "-q", "main")
        _git(work, "branch", "-D", "theirs")
        _git(work, "fetch", "-q", "origin")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            reason = _refusal_reason("git push origin theirs", work)
        assert "colleague-000@example.com" in reason, "the control: the refusal really is quoting the author set"
        assert "…" in reason, "an oversized author set is truncated, not quoted whole"
        assert len(reason) < _BOUNDED_REFUSAL_CHARS, f"the refusal grows with the author count: {len(reason)} chars"

    def test_a_git_that_cannot_be_executed_is_distinguished_from_a_timeout(self, work: Path) -> None:
        _git(work, "checkout", "-q", "-b", "ac/brand-new")
        missing = FileNotFoundError(2, "No such file or directory", "git")
        with patch.object(git_probes.subprocess, "run", side_effect=missing):
            blocked, payload = _run("git push origin ac/brand-new", work)
        assert blocked is True
        assert payload is not None
        reason = payload["permissionDecisionReason"]
        assert "could not run git" in reason
        assert "timed out" not in reason
        _assert_undetermined_not_absent(reason, work)


class TestAbsenceIsAssertedOnlyWhenGitReportsIt:
    """A non-zero return code is not absence — the refusal may claim only what git said.

    An unsupported ``repositoryformatversion`` and dubious ownership on a shared
    checkout both exit non-zero over a directory that IS a repository; a
    directory git could not enter was never looked inside at all. Each produced
    "there is no git repository at `<dir>`" two clauses in front of git's own
    contradicting cause, prescribing a ``git -C`` remedy reproducing that rc.
    """

    def test_a_directory_git_could_not_enter_is_undetermined_not_absent(self, work: Path) -> None:
        """An unexpanded `$WORKTREE` joined onto a live repo's path: git never looked inside one."""
        reason = _refusal_reason("cd $WORKTREE && git push origin ac/brand-new", work)
        assert "No such file or directory" in reason, "git's own cause must still be quoted"
        _assert_undetermined_not_absent(reason, work / "$WORKTREE")

    def test_an_unsupported_repository_version_is_undetermined_not_absent(self, work: Path) -> None:
        """`.git` is present and healthy; only its declared format is one this git will not read."""
        config = work / ".git" / "config"
        config.write_text(
            config.read_text(encoding="utf-8").replace("repositoryformatversion = 0", "repositoryformatversion = 99"),
            encoding="utf-8",
        )
        reason = _refusal_reason("git push origin ac/brand-new", work)
        assert "found 99" in reason, "git's own cause must still be quoted"
        _assert_undetermined_not_absent(reason, work)

    @pytest.mark.parametrize(
        ("stderr", "reports_absence"),
        [
            ("fatal: not a git repository (or any of the parent directories): .git", True),
            ("fatal: 'origin' does not appear to be a git repository", True),
            ("fatal: detected dubious ownership in repository at '/srv/shared/app'", False),
            ("fatal: Expected git repo version <= 1, found 99", False),
            ("fatal: cannot change to '/srv/shared/app/$WORKTREE': No such file or directory", False),
            ("", False),
        ],
    )
    def test_the_headline_follows_gits_own_words(self, stderr: str, *, reports_absence: bool) -> None:
        """The absence headline and its name-the-repository remedy are one decision, taken here."""
        probe = git_probes.GitProbe(ok=False, out="", code=128, err=stderr)
        body = refusal_text.no_repository_body("/srv/shared/app", "origin", "ac/x", probe)
        assert ("there is no git repository" in body) is reports_absence
        assert ("could not determine whether" in body) is not reports_absence
        assert ("git -C <repo> push" in body) is reports_absence
        assert stderr in body or not stderr, "git's cause is quoted whichever headline it earns"


class TestEveryGitProbeThatFailedCarriesItsCauseToTheRefusal:
    """Rung 4's claim, executed at every probe site rather than at the one that was threaded.

    The ladder says a refusal names the unanswered probe PLUS its rc and stderr.
    Three sites named the argv and dropped the answer, which is exactly the shape
    that sent a reader pasting `git ls-remote --heads origin <branch>` against
    their worktree, watching it succeed, and learning nothing: the argv is the
    question, and only git's own rc and stderr are the answer.
    """

    def test_the_remote_get_url_probe_carries_its_rc_and_stderr(self, work: Path) -> None:
        """`_forge_context` runs `git remote get-url` — a git probe like any other.

        A push whose first operand is a URL rather than a configured remote name
        reaches this for real: `ls-remote <url>` answers, and `remote get-url <url>`
        exits non-zero because no remote is named that.
        """
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        url = subprocess.run(
            # `git` from PATH deliberately, as `_git` above: the fixture's own bare origin.
            ["git", "-C", str(work), "remote", "get-url", "origin"],  # noqa: S607 — see above
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            reason = _refusal_reason(f"git push {url} theirs", work)
        assert f"`git remote get-url {url}`" in reason, "the control: this really is the get-url branch"
        assert "exit " in reason, "the probe ran and failed; its exit code is the answer"
        assert "No such remote" in reason, "git's own words are what tell this failure from a dead forge"

    def test_the_head_probe_carries_its_rc_and_stderr(self, work: Path) -> None:
        """`_current_branch` pins a bare `git push origin`; a detached HEAD is a probe that failed."""
        _git(work, "checkout", "-q", "--detach")
        reason = _refusal_reason("git push origin", work)
        assert "HEAD is detached or unreadable" in reason, "the control: this really is the unpinned-target branch"
        assert "exit " in reason, "the probe ran and failed; its exit code is the answer"
        assert "symbolic ref" in reason, "git's own words say WHICH way HEAD would not resolve"

    def test_the_remote_default_branch_probe_carries_its_rc_and_stderr(self, work: Path) -> None:
        """`live_remote_range` opens on `<remote>/HEAD`; without it there is no range to read."""
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(work, "remote", "set-head", "origin", "--delete")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            reason = _refusal_reason("git push origin theirs", work)
        assert "the remote's default branch" in reason, "the control: this really is the live-range branch"
        assert "exit " in reason, "the probe ran and failed; its exit code is the answer"
        assert "symbolic ref" in reason, "git's own words are the cause, not a restatement of the argv"


class TestAQuoteIsRedactedBeforeItIsClipped:
    """`bounded` caps a quote at 400 chars, and the ORDER of that against the scrub decides a leak.

    Clipping first severs the `@` a userinfo pattern needs and the value a pair
    pattern needs, so the scrub applied to the assembled refusal afterwards matches
    nothing and the surviving PREFIX of a token is published. Git's stderr is
    quoted through this function, and git echoes the URL it failed on.
    """

    @staticmethod
    def _stderr_clipped_inside_the_userinfo() -> str:
        lead = "fatal: unable to access "
        url = f"https://{_FAKE_TOKEN_VALUE}:x-oauth-basic@host.invalid/o/r.git"
        # Pad so the 400-char cap lands INSIDE the userinfo: the token's prefix is
        # kept and the `@` that ends it is cut away.
        padding = git_probes._MAX_QUOTED_CHARS - len(lead) - len(f"https://{_FAKE_TOKEN_VALUE}")
        return f"remote: {'x' * (padding - len('remote: '))}\n{lead}{url}: 403"

    def test_a_clip_landing_inside_a_userinfo_publishes_nothing(self) -> None:
        quoted = git_probes.bounded("; ".join(self._stderr_clipped_inside_the_userinfo().splitlines()))
        assert _FAKE_TOKEN_VALUE not in quoted
        # Control: a clip-then-redact order leaves exactly this prefix behind, so a
        # bare "the whole token is absent" assertion would pass on the broken order.
        assert _FAKE_TOKEN_VALUE[:8] not in quoted, "the clip severed the `@` and published the token's prefix"
        assert "…" in quoted, "the control: this input really is long enough to be clipped"
        assert len(quoted) <= git_probes._MAX_QUOTED_CHARS + 1, "redacting first must not lift the cap"

    def test_a_clip_landing_inside_a_named_value_publishes_nothing(self) -> None:
        lead = "fatal: unable to access "
        url = f"https://host.invalid/o/r.git?private_token={_FAKE_TOKEN_VALUE}"
        padding = git_probes._MAX_QUOTED_CHARS - len(lead) - len(url) + 6
        quoted = git_probes.bounded(f"{'x' * padding}{lead}{url}: 403")
        assert _FAKE_TOKEN_VALUE not in quoted
        assert _FAKE_TOKEN_VALUE[:8] not in quoted, "the clip severed the value and published its prefix"
        assert "…" in quoted, "the control: this input really is long enough to be clipped"

    def test_a_quote_short_enough_to_survive_the_cap_is_unchanged_apart_from_the_scrub(self) -> None:
        """The control on both: `bounded` is still a cap, not a rewrite."""
        assert git_probes.bounded("fatal: not a git repository") == "fatal: not a git repository"


class TestNoCredentialSurvivesIntoARefusal:
    """A credential must be absent from the WHOLE refusal, not from one clause of it.

    ``spec.remote`` is the push's first operand, which git accepts as a full URL,
    so a query-string credential reaches the headline, the probe argv and the
    remedy without passing through a probe. Scrubbing git's stderr alone left the
    same token redacted in one clause and verbatim in two others, one sentence
    apart — which is why the redaction is now applied to the assembled refusal.
    """

    @staticmethod
    def _url(parameter: str) -> str:
        return f"https://host.invalid/acme/app.git?{parameter}={_FAKE_TOKEN_VALUE}"

    @staticmethod
    def _userinfo_url() -> str:
        """Git's own HTTPS credential form, which this repo's CI uses to push."""
        return f"https://oauth2:{_FAKE_TOKEN_VALUE}@host.invalid/acme/app.git?ref=main"

    @staticmethod
    def _pat_url() -> str:
        """GitHub's documented PAT push URL: the TOKEN is the Basic-auth USERNAME."""
        return f"https://{_FAKE_TOKEN_VALUE}:x-oauth-basic@host.invalid/acme/app.git?ref=main"

    def test_a_userinfo_credential_in_the_pushed_url_reaches_no_clause_of_the_refusal(self, work: Path) -> None:
        _git(work, "checkout", "-q", "-b", "ac/brand-new")
        reason = _refusal_reason(f"git push {self._userinfo_url()} ac/brand-new", work)
        assert _FAKE_TOKEN_VALUE not in reason, "`user:token@` is the git credential form, not just a query string"
        # Controls: a refusal quoting nothing, or everything, would pass the line above.
        assert "<redacted>@host.invalid" in reason, "the host after the userinfo must survive, so the refusal is usable"
        assert "host.invalid" in reason, "the host must survive the scrub"
        assert "ac/brand-new" in reason, "the branch must survive the scrub"
        assert "?ref=<redacted>" in reason, "the parameter NAME must survive, so the reader sees what went"

    def test_a_pat_carried_as_the_basic_auth_username_reaches_no_clause_of_the_refusal(self, work: Path) -> None:
        """The username half is not safe to keep: on `http(s)` git reads it as the credential.

        `https://<TOKEN>:x-oauth-basic@github.com/o/r.git` is GitHub's own
        documented PAT push URL, so appending `:x-oauth-basic` was the whole cost
        of flipping a redacted URL into a leaked one.
        """
        _git(work, "checkout", "-q", "-b", "ac/brand-new")
        reason = _refusal_reason(f"git push {self._pat_url()} ac/brand-new", work)
        assert _FAKE_TOKEN_VALUE not in reason, "git reads the userinfo USERNAME as the Basic-auth credential"
        assert "x-oauth-basic" not in reason, "the throwaway password half names the form and goes with it"
        # Controls: a refusal quoting nothing would pass both lines above.
        assert "<redacted>@host.invalid" in reason, "the host after the userinfo must survive"
        assert "ac/brand-new" in reason
        assert "?ref=<redacted>" in reason

    @pytest.mark.parametrize(
        "url_of",
        [
            pytest.param(lambda s: s._userinfo_url(), id="user:token@"),
            pytest.param(lambda s: s._pat_url(), id="pat-as-the-username"),
        ],
    )
    def test_the_no_repository_refusal_redacts_every_userinfo_occurrence(self, tmp_path: Path, url_of) -> None:
        """Rung 0 quotes the remote four times: the headline, the body, and both remedy forms."""
        outside = tmp_path / "no-repo-here"
        outside.mkdir()
        reason = _refusal_reason(f"git push {url_of(self)} ac/brand-new", outside)
        assert _FAKE_TOKEN_VALUE not in reason
        quoted = reason.count("https://")
        assert quoted == 4, f"rung 0 should quote the remote four times, not {quoted}"
        assert reason.count("https://<redacted>@host.invalid") == quoted, "one unredacted occurrence is a leak"

    @pytest.mark.parametrize(
        ("value", "leaked_before"),
        [
            pytest.param("YWJj/ZGVmZw==", "ZGVmZw", id="base64-with-a-slash"),
            pytest.param("aB3/xY9+Qw==", "+Qw==", id="base64-with-a-slash-and-a-plus"),
            pytest.param("aB3%2FxY9:Qw==", "Qw==", id="percent-escape-and-a-colon"),
        ],
    )
    def test_a_credential_whose_own_value_looks_like_a_pair_boundary_is_redacted_whole(
        self, tmp_path: Path, value: str, leaked_before: str
    ) -> None:
        """Base64's alphabet puts `/`, `+` and `=` INSIDE a secret, so a boundary read there splits it.

        Reading the next `name=` as the end of a CREDENTIAL value published the
        tail: `?client_secret=aB3/xY9+Qw==` redacted to `<redacted>+Qw==`, in all
        four places rung 0 quotes the remote.
        """
        outside = tmp_path / "no-repo-here"
        outside.mkdir()
        url = f"https://host.invalid/acme/app.git?private_token={value}"
        reason = _refusal_reason(f"git push {url} ac/brand-new", outside)
        assert value not in reason
        assert leaked_before not in reason, "a boundary inside the value published its tail"
        assert reason.count("?private_token=<redacted>") == 4, "every one of rung 0's four quotes must be redacted"

    @pytest.mark.parametrize(
        "parameter",
        ["ssh_keys", "passwords", "signatures", "api_keys", "passphrases", "sessions", "jwts", "pats", "bearers"],
    )
    def test_the_plural_of_a_credential_name_is_redacted_like_its_singular(
        self, tmp_path: Path, parameter: str
    ) -> None:
        """`secrets`/`credentials`/`tokens` were listed and these were not, so each leaked verbatim."""
        outside = tmp_path / "no-repo-here"
        outside.mkdir()
        url = f"https://host.invalid/acme/app.git?{parameter}={_FAKE_TOKEN_VALUE}"
        reason = _refusal_reason(f"git push {url} ac/brand-new", outside)
        assert _FAKE_TOKEN_VALUE not in reason
        assert reason.count(f"?{parameter}=<redacted>") == 4

    def test_a_percent_encoded_credential_name_is_decoded_before_it_is_classified(self, tmp_path: Path) -> None:
        """`?%70%61%73%73%77%6F%72%64=` spells `password` and matched no name pattern at all."""
        outside = tmp_path / "no-repo-here"
        outside.mkdir()
        url = f"https://host.invalid/acme/app.git?%70%61%73%73%77%6F%72%64={_FAKE_TOKEN_VALUE}"
        reason = _refusal_reason(f"git push {url} ac/brand-new", outside)
        assert _FAKE_TOKEN_VALUE not in reason
        assert reason.count("?%70%61%73%73%77%6F%72%64=<redacted>") == 4, "the original spelling is what is printed"

    def test_a_credential_in_the_pushed_url_reaches_no_clause_of_the_refusal(self, work: Path) -> None:
        _git(work, "checkout", "-q", "-b", "ac/brand-new")
        reason = _refusal_reason(f"git push {self._url('private_token')} ac/brand-new", work)
        assert _FAKE_TOKEN_VALUE not in reason, "the pushed URL is quoted in the headline and the probe argv"
        # Controls: a refusal quoting nothing, or everything, would pass the line above.
        assert "private_token=<redacted>" in reason, "the parameter name is kept so the reader sees what went"
        assert "host.invalid" in reason, "the rest of the cause must survive the scrub"
        assert "ac/brand-new" in reason, "the branch must survive the scrub"

    def test_the_no_repository_refusal_redacts_every_occurrence_and_not_just_the_first(self, tmp_path: Path) -> None:
        """Rung 0 quotes the remote four times: the headline, the body, and both remedy forms."""
        outside = tmp_path / "no-repo-here"
        outside.mkdir()
        reason = _refusal_reason(f"git push {self._url('private_token')} ac/brand-new", outside)
        assert _FAKE_TOKEN_VALUE not in reason
        quoted = reason.count("?private_token=")
        assert quoted == 4, f"rung 0 should quote the remote four times, not {quoted}"
        assert reason.count("?private_token=<redacted>") == quoted, "one unredacted occurrence is a leak"

    @pytest.mark.parametrize(
        "parameter",
        [
            "password",
            "client_secret",
            "api_key",
            "apikey",
            "key",
            "secret",
            "sig",
            "X-Amz-Signature",
            "pat",
            "auth",
            "job_token",
            "PRIVATE-TOKEN",
            "accessToken",
            "wombat_grommet",
            "ssh_keys",
        ],
    )
    def test_a_credential_named_parameter_is_redacted_whatever_its_spelling(self, work: Path, parameter: str) -> None:
        """The redaction is scoped by what a name MEANS, not by an enumeration of three names."""
        _git(work, "checkout", "-q", "-b", "ac/brand-new")
        reason = _refusal_reason(f"git push {self._url(parameter)} ac/brand-new", work)
        assert _FAKE_TOKEN_VALUE not in reason
        assert f"{parameter}=<redacted>" in reason

    def test_a_parameter_naming_no_credential_keeps_its_name_and_loses_its_value(self, work: Path) -> None:
        """The control on the two tests above: this is a redaction, not a blanket erasure.

        There is no allowlist, so `ref` is redacted like any other name — but the
        NAME, the host, the path and the branch all survive, which is what keeps
        the refusal actionable.
        """
        _git(work, "checkout", "-q", "-b", "ac/brand-new")
        reason = _refusal_reason("git push https://host.invalid/acme/app.git?ref=main ac/brand-new", work)
        assert "?ref=<redacted>" in reason
        assert "host.invalid/acme/app.git" in reason
        assert "ac/brand-new" in reason

    def test_a_credential_in_the_configured_remotes_url_is_scrubbed_out_of_gits_stderr(self, work: Path) -> None:
        """The other route in: git echoes the URL it failed on, sanitizing only `user:token@`."""
        _git(work, "checkout", "-q", "-b", "ac/brand-new")
        _git(work, "remote", "set-url", "origin", self._url("private_token"))
        reason = _refusal_reason("git push origin ac/brand-new", work)
        assert _FAKE_TOKEN_VALUE not in reason
        assert "private_token=<redacted>" in reason
        assert "host.invalid" in reason


class TestALoneUserinfoIsACredentialInTheLiveRefusal:
    r"""`https://<token>@host` has no colon, and git reads it as the Basic-auth USERNAME.

    Measured: `printf 'url=https://<token>@host\n\n' | git credential fill` answers
    `could not read Password for 'https://<token>@host'` — the token IS the
    username, so the round-3 comment "the username is kept: it is not the secret"
    was a false premise, and the `ssh://git@host` row above pinned the un-redacted
    behaviour rather than guarding against it. Four verbatim occurrences reached
    the rung-0 refusal at the previous head.
    """

    @staticmethod
    def _lone_userinfo_url() -> str:
        return f"https://{_FAKE_TOKEN_VALUE}@host.invalid/acme/app.git?ref=main"

    def test_a_lone_userinfo_reaches_no_clause_of_the_refusal(self, work: Path) -> None:
        _git(work, "checkout", "-q", "-b", "ac/brand-new")
        reason = _refusal_reason(f"git push {self._lone_userinfo_url()} ac/brand-new", work)
        assert _FAKE_TOKEN_VALUE not in reason, "git reads a lone userinfo as the credential, not as a login"
        # Controls: a refusal quoting nothing, or everything, would pass the line above.
        assert "https://<redacted>@" in reason, "the whole userinfo goes; there is no username half to keep"
        assert "host.invalid" in reason, "the host must survive the scrub"
        assert "ac/brand-new" in reason, "the branch must survive the scrub"
        assert "?ref=<redacted>" in reason, "the parameter NAME must survive, so the reader sees what went"

    def test_the_no_repository_refusal_redacts_every_lone_userinfo_occurrence(self, tmp_path: Path) -> None:
        """Rung 0 quotes the remote four times: the headline, the body, and both remedy forms."""
        outside = tmp_path / "no-repo-here"
        outside.mkdir()
        reason = _refusal_reason(f"git push {self._lone_userinfo_url()} ac/brand-new", outside)
        assert _FAKE_TOKEN_VALUE not in reason
        quoted = reason.count("https://")
        assert quoted == 4, f"rung 0 should quote the remote four times, not {quoted}"
        assert reason.count("https://<redacted>@") == quoted, "one unredacted occurrence is a leak"

    def test_a_remote_helper_prefix_does_not_hide_the_scheme(self, tmp_path: Path) -> None:
        """`git::https://<token>@host` is a valid push operand and leaked identically."""
        outside = tmp_path / "no-repo-here"
        outside.mkdir()
        reason = _refusal_reason(f"git push git::{self._lone_userinfo_url()} ac/brand-new", outside)
        assert _FAKE_TOKEN_VALUE not in reason
        assert "git::https://<redacted>@" in reason

    def test_a_scheme_whose_userinfo_is_a_login_stays_readable(self, tmp_path: Path) -> None:
        """The control on the three above: scheme-scoping is what keeps `ssh://git@` usable."""
        outside = tmp_path / "no-repo-here"
        outside.mkdir()
        reason = _refusal_reason("git push ssh://git@host.invalid/acme/app.git ac/brand-new", outside)
        assert "ssh://git@host.invalid" in reason
        assert "<redacted>" not in reason


class TestTheDiagnosticDidNotWeakenTheGuard:
    """Anti-vacuity controls: each refusal below must be the GATE's, not the fixture's.

    Every case is re-run with the owner's kill switch flipped. If the push is
    still refused with the gate OFF, the assertion above it proved nothing.
    """

    @staticmethod
    def _with_gate_off(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            router,
            "_teatree_bool_setting",
            lambda key, default=True: False if key == gate.GATE_SETTING else default,
        )

    def test_a_colleagues_open_mr_is_still_refused_and_only_the_gate_refuses_it(
        self, work: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _branch_pushed_by(work, "1234-feat-search", email=THEIR_EMAIL, author="colleague-login")
        _git(work, "checkout", "-q", "-b", "1234-feat-search", "origin/1234-feat-search")
        forge = _forge(mr_rows=_mr("colleague-login"))
        with patch.object(gate, "_forge_seam", return_value=forge):
            blocked, payload = _run("git push origin 1234-feat-search", work)
        assert blocked is True
        assert payload is not None
        assert "colleague-login" in payload["permissionDecisionReason"]

        self._with_gate_off(monkeypatch)
        with patch.object(gate, "_forge_seam", return_value=forge):
            unguarded, unguarded_payload = _run("git push origin 1234-feat-search", work)
        assert unguarded is False, "the refusal above came from the fixture, not the gate"
        assert unguarded_payload is None

    def test_a_colleagues_branch_still_has_no_force_push_escape(self, work: Path) -> None:
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        command = "git push --force origin theirs  # [scope-push-ok: vetted] [fp-confirmed: seen it]"
        with (
            patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("colleague-login"))),
            patch.object(router, "_danger_gate_fail_open_enabled", return_value=True),
        ):
            blocked, payload = _run(command, work)
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"

    def test_an_unpinnable_work_dir_is_still_refused_and_only_the_gate_refuses_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outside = tmp_path / "no-repo-here"
        outside.mkdir()
        blocked, _payload = _run("git push origin ac/brand-new", outside)
        assert blocked is True

        self._with_gate_off(monkeypatch)
        unguarded, unguarded_payload = _run("git push origin ac/brand-new", outside)
        assert unguarded is False, "the refusal above came from the fixture, not the gate"
        assert unguarded_payload is None


class TestWhatThePushArgvActuallyNames:
    """Every shape the parser read wrong, in the direction each one matters.

    THREE were false ALLOWs, measured against a local bare repo with
    `receive.advertisePushOptions=true` (without it the file transport rejects
    push options and the writing shapes are indistinguishable from the control),
    deleting `refs/heads/colleagues-branch` between runs under git 2.50.1:

        git push -o -n origin colleagues-branch                        LANDED
        git push --push-option --help --force origin colleagues-branch LANDED
        git push --push-opt --dry-run origin colleagues-branch         LANDED
        git push origin colleagues-branch --                           LANDED
        git push -n origin colleagues-branch          (control)        no ref

    The first three yielded NO spec at all, so the gate never ran; the fourth
    yielded `remote='origin', refspecs=()`, which judges the agent's own current
    branch instead of the colleague's. The rest are false DENIES on ordinary work.
    """

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push -o -n origin colleagues-branch", id="a-short-option-eats-the-dry-run"),
            pytest.param(
                "git push --push-option --help --force origin colleagues-branch", id="a-long-option-eats-the-help"
            ),
            pytest.param("git push --push-opt --dry-run origin colleagues-branch", id="a-unique-abbreviation"),
            pytest.param("git push -qo VAL origin colleagues-branch", id="a-short-cluster-ending-in-o"),
        ],
    )
    def test_a_value_flags_argument_is_not_a_flag(self, command: str, work: Path) -> None:
        specs = gate.push_specs(command, str(work)).specs
        assert [(spec.remote, spec.refspecs) for spec in specs] == [("origin", ("colleagues-branch",))]

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push origin colleagues-branch --", id="trailing"),
            pytest.param("git push origin -- colleagues-branch", id="between-the-operands"),
        ],
    )
    def test_a_double_dash_keeps_the_operands_before_it(self, command: str, work: Path) -> None:
        specs = gate.push_specs(command, str(work)).specs
        assert [(spec.remote, spec.refspecs) for spec in specs] == [("origin", ("colleagues-branch",))]

    def test_a_double_dash_at_a_flag_position_is_still_end_of_options(self, work: Path) -> None:
        """The control: git makes `--force` the REMOTE here, so this is no force push."""
        specs = gate.push_specs("git push -- --force origin colleagues-branch", str(work)).specs
        assert [(spec.remote, spec.refspecs, spec.force) for spec in specs] == [
            ("--force", ("origin", "colleagues-branch"), False)
        ]

    @pytest.mark.parametrize(
        ("command", "remote", "refspecs", "force"),
        [
            pytest.param("git push --signed true origin main", "true", ("origin", "main"), False, id="signed"),
            pytest.param("git push --force-with-lease origin main", "origin", ("main",), True, id="force-with-lease"),
            pytest.param("git push -oX origin main", "origin", ("main",), False, id="a-glued-short-value"),
        ],
    )
    def test_a_flag_that_takes_no_value_leaves_the_operand_alone(
        self, command: str, remote: str, refspecs: tuple[str, ...], work: Path, *, force: bool
    ) -> None:
        specs = gate.push_specs(command, str(work)).specs
        assert [(spec.remote, spec.refspecs, spec.force) for spec in specs] == [(remote, refspecs, force)]

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push --recurse-submodules on-demand origin colleagues-branch", id="space-form"),
            pytest.param("git push --recurse-submodules=no origin colleagues-branch", id="the-control-glued-form"),
        ],
    )
    def test_recurse_submodules_names_no_remote(self, command: str, work: Path) -> None:
        specs = gate.push_specs(command, str(work)).specs
        assert [(spec.remote, spec.refspecs) for spec in specs] == [("origin", ("colleagues-branch",))]

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push --repo https://h.invalid/o/r.git", id="separate-value"),
            pytest.param("git push --repo=https://h.invalid/o/r.git", id="glued-value"),
        ],
    )
    def test_a_repo_flag_names_the_remote(self, command: str, work: Path) -> None:
        specs = gate.push_specs(command, str(work)).specs
        assert [spec.remote for spec in specs] == ["https://h.invalid/o/r.git"]
        assert specs[0].refspecs == (), "the flag's value is the remote, never a refspec"

    def test_an_operand_still_outranks_the_repo_flag(self, work: Path) -> None:
        """Git's own precedence: `--repo` applies only when no repository argument is given."""
        specs = gate.push_specs("git push origin main --repo https://h.invalid/o/r.git", str(work)).specs
        assert [(spec.remote, spec.refspecs) for spec in specs] == [("origin", ("main",))]

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push --help", id="long-help"),
            pytest.param("git push -h", id="short-help"),
            pytest.param("git push --dry-run origin main", id="the-control-dry-run"),
        ],
    )
    def test_a_push_that_writes_nothing_yields_no_spec(self, command: str, work: Path) -> None:
        assert gate.push_specs(command, str(work)).specs == ()

    def test_a_real_push_still_yields_one(self, work: Path) -> None:
        """The control on the row above: the parser did not simply stop finding pushes."""
        assert len(gate.push_specs("git push origin main", str(work)).specs) == 1

    @pytest.mark.parametrize(
        ("command", "remote", "refspecs"),
        [
            pytest.param("git push 2>/dev/null", "origin", (), id="stderr-to-devnull"),
            pytest.param("git push origin main 2>&1", "origin", ("main",), id="stderr-onto-stdout"),
            pytest.param("git push 2origin main", "2origin", ("main",), id="the-control-a-remote-starting-with-2"),
        ],
    )
    def test_a_redirection_file_descriptor_is_not_an_operand(
        self, command: str, remote: str, refspecs: tuple[str, ...], work: Path
    ) -> None:
        specs = gate.push_specs(command, str(work)).specs
        assert [(spec.remote, spec.refspecs) for spec in specs] == [(remote, refspecs)]


class TestAGluedPushOptionCannotBlindTheGate:
    """`git push -f -oskip.notify origin victim` LANDED a force-push past the gate.

    Measured under git 2.50.1 against a bare repo with
    `receive.advertisePushOptions=true`, a `pre-receive` hook echoing
    `GIT_PUSH_OPTION_<n>`, and a fresh branch per run:

        -oskip.notify   `-o` with value `skip.notify`   PUSHOPT_0=[skip.notify], ref landed
        -fovalue.a      `-f` AND `-o value.a`           PUSHOPT_0=[value.a],     ref landed
        -of             `-o` with value `f`             PUSHOPT_0=[f],           ref landed
        -fo value.c     `-f` AND `-o value.c`           PUSHOPT_0=[value.c],     ref landed
        -fvalue         cluster -f -v -a                error: unknown switch `a', rc 129, no push
        -nf             dry-run + force                 prints, writes nothing

    So a short-option token is a CLUSTER whose letters are consumed left to right,
    and on `o` — git push's ONLY short option taking a required argument — the rest
    of the token is that option's value. Reading the value's letters as flags made
    any `n` in it a dry run, so the gate yielded no spec at all and never ran.
    """

    def test_a_glued_short_option_value_does_not_land_a_force_push(self, work: Path) -> None:
        """The refusal is measured on the REMOTE REF, not on a parse: the push really runs."""
        _advertising_push_options(work)
        before = _diverged_victim(work)
        command = "git push -f -oskip.notify origin victim"
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run(command, work)
        if not blocked:
            _really_run(command, work)
        assert _remote_victim(work) == before, "the colleague's branch was force-updated past the gate"
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"

    def test_a_genuine_dry_run_is_allowed_and_still_writes_nothing(self, work: Path) -> None:
        """CONTROL: without it, a gate that refused everything would pass the row above."""
        _advertising_push_options(work)
        before = _diverged_victim(work)
        command = "git push -f -n origin victim"
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, _payload = _run(command, work)
        _really_run(command, work)
        assert blocked is False
        assert _remote_victim(work) == before

    def test_the_same_push_one_token_apart_is_refused(self, work: Path) -> None:
        """CONTROL: `-f` alone denies even pre-fix, so the harness is wired to a gate that can refuse."""
        _advertising_push_options(work)
        _diverged_victim(work)
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run("git push -f origin victim", work)
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"

    def test_git_glues_a_short_options_value(self, work: Path) -> None:
        """The PREMISE the expansion rests on, pinned against real git rather than quoted."""
        origin = _advertising_push_options(work)
        hook = origin / "hooks" / "pre-receive"
        hook.write_text(
            '#!/bin/sh\necho "SEEN_0=[${GIT_PUSH_OPTION_0}] COUNT=[${GIT_PUSH_OPTION_COUNT}]" >&2\nexit 0\n',
            encoding="utf-8",
        )
        hook.chmod(0o755)
        _git(work, "checkout", "-q", "-b", "glued", "main")
        _commit(work, "glued.txt", email=OUR_EMAIL, author="Us")
        proc = _really_run("git push -oskip.notify origin glued", work)
        assert "SEEN_0=[skip.notify] COUNT=[1]" in proc.stderr, proc.stderr

    @pytest.mark.parametrize("value", ["n", "notify", "run", "info", "integrations.skip", "skip.notify", "X"])
    def test_a_glued_value_is_a_value_and_never_a_flag(self, value: str, work: Path) -> None:
        """`X` is the round-9 pin, kept beside its siblings: the fix must not merely move the blind spot."""
        specs = gate.push_specs(f"git push -o{value} origin main", str(work)).specs
        assert [(spec.remote, spec.refspecs, spec.force) for spec in specs] == [("origin", ("main",), False)]

    def test_a_glued_value_of_f_is_not_a_force(self, work: Path) -> None:
        specs = gate.push_specs("git push -of origin main", str(work)).specs
        assert [spec.force for spec in specs] == [False]

    def test_the_control_a_bare_f_is_a_force(self, work: Path) -> None:
        specs = gate.push_specs("git push -f origin main", str(work)).specs
        assert [spec.force for spec in specs] == [True]

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push -fovalue.a origin main", id="glued"),
            pytest.param("git push -fo value.a origin main", id="the-control-separated"),
        ],
    )
    def test_a_letter_before_the_value_letter_still_counts(self, command: str, work: Path) -> None:
        specs = gate.push_specs(command, str(work)).specs
        assert [(spec.remote, spec.refspecs, spec.force) for spec in specs] == [("origin", ("main",), True)]

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push -oo origin main", id="glued"),
            pytest.param("git push -o o origin main", id="the-control-separated"),
        ],
    )
    def test_a_glued_value_of_o_eats_no_operand(self, command: str, work: Path) -> None:
        specs = gate.push_specs(command, str(work)).specs
        assert [(spec.remote, spec.refspecs) for spec in specs] == [("origin", ("main",))]

    @pytest.mark.parametrize("command", ["git push -nf origin main", "git push -fn origin main"])
    def test_a_dry_run_letter_anywhere_in_a_cluster_still_writes_nothing(self, command: str, work: Path) -> None:
        assert gate.push_specs(command, str(work)).specs == ()


class TestWhatTheReposOwnConfigSaysAPushWrites:
    """A refspec-less push writes what `remote.<remote>.push` and `push.default` say.

    Measured against a seeded remote holding `mine` and `victim`, both local and both
    ahead, under git 2.50.1:

        -c push.default=matching                        writes mine AND victim
        -c remote.origin.push=refs/heads/*:refs/heads/* writes mine, victim, and creates main
        -c push.default=current                         writes mine only  (the control)

    The gate resolved one branch through `@{u}` and allowed the rest unexamined. And
    with `mine` tracking `origin/other`, `@{u}` names the branch git does NOT write:
    `current` writes `mine` while `upstream`/`simple` write `other`, so an
    unconditional `@{u}` both refuses on a branch git leaves alone and allows a write
    to one it never checked.
    """

    @staticmethod
    def _two_branches(work: Path) -> None:
        """`mine` (ours) and `victim` (a colleague's), both local and both on the remote."""
        _branch_pushed_by(work, "victim", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "victim", "origin/victim")
        _git(work, "checkout", "-q", "-b", "mine", "main")
        _commit(work, "mine.txt", email=OUR_EMAIL, author="Us")
        _git(work, "push", "-q", "-u", "origin", "mine")

    @staticmethod
    def _crossed_upstream(work: Path) -> None:
        """`mine`, absent from the remote, tracking the colleague's `other`."""
        _branch_pushed_by(work, "other", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "mine", "main")
        _git(work, "branch", "--set-upstream-to=origin/other", "mine")

    def test_a_matching_push_is_refused_because_it_names_no_branch(self, work: Path) -> None:
        self._two_branches(work)
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run("git -c push.default=matching push origin", work)
        assert blocked is True
        assert payload is not None
        assert "push.default" in payload["permissionDecisionReason"]

    def test_the_control_a_current_push_names_one_branch_and_is_allowed(self, work: Path) -> None:
        """Without it, a policy that refused every refspec-less push would pass the row above."""
        self._two_branches(work)
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, _payload = _run("git -c push.default=current push origin", work)
        assert blocked is False

    def test_a_wildcard_remote_push_refspec_is_unpinnable(self, work: Path) -> None:
        self._two_branches(work)
        command = "git -c 'remote.origin.push=refs/heads/*:refs/heads/*' push origin"
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, payload = _run(command, work)
        assert blocked is True
        assert payload is not None
        assert "remote.origin.push" in payload["permissionDecisionReason"]

    def test_the_control_a_named_remote_push_refspec_resolves_to_its_branch(self, work: Path) -> None:
        """The rung reads the VALUE; refusing on the key's mere presence would pass the row above."""
        self._two_branches(work)
        command = "git -c 'remote.origin.push=refs/heads/mine:refs/heads/mine' push origin"
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, _payload = _run(command, work)
        assert blocked is False

    def test_push_default_current_judges_the_local_name(self, work: Path) -> None:
        self._crossed_upstream(work)
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, _payload = _run("git -c push.default=current push origin", work)
        assert blocked is False, "git writes `mine`, which is absent from the remote and ours to create"

    def test_the_control_push_default_upstream_judges_the_tracked_name(self, work: Path) -> None:
        """The mirror row: neither direction alone pins the mapping."""
        self._crossed_upstream(work)
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])):
            blocked, _payload = _run("git -c push.default=upstream push origin", work)
        assert blocked is True

    def test_an_unreadable_config_probe_refuses(self, work: Path) -> None:
        real = git_probes.git_probe

        def unreadable(work_dir: str, *args: str, **kwargs: object) -> git_probes.GitProbe:
            if "config" in args:
                return git_probes.GitProbe(ok=False, out="", code=128, err="fatal: bad config line 1")
            return real(work_dir, *args, **kwargs)

        with (
            patch.object(git_probes, "git_probe", unreadable),
            patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=[])),
        ):
            blocked, payload = _run("git push", work)
        assert blocked is True
        assert payload is not None
        assert "exit 128: fatal: bad config line 1" in payload["permissionDecisionReason"]

    def test_the_control_an_unset_key_is_gits_own_built_in_default(self, work: Path) -> None:
        """`git config --get` exits 1 for a key nobody set: that is an ANSWER, not a failure."""
        unset = git_probes.GitProbe(ok=False, out="", code=1, err="")
        with patch.object(git_probes, "git_probe", return_value=unset):
            policy = git_probes.push_target_policy(str(work), "origin", ())
        assert policy.unpinnable == ""
        assert policy.prefer_upstream is True

    def test_the_config_overrides_reach_the_probe(self, work: Path) -> None:
        """The `-c` pairs are walked past by the parser; a probe blind to them misses the attack."""
        specs = gate.push_specs("git -c push.default=matching -C . push origin", str(work)).specs
        assert [spec.config_overrides for spec in specs] == [("push.default=matching",)]


class TestAnAbbreviatedFlagIsReadTheWayItsSetPoints:
    """Prefix-match the sets whose membership makes the gate refuse MORE; exact-match the rest.

    Git accepts unique abbreviations, measured under 2.50.1:

        --al -> --all   --mir -> --mirror   --bran -> --all (--branches is its alias)
        --force-w -> --force-with-lease (rc 0, push proceeds)   --dry -> --dry-run (rc 0)
        --forc -> AMBIGUOUS, rc 129, no push

    The gate did not, and one blanket rule is what made that a hole rather than a
    trade: reading MORE tokens as `--all` refuses more, while reading more as
    `--dry-run` blinds the gate outright.

    Accepted over-refusal: `--f`/`--fo`/`--forc` are ambiguous to git (rc 129, no
    push) and read as force here. A spurious refusal on a command git would never
    run costs a confusing message and nothing else.
    """

    @pytest.mark.parametrize("flag", ["--al", "--mir", "--bran", "--all", "--mirror", "--branches"])
    def test_a_flag_that_names_no_branch_is_unpinnable_however_it_is_abbreviated(self, flag: str, work: Path) -> None:
        specs = gate.push_specs(f"git push {flag} origin", str(work)).specs
        assert [spec.unpinnable for spec in specs] != [""], f"`{flag}` named no branch and was not refused"

    @pytest.mark.parametrize("flag", ["--d", "--dr", "--dry"])
    def test_an_abbreviated_dry_run_is_not_read_as_one(self, flag: str, work: Path) -> None:
        """The row that keeps the per-set rule from being flattened back into a blanket one.

        Prefix-matching `_DRY_RUN_FLAGS` would make this yield NO spec, which is the
        exact bypass the gate exists to close.
        """
        specs = gate.push_specs(f"git push {flag} origin colleagues-branch", str(work)).specs
        assert [(spec.remote, spec.refspecs) for spec in specs] == [("origin", ("colleagues-branch",))]

    @pytest.mark.parametrize("flag", ["--force-w", "--force-with-lease", "--forc", "-f"])
    def test_an_abbreviated_force_takes_the_no_escape_path(self, flag: str, work: Path) -> None:
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("colleague-login"))):
            route = _deny_route(f"git push {flag} origin theirs", work)
        assert route == "emit_pretooluse_deny"

    def test_the_control_a_non_force_foreign_push_routes_through_the_shared_chain(self, work: Path) -> None:
        """Without it, a handler that always took the no-escape path would pass the row above."""
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("colleague-login"))):
            route = _deny_route("git push origin theirs", work)
        assert route == "_fail_open_or_deny"


class TestDeletingAColleaguesBranchHasNoEscapeEither:
    """A delete destroys strictly more than a force: the branch and its reflog both go.

    Measured at the pre-fix head, branch resolution correct in all three and only
    `force` wrong, so a delete routed through the escapable chain while a rewrite
    did not:

        git push origin :victim         refspecs=(':victim',)  force=False
        git push --delete origin victim refspecs=('victim',)   force=False
        git push -d origin victim       refspecs=('victim',)   force=False
        git push --force origin victim  force=True   (control)
        git push origin +victim         force=True   (control)
    """

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push --delete origin theirs", id="long"),
            pytest.param("git push -d origin theirs", id="short"),
            pytest.param("git push --del origin theirs", id="abbreviated"),
            pytest.param("git push origin :theirs", id="an-empty-source-side"),
        ],
    )
    def test_a_delete_takes_the_no_escape_path(self, command: str, work: Path) -> None:
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("colleague-login"))):
            route = _deny_route(command, work)
        assert route == "emit_pretooluse_deny"

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push --delete origin theirs", id="long"),
            pytest.param("git push origin :theirs", id="an-empty-source-side"),
        ],
    )
    def test_a_delete_still_names_the_branch_it_deletes(self, command: str, work: Path) -> None:
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("colleague-login"))):
            reason = _refusal_reason(command, work)
        assert "theirs" in reason
        assert " --force" not in reason, "a plain delete is no force push, and the prose may not say it is"

    def test_the_control_a_force_still_says_force(self, work: Path) -> None:
        _branch_pushed_by(work, "theirs", email=THEIR_EMAIL, author="Colleague")
        _git(work, "checkout", "-q", "-b", "theirs", "origin/theirs")
        with patch.object(gate, "_forge_seam", return_value=_forge(mr_rows=_mr("colleague-login"))):
            reason = _refusal_reason("git push --force origin theirs", work)
        assert " --force" in reason


# Every probe description the gate can name, DERIVED from the modules that build them.
# Transcribing them was vacuous: the reviewers appended `(mode=strict)` to the
# `push_work_dir` fallback in production, the assembled refusal self-redacted, and this
# suite stayed green — so a RED reported against a transcribed tuple can only have come
# from editing the tuple. Over-collection is harmless: every string collected must
# satisfy the same property, that naming it does not redact a word of the gate's own prose.
_PLACEHOLDER: Final[str] = "origin"
_THE_RE: Final[re.Pattern[str]] = re.compile(r"\bthe\b")
_PROBE_DESCRIPTIONS_KNOWN_TODAY: Final[int] = 14


def _probe_descriptions(source: str) -> frozenset[str]:
    """Every string constant in *source* that can reach a refusal, its interpolations filled.

    Docstrings are excluded — they quote shapes like `ok=True` that no refusal carries
    — and everything else is kept, filtered to the strings that read like prose.
    """
    tree = ast.parse(source)
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    found: set[str] = set()
    for node in ast.walk(tree):
        if node in docstrings:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
        elif isinstance(node, ast.JoinedStr):
            found.add(
                "".join(
                    part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else _PLACEHOLDER
                    for part in node.values
                )
            )
    return frozenset(text for text in found if "`" in text or _THE_RE.search(text))


_PROBE_SOURCES: Final[tuple[Path, ...]] = tuple(
    Path(module.__file__ or "") for module in (gate, argv_parse, git_probes, refusal_text)
)
_EVERY_PROBE_DESCRIPTION: Final[tuple[str, ...]] = tuple(
    sorted(frozenset().union(*(_probe_descriptions(path.read_text(encoding="utf-8")) for path in _PROBE_SOURCES)))
)
_EVERY_PROBE_RESULT = (
    git_probes.GitProbe(ok=False, out="", code=128, err="fatal: not a git repository"),
    git_probes.GitProbe(ok=False, out="", err="timed out after 12s"),
    git_probes.GitProbe(ok=False, out="", err="could not run git: no such file"),
    git_probes.GitProbe(
        ok=False,
        out="",
        code=0,
        err="an `ls-remote` that exited 0 but advertised no parsable `refs/heads/ac/x` line; it said: 'ac/x'",
    ),
)


class TestTheGateNeverRedactsItself:
    """With no allowlist, any `name=value` the gate's OWN prose acquires is redacted.

    So the gate authors none: `probe_cause` renders `exit <n>`, not `rc=<n>`. Every
    description the gate can name is enumerated above, because the clause that would
    self-redact is the one added to a probe nobody thought to exercise.
    """

    @staticmethod
    def _refusals(body: str) -> list[str]:
        return [
            refusal_text.refusal("origin", branch, body, force=force)
            for branch in ("ac/x", refusal_text.UNPINNED)
            for force in (True, False)
        ]

    def _assert_none_self_redacts(self, bodies: list[str]) -> None:
        for body in bodies:
            for text in self._refusals(body):
                assert redaction.REDACTED not in text, f"the gate redacted its own prose: {text}"

    @pytest.mark.parametrize("probe", _EVERY_PROBE_DESCRIPTION)
    def test_no_refusal_naming_a_probe_redacts_a_word_of_its_own(self, probe: str) -> None:
        self._assert_none_self_redacts(
            [
                refusal_text.unobtainable_body(probe),
                *(refusal_text.unobtainable_body(probe, result) for result in _EVERY_PROBE_RESULT),
            ]
        )

    def test_no_ownership_refusal_redacts_a_word_of_its_own(self) -> None:
        self._assert_none_self_redacts(
            [
                refusal_text.foreign_mr_body("colleague", "https://h.invalid/o/r/-/merge_requests/7"),
                refusal_text.foreign_commits_body("ac/x", ("colleague@example.com", "Colleague")),
                *(
                    refusal_text.no_repository_body("/tmp/w", "origin", "ac/x", result)
                    for result in _EVERY_PROBE_RESULT
                ),
            ]
        )

    def test_the_derived_collection_is_not_empty(self) -> None:
        """A collector that silently stopped finding strings would parametrize over nothing."""
        assert len(_EVERY_PROBE_DESCRIPTION) >= _PROBE_DESCRIPTIONS_KNOWN_TODAY

    def test_the_derived_collection_tracks_production(self) -> None:
        """A hard-coded tuple cannot follow a mutation in the source; a derived one must."""
        fallback = "the repository selected by the git global options"
        assert fallback in _EVERY_PROBE_DESCRIPTION
        source = Path(git_probes.__file__ or "").read_text(encoding="utf-8")
        mutated = _probe_descriptions(source.replace(fallback, f"{fallback} (mode=strict)", 1))
        assert f"{fallback} (mode=strict)" in mutated
        assert fallback not in mutated

    def test_the_mutation_a_transcribed_tuple_missed_does_self_redact(self) -> None:
        """So the parametrized row above goes RED on it once the collection is derived."""
        body = refusal_text.unobtainable_body("the repository selected by the git global options (mode=strict)")
        assert all(redaction.REDACTED in text for text in self._refusals(body))

    def test_the_probe_cause_rendering_is_what_keeps_it_that_way(self) -> None:
        """The control: this is the clause that would self-redact if it read `rc=<n>`."""
        cause = git_probes.probe_cause(git_probes.GitProbe(ok=False, out="", code=128, err="fatal: no repo"))
        assert cause == "exit 128: fatal: no repo"
        assert "=" not in cause


class TestNoOperandLengthCollapsesTheRefusal:
    """Every untrusted operand is bounded AT its interpolation, never after assembly.

    Measured at `294ecfb5c`: a 100000-character refspec left 176 characters — the
    cause, the remedy and `gate foreign-push disable` all gone, because the only
    bound was the scanner's cap over the ASSEMBLED text and one huge operand
    consumed it. A fail-closed gate with no per-call override that loses its own
    instruction for turning itself off refuses with nowhere to go.

    The predecessor of this class varied `branch` alone against a hardcoded small
    body, so it pinned the headline and NOTHING else: removing `bounded()` from the
    `remote` operand left the whole 285-test suite green. Each operand of each
    builder therefore gets its own row, the already-bounded ones included.
    """

    @pytest.mark.parametrize(
        ("build", "cause"),
        [
            pytest.param(
                lambda huge: refusal_text.refusal(huge, "ac/x", _UNOBTAINABLE_BODY, force=False),
                _UNOBTAINABLE_CAUSE,
                id="refusal.remote",
            ),
            pytest.param(
                lambda huge: refusal_text.refusal("origin", huge, _UNOBTAINABLE_BODY, force=False),
                _UNOBTAINABLE_CAUSE,
                id="refusal.branch",
            ),
            pytest.param(
                lambda huge: _refusal_for(refusal_text.unobtainable_body(f"the refspec `{huge}`")),
                _UNOBTAINABLE_CAUSE,
                id="unobtainable_body.probe",
            ),
            pytest.param(
                lambda huge: _refusal_for(
                    refusal_text.unobtainable_body("`git ls-remote`", _unparsable_ls_remote_probe(huge))
                ),
                "It failed with",
                id="remote_branch_oid.ref",
            ),
            pytest.param(
                lambda huge: _refusal_for(refusal_text.foreign_mr_body(huge, "https://forge.invalid/mr/1")),
                "The open MR/PR on it",
                id="foreign_mr_body.author",
            ),
            pytest.param(
                lambda huge: _refusal_for(refusal_text.foreign_mr_body("them", huge)),
                "The open MR/PR on it",
                id="foreign_mr_body.mr_ref",
            ),
            pytest.param(
                lambda huge: _refusal_for(refusal_text.foreign_commits_body(huge, ("them",))),
                "No MR backs",
                id="foreign_commits_body.branch",
            ),
            pytest.param(
                lambda huge: _refusal_for(refusal_text.foreign_commits_body("ac/x", (huge,))),
                "No MR backs",
                id="foreign_commits_body.authors",
            ),
            pytest.param(
                lambda huge: _no_repository_refusal(work_dir=huge),
                _UNOBTAINABLE_CAUSE,
                id="no_repository_body.work_dir",
            ),
            pytest.param(
                lambda huge: _no_repository_refusal(remote=huge),
                _UNOBTAINABLE_CAUSE,
                id="no_repository_body.remote",
            ),
            pytest.param(
                lambda huge: _no_repository_refusal(target=huge),
                _UNOBTAINABLE_CAUSE,
                id="no_repository_body.target",
            ),
            # `probe_cause` is the only OTHER text a probe reaches a refusal through, and
            # its three interpolations are three separate bounds. One row each, because a
            # row covering two of them leaves the third free to regress unnoticed.
            pytest.param(
                lambda huge: _refusal_for(refusal_text.unobtainable_body("`git ls-remote`", _failed_probe(huge))),
                _UNOBTAINABLE_CAUSE,
                id="unobtainable_body.probe_cause",
            ),
            pytest.param(
                lambda huge: _no_repository_refusal(probe=git_probes.GitProbe(ok=False, out="", err=huge)),
                _UNOBTAINABLE_CAUSE,
                id="no_repository_body.probe_cause_never_ran",
            ),
            pytest.param(
                lambda huge: _no_repository_refusal(probe=_failed_probe(f"not a git repository: {huge}")),
                _UNOBTAINABLE_CAUSE,
                id="no_repository_body.probe_cause_absent",
            ),
        ],
    )
    def test_an_enormous_operand_leaves_the_refusal_whole(self, build: Callable[[str], str], cause: str) -> None:
        huge = "z" * 100000
        text = build(huge)
        assert cause in text, "the cause the refusal exists to state did not survive the operand"
        assert _carries_a_remedy(text), "the refusal named no way forward"
        assert "gate foreign-push disable" in text, "the gate lost its own instruction for turning itself off"
        assert huge[: git_probes._MAX_QUOTED_CHARS + 1] not in text, "the operand was published unbounded"

    def test_small_operands_still_render_verbatim(self) -> None:
        """The control: a bound that clipped every operand would satisfy the rows above."""
        text = _refusal_for(refusal_text.foreign_mr_body("them", "https://forge.invalid/mr/1"))
        assert "ac/x" in text
        assert "`them`" in text
        assert "https://forge.invalid/mr/1" in text


class TestRedactionPrecedesEveryMutation:
    """`_one_line` joins and `bounded` clips; both must act on already-scrubbed text."""

    def test_the_join_cannot_split_a_credential(self) -> None:
        """The gate's own `"; "` join is a mutation, so it runs after the scrub, not before."""
        joined = git_probes._one_line(f"?private_token={_FAKE_TOKEN_VALUE}\nnext line")
        assert _FAKE_TOKEN_VALUE not in joined
        assert joined == "?private_token=<redacted>; next line", (
            "scrubbing the JOINED text instead eats the separator the join added"
        )

    def test_a_credential_free_stderr_joins_untouched(self) -> None:
        """The control: an over-eager scrub would fail this."""
        assert git_probes._one_line("line one\nline two") == "line one; line two"

    def test_an_over_length_quote_is_scrubbed_before_it_is_clipped(self) -> None:
        url = f"https://host.invalid/o/r.git?private_token={_FAKE_TOKEN_VALUE}"
        padding = git_probes._MAX_QUOTED_CHARS - len(url) + 6
        quoted = git_probes.bounded(f"{'x' * padding}fatal: unable to access '{url}': 403")
        assert _FAKE_TOKEN_VALUE not in quoted
        assert _FAKE_TOKEN_VALUE[:8] not in quoted, "the clip severed the value and published its prefix"
        assert "…" in quoted, "the control: this input really is long enough to be clipped"

    def test_a_short_credential_free_quote_is_byte_identical(self) -> None:
        assert git_probes.bounded("fatal: not a git repository") == "fatal: not a git repository"


class TestNothingPastTheScanCapVanishesSilently:
    """Measured at the shipped head: a 70 KB no-boundary answer rendered as no answer.

    `bounded()` returned `''` and the refusal quoting it read `exit 128 with no
    stderr` — the one wording the gate uses for a probe that said nothing at all.
    """

    def test_a_quote_past_the_scan_cap_is_marked_not_erased(self) -> None:
        assert git_probes.bounded("a" * 70000) == _TRUNCATED

    def test_a_stderr_past_the_scan_cap_is_not_reported_as_no_stderr(self) -> None:
        probe = git_probes.GitProbe(ok=False, out="", code=128, err=git_probes._one_line("a" * 70000))
        assert git_probes.probe_cause(probe) == f"exit 128: {_TRUNCATED}"

    def test_a_quote_under_the_cap_is_marked_nowhere(self) -> None:
        """The control: the marker appears only where something really was dropped."""
        assert git_probes.bounded("fatal: no repo") == "fatal: no repo"

    def test_a_userinfo_url_past_the_cap_publishes_no_prefix(self) -> None:
        url = f"https://{_FAKE_TOKEN_VALUE}{'a' * 70000}@h.invalid/o/r.git"
        assert git_probes.bounded(url) == _TRUNCATED


class TestOneLineCutsStderrToTheQuoteCap:
    """Between the two caps the scrub returns stderr whole, so this clip is the only bound.

    It reads as redundant once `probe_cause`'s interpolations clip too — measured: removing
    it left all 298 tests in this file green — and a bound nothing pins is one a later
    reader deletes.
    """

    def test_a_stderr_under_the_scan_cap_is_still_cut(self) -> None:
        assert len(git_probes._one_line("z" * 60000)) == git_probes._MAX_QUOTED_CHARS + 1

    def test_a_stderr_at_the_quote_cap_is_byte_identical(self) -> None:
        """The control: a clip off by one, or one that cut everything, would fail here."""
        at_the_cap = "z" * git_probes._MAX_QUOTED_CHARS
        assert git_probes._one_line(at_the_cap) == at_the_cap


class TestOneLineIsBoundedByTheScanCap:
    """A chatty remote's sideband is unbounded, and this hook has no wall-clock budget.

    Measured at the shipped head, where `_one_line` scrubbed once PER LINE and so
    cost grew with the whole of stderr: 1002-1522 ms at 4.5 MB and 4069-5491 ms at
    18 MB across two runs on the same contended 10-core box. The absolute number is
    box-dependent; the DEPENDENCE on stderr size is the finding, which is why the
    test below asserts a ratio.
    """

    @staticmethod
    def _fastest(stderr: str, trials: int = 3) -> float:
        def once() -> float:
            started = time.perf_counter()
            git_probes._one_line(stderr)
            return time.perf_counter() - started

        return min(once() for _ in range(trials))

    def test_one_line_costs_the_scan_cap_and_not_the_stderr(self) -> None:
        """A RATIO, not a wall clock: the box is shared, and what regressed was the DEPENDENCE on size."""
        line = "remote: fatal: unable to access\n"
        capped = line * (redaction._MAX_SCANNED_CHARS // len(line))
        huge = line * (8 * 1024 * 1024 // len(line))
        assert self._fastest(huge) / self._fastest(capped) < _ONE_LINE_GROWTH_CEILING

    @pytest.mark.parametrize("separator", ["\x85", "\x1c", "\x1d", "\x1e", "\u2028", "\u2029"])
    def test_a_line_separator_that_is_no_run_boundary_does_not_leak(self, separator: str) -> None:
        """`str.splitlines` splits on six characters the scanner does not treat as run boundaries."""
        assert _FAKE_TOKEN_VALUE not in git_probes._one_line(f"?token=AAA&BBB{separator}{_FAKE_TOKEN_VALUE}")

    @pytest.mark.parametrize("boundary", ["\x0b", "\x0c"])
    def test_a_separator_that_is_a_run_boundary_stays_a_recorded_residual(self, boundary: str) -> None:
        """R1/R2, not coverage: these ARE run boundaries, so the taint legitimately resets."""
        assert _FAKE_TOKEN_VALUE in git_probes._one_line(f"?token=AAA&BBB{boundary}{_FAKE_TOKEN_VALUE}")
