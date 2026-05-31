"""Golden lockout-regression corpus: must-allow and must-deny command verdicts.

Two explicit corpora guard the command-prohibition gate against two failure modes.
MUST-ALLOW: legitimate factory commands the gate must never block; a regression
here causes a lockout.  MUST-DENY: prohibited commands the gate must always block;
a regression here is a bypass.

Each corpus entry calls the same real gate function the PreToolUse hook uses:
``_deny_match`` (F3/F6/F8/--no-verify/blocked-tools),
``_extract_bash_ai_sig_payload`` (F1 double-space, F2 REST-API write routing), and
``handle_block_out_of_band_merge`` (raw merge on managed repos).  No matchers are
re-implemented in this test.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _deny_match, _extract_bash_ai_sig_payload, handle_block_out_of_band_merge

# ── fixtures ──────────────────────────────────────────────────────────────────


class _FakeHomePath:
    """Pin ``router.Path.home()`` to a tmp dir for managed-repo slug resolution."""

    def __init__(self, home: Path) -> None:
        self._home = home

    def __call__(self, *args: object, **kwargs: object) -> Path:
        return Path(*args, **kwargs)

    def home(self) -> Path:
        return self._home


def _write_managed_config(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home.mkdir(exist_ok=True)
    (home / ".teatree.toml").write_text(
        '[overlays.example]\nworkspace_repos = ["example-org/repo"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(router, "Path", _FakeHomePath(home))


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


def _managed_repo(tmp_path: Path, slug: str = "example-org/repo") -> Path:
    """Return a git repo whose remote slug is listed as teatree-managed."""
    repo = tmp_path / "wt"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "remote", "add", "origin", f"git@github.com:{slug}.git")
    return repo


def _merge_event(command: str, cwd: Path | None) -> dict:
    return {
        "session_id": "sess-corpus",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(cwd) if cwd is not None else "",
    }


def _parse_deny(capsys: pytest.CaptureFixture[str]) -> dict | None:
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


# ── must-allow corpus ─────────────────────────────────────────────────────────
#
# These are legitimate factory commands. A gate regression that DENIES any of
# them causes a lockout — the factory cannot operate until the bug is fixed.

_MUST_ALLOW_DENY_MATCH: list[tuple[str, str]] = [
    # self-rescue: t3 gate disable + kill-switch edit
    ("t3 teatree gate disable", "gate disable (self-rescue)"),
    ("t3 loop claim --take-over", "loop claim (self-rescue)"),
    # ship path: PR/MR create
    ('gh pr create --title "x" --body "Closes #12"', "gh pr create"),
    ("glab mr create --title 'feat: add feature' --description 'Closes #12'", "glab mr create"),
    # conventional commits mentioning tool names INSIDE the quoted message
    ("git commit -m 'fix: guard pip install path'", "commit mentioning pip install in quotes"),
    ('git commit -m "docs: document docker compose usage"', "commit mentioning docker compose in quotes"),
    ("git commit -m 'test: cover manage.py migrate edge case'", "commit mentioning manage.py in quotes"),
    # read pipelines that mention tool names in quoted args or patterns
    ("cat file.txt | grep 'pip install'", "grep with pip install pattern"),
    ('grep -r "manage.py migrate" .', "grep for manage.py migrate"),
    ('grep -r "docker compose up" .', "grep for docker compose up"),
    # gh/glab API reads (no body flag, no write method)
    ("gh api repos/o/r/pulls/1", "gh api read"),
    ("glab api projects/1/merge_requests", "glab api list read"),
    # push variants
    ("git push", "plain git push"),
    ("git push -u origin branch", "git push with upstream"),
    ("git push --force-with-lease", "git push force-with-lease"),
    # ordinary reads
    ("ls -la", "ls"),
    ("cat file.txt", "cat"),
    ("git log --oneline -10", "git log"),
    ("t3 teatree ticket list", "t3 ticket list"),
    ("t3 teatree worktree status", "t3 worktree status"),
    # echo/printf are in READONLY prefix — even if they mention blocked tool names
    ("echo 'manage.py runserver is not allowed'", "echo mentioning blocked tool"),
    ("printf 'pip install is blocked'", "printf mentioning pip install"),
]


@pytest.mark.parametrize(
    ("command", "label"),
    _MUST_ALLOW_DENY_MATCH,
    ids=[label for _, label in _MUST_ALLOW_DENY_MATCH],
)
def test_must_allow_deny_match(command: str, label: str) -> None:
    """Gate must not deny any legitimate factory command."""
    reason = _deny_match(command)
    assert reason is None, (
        f"LOCKOUT regression — legitimate command was denied.\n"
        f"  command : {command!r}\n"
        f"  label   : {label}\n"
        f"  reason  : {reason}"
    )


_MUST_ALLOW_AI_SIG_ROUTING: list[tuple[str, str]] = [
    # Conventional commits with clean messages must NOT be routed to AI-sig scan
    # (no inline body → None; but commits without -m return None — correct).
    # A commit with a -m that has no Co-Authored-By must not route (still None).
    ("git commit -m 'fix: guard pip install path'", "clean commit not routed to AI-sig"),
    ('git commit -m "refactor: tidy up"', "clean commit not routed to AI-sig (dq)"),
]


@pytest.mark.parametrize(
    ("command", "label"),
    _MUST_ALLOW_AI_SIG_ROUTING,
    ids=[label for _, label in _MUST_ALLOW_AI_SIG_ROUTING],
)
def test_must_allow_ai_sig_routing_passes(command: str, label: str) -> None:
    """Clean ``-m`` commits return their message text (a non-None string).

    For a ``git commit -m <msg>`` command, ``_extract_bash_ai_sig_payload``
    must return the message text so the AI-signature scanner can inspect it.
    Returning ``None`` would mean the scanner never sees the payload — a bypass
    for a ``-m`` commit carrying a banned trailer.  These entries have no banned
    trailer, so the scanner passes them; what this test guards is that the
    function returns a scannable string rather than ``None``.
    """
    payload = _extract_bash_ai_sig_payload(command)
    assert payload is not None, (
        f"Clean commit should produce a scannable payload (message text), got None.\n"
        f"  command : {command!r}\n"
        f"  label   : {label}"
    )


class TestMustAllowMerge:
    """Merge commands on unmanaged repos must not be blocked."""

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_managed_config(tmp_path / "home", monkeypatch)

    @pytest.mark.parametrize(
        ("command", "label"),
        [
            ("gh pr merge 3 --squash", "gh pr merge on unmanaged repo"),
            ("glab mr merge 4", "glab mr merge on unmanaged repo"),
        ],
        ids=["gh pr merge on unmanaged repo", "glab mr merge on unmanaged repo"],
    )
    def test_merge_on_unmanaged_repo_is_allowed(
        self,
        command: str,
        label: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        repo = _managed_repo(tmp_path, slug="example-org/public-repo")
        verdict = handle_block_out_of_band_merge(_merge_event(command, repo))
        assert verdict is not True, (
            f"LOCKOUT regression — merge on unmanaged repo was blocked.\n  command : {command!r}\n  label   : {label}"
        )
        assert capsys.readouterr().out.strip() == ""


# ── must-deny corpus ──────────────────────────────────────────────────────────
#
# These are prohibited commands. A gate regression that ALLOWS any of them is a
# bypass — the gate's invariant is violated and the protection it provides is lost.

_MUST_DENY_DENY_MATCH: list[tuple[str, str]] = [
    # F3 hooksPath — all quoting variants
    ("git -c core.hooksPath=/dev/null commit", "F3 hooksPath unquoted"),
    ("git -c core.hookspath=x commit", "F3 hookspath lowercase"),
    ('git -c "core.hooksPath=x" commit', "F3 hooksPath double-quoted"),
    ("git -c 'core.hooksPath=x' commit", "F3 hooksPath single-quoted"),
    # F6 shell chain: read-only prefix chaining a blocked command
    ("grep '' /dev/null; python manage.py migrate", "F6 chain: grep ; manage.py migrate"),
    ("cat x | pip install y", "F6 chain: cat | pip install"),
    # F8 auto-merge push option — quoted + unquoted + long form
    ("git push -o merge_request.merge_when_pipeline_succeeds", "F8 push-option unquoted"),
    ("git push -o 'merge_request.merge_when_pipeline_succeeds'", "F8 push-option single-quoted"),
    ('git push -o "merge_request.merge_when_pipeline_succeeds"', "F8 push-option double-quoted"),
    ("git push --push-option=merge_request.merge_when_pipeline_succeeds", "F8 --push-option long form"),
    # --no-verify
    ("git commit --no-verify -m 'skip hooks'", "--no-verify on commit"),
    ("git push --no-verify origin main", "--no-verify on push"),
    # --no-gpg-sign (in _QUOTE_STRIPPED_BLOCKED ~line 176)
    ("git commit --no-gpg-sign -m msg", "--no-gpg-sign on commit"),
    # blocked tools (not prefixed by readonly/t3)
    ("pip install requests", "bare pip install"),
    ("python manage.py migrate", "bare manage.py migrate"),
    ("docker compose up -d", "bare docker compose up"),
    ("npx playwright test", "bare playwright test"),
    # remaining _QUOTE_STRIPPED_BLOCKED tools
    (".venv/bin/python script.py", ".venv/bin invocation"),
    ("nx serve", "bare nx serve"),
    ("createdb mydb", "bare createdb"),
    ("dropdb mydb", "bare dropdb"),
    ("npm run build", "bare npm run"),
    ("pg_dump mydb", "bare pg_dump"),
    ("pg_restore mydb.dump", "bare pg_restore"),
    ("dslr restore mysnap", "dslr restore (mutating subcommand)"),
    ("uv run t3 worktree status", "uv run t3 (use t3 directly)"),
    ("safety check", "safety check (use pip-audit instead)"),
]


@pytest.mark.parametrize(
    ("command", "label"),
    _MUST_DENY_DENY_MATCH,
    ids=[label for _, label in _MUST_DENY_DENY_MATCH],
)
def test_must_deny_deny_match(command: str, label: str) -> None:
    """Gate must deny every prohibited command."""
    reason = _deny_match(command)
    assert reason is not None, (
        f"BYPASS regression — prohibited command was allowed.\n  command : {command!r}\n  label   : {label}"
    )


_MUST_DENY_AI_SIG_ROUTING: list[tuple[str, str]] = [
    # F1 double-space: bypass attempt via extra whitespace — gate uses \s+ so
    # these still route to the AI-sig scanner (non-None payload).
    ('git  commit -m "fix: add feature\n\nCo-Authored-By: Bot <bot@example.com>"', "F1 double-space git commit"),
    ("glab  mr  create --title 'feat' --description 'Co-Authored-By: x'", "F1 double-space glab mr create"),
    # F2 REST-API PR/MR create write: must route to AI-sig scanner (non-None payload).
    ("gh api repos/o/r/pulls -X POST -f title=x", "F2 gh api POST to pulls"),
    ("glab api projects/1/merge_requests --method POST -f title=feat", "F2 glab api POST to merge_requests"),
]


@pytest.mark.parametrize(
    ("command", "label"),
    _MUST_DENY_AI_SIG_ROUTING,
    ids=[label for _, label in _MUST_DENY_AI_SIG_ROUTING],
)
def test_must_deny_ai_sig_routes_to_scanner(command: str, label: str) -> None:
    """Prohibited F1/F2 commands must be routed to the AI-signature scanner.

    A non-None payload confirms the command is NOT exempted from the scanner.
    The scanner itself then blocks any command whose payload contains a banned
    trailer; this test guards only that the routing step doesn't exempt the
    command before it reaches the scanner (which would be a bypass).
    """
    payload = _extract_bash_ai_sig_payload(command)
    assert payload is not None, (
        f"BYPASS regression — F1/F2 command was exempted from AI-sig scanner (got None payload).\n"
        f"  command : {command!r}\n"
        f"  label   : {label}"
    )


class TestMustDenyMerge:
    """Merge commands on teatree-managed repos must be blocked."""

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_managed_config(tmp_path / "home", monkeypatch)

    @pytest.mark.parametrize(
        ("command", "label"),
        [
            ("gh pr merge 1", "gh pr merge on managed repo"),
            ("glab mr merge 1", "glab mr merge on managed repo"),
            (
                "gh api repos/example-org/repo/pulls/1/merge -X PUT",
                "REST-API merge via gh api PUT (_is_raw_merge_api_write arm)",
            ),
            (
                "glab api projects/1/merge_requests/1/merge --method POST",
                "REST-API merge via glab api POST (_is_raw_merge_api_write arm)",
            ),
        ],
        ids=[
            "gh pr merge on managed repo",
            "glab mr merge on managed repo",
            "REST-API merge via gh api PUT (_is_raw_merge_api_write arm)",
            "REST-API merge via glab api POST (_is_raw_merge_api_write arm)",
        ],
    )
    def test_merge_on_managed_repo_is_denied(
        self,
        command: str,
        label: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        repo = _managed_repo(tmp_path, slug="example-org/repo")
        verdict = handle_block_out_of_band_merge(_merge_event(command, repo))
        deny = _parse_deny(capsys)
        assert verdict is True, (
            f"BYPASS regression — raw merge on managed repo was not blocked.\n"
            f"  command : {command!r}\n"
            f"  label   : {label}"
        )
        assert deny is not None
        assert "ticket merge" in deny["permissionDecisionReason"]


class TestCircuitBreakerNeverOpensSafetyGate:
    """The repeated-denial circuit breaker must NEVER auto-relax a SAFETY gate.

    The breaker fails OPEN a looped UX gate to break a token-burning retry loop,
    but a safety gate (any reason NOT on the UX allow-list — merge/substrate,
    banned-terms, privacy, out-of-band-merge, orchestrator-boundary) must keep
    denying past the threshold. Auto-relaxing a safety gate after N retries would
    be a bypass: the loop would eventually punch the call through. This guards
    that the breaker still DENIES the K-th and (K+1)-th identical safety denial.
    """

    @pytest.fixture(autouse=True)
    def _breaker_context(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path / "state")
        router.STATE_DIR.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(router, "_CURRENT_EVENT", "PreToolUse")
        monkeypatch.setattr(router, "_CURRENT_DATA", {"session_id": "corpus-breaker", "tool_name": "Bash"})

    def test_safety_gate_deny_never_relaxes_at_or_past_threshold(self) -> None:
        reason = (
            "BLOCKED: the orchestrator (main agent) ran a command that looks like heavy work. Delegate to a sub-agent."
        )
        decisions = [router._apply_deny_circuit_breaker(reason) for _ in range(5)]
        assert all(d.allow is False for d in decisions), (
            "BYPASS regression — the circuit breaker auto-relaxed a SAFETY gate; "
            "a safety gate must never fail open no matter how many times it is retried."
        )
        # From the threshold onward the deny reason carries the loop escalation;
        # it is still a deny, never an allow.
        assert "CIRCUIT BREAKER" in decisions[-1].reason
        assert "LOOPING" in decisions[-1].reason
