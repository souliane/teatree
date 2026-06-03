"""Golden lockout-regression corpus: must-allow and must-deny command verdicts.

Two explicit corpora guard the command-prohibition gate against two failure modes.
MUST-ALLOW: legitimate factory commands the gate must never block; a regression
here causes a lockout.  MUST-DENY: prohibited commands the gate must always block;
a regression here is a bypass.

Each corpus entry calls the same real gate function the PreToolUse hook uses:
``_deny_match`` (F3/F6/F8/--no-verify/blocked-tools),
``_extract_bash_ai_sig_payload`` (F1 double-space, F2 REST-API write routing),
``handle_block_out_of_band_merge`` (raw merge on managed repos), and
``handle_enforce_skill_loading`` (skill-loading lockout vs bypass dimension).
No matchers are re-implemented in this test.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import (
    _deny_match,
    _extract_bash_ai_sig_payload,
    handle_block_out_of_band_merge,
    handle_dispatch_prompt_quote_scanner_on_task_create,
    handle_enforce_orchestrator_boundary,
    handle_enforce_skill_loading,
    handle_quote_scanner_pretool,
    handle_validate_mr_metadata,
)

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


class TestSkillLoadingLockoutDimension:
    """Over-block (lockout) vs under-block (bypass) corpus for the skill-loading gate.

    MUST-ALLOW: a bare ``<session>.pending`` demand whose ONLY loaded form is
    namespaced (``rules`` demanded, ``t3:rules`` loaded by the Skill tool)
    must clear the gate — both canonicalize UP to ``t3:rules`` — otherwise
    the gate hard-wedges every Bash/Edit/Write with no in-session
    self-rescue but the per-call token. MUST-DENY (defang guard): a
    genuinely-unloaded resolvable skill still blocks. MUST-DENY
    (distinctness): a demand for ``t3:code`` is NOT satisfied by a loaded
    ``other:code`` — canonicalizing UP keeps distinct namespaces distinct,
    which the bare-strip approach would have wrongly conflated.

    ``rules``/``code`` are real plugin-owned skills, so they canonicalize to
    ``t3:*``; the fixture also seeds them as resolvable so the gate reaches
    the block path rather than failing open on a stale name.
    """

    @pytest.fixture
    def skill_gate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        original_state = router.STATE_DIR
        router.STATE_DIR = tmp_path / "state"
        router.STATE_DIR.mkdir(parents=True, exist_ok=True)
        skills_dir = tmp_path / "skills"
        for name in ("rules", "code"):
            skill = skills_dir / name
            skill.mkdir(parents=True, exist_ok=True)
            (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
        monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", str(skills_dir))
        yield
        router.STATE_DIR = original_state

    def _write(self, session_id: str, suffix: str, names: list[str]) -> None:
        (router.STATE_DIR / f"{session_id}.{suffix}").write_text("\n".join(names) + "\n", encoding="utf-8")

    def test_must_allow_bare_demand_namespaced_loaded(self, skill_gate: None) -> None:
        self._write("sess-skill-allow", "pending", ["rules"])
        self._write("sess-skill-allow", "skills", ["t3:rules"])
        blocked = handle_enforce_skill_loading(
            {"session_id": "sess-skill-allow", "tool_name": "Bash", "tool_input": {"command": "uv run pytest -q"}}
        )
        assert blocked is False, "LOCKOUT regression — bare demand 'rules' not cleared by loaded 't3:rules'."

    def test_must_deny_distinct_namespace_not_conflated(self, skill_gate: None) -> None:
        self._write("sess-skill-distinct", "pending", ["code"])
        self._write("sess-skill-distinct", "skills", ["other:code"])
        blocked = handle_enforce_skill_loading(
            {"session_id": "sess-skill-distinct", "tool_name": "Bash", "tool_input": {"command": "uv run pytest -q"}}
        )
        assert blocked is True, "CONFLATION regression — demand 't3:code' wrongly cleared by loaded 'other:code'."

    def test_must_deny_genuinely_unloaded_skill(self, skill_gate: None) -> None:
        self._write("sess-skill-deny", "pending", ["code"])
        self._write("sess-skill-deny", "skills", ["rules"])
        blocked = handle_enforce_skill_loading(
            {"session_id": "sess-skill-deny", "tool_name": "Bash", "tool_input": {"command": "uv run pytest -q"}}
        )
        assert blocked is True, "BYPASS regression — genuinely-unloaded 'code' was allowed."

    def test_must_deny_distinct_when_owned_set_unreadable(
        self, skill_gate: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Strict-degrade guard: when the owned-set scan fails (returns empty),
        # the canonicalizer collapses to verbatim equality. A bare demand
        # ``code`` and a loaded namespaced ``t3:rules`` then never match, so
        # the gate must still BLOCK — skill A (rules) can never satisfy a
        # demand for skill B (code). The safe failure mode over-blocks
        # (recoverable via kill-switch / token / circuit breaker); it must
        # never fail open onto the wrong skill.
        monkeypatch.setattr(router, "_plugin_owned_skills", lambda: (_ for _ in ()).throw(OSError("unreadable")))
        self._write("sess-skill-unreadable", "pending", ["code"])
        self._write("sess-skill-unreadable", "skills", ["t3:rules"])
        blocked = handle_enforce_skill_loading(
            {
                "session_id": "sess-skill-unreadable",
                "tool_name": "Bash",
                "tool_input": {"command": "uv run pytest -q"},
            }
        )
        assert blocked is True, "STRICT-DEGRADE regression — demand 'code' wrongly cleared while owned set unreadable."

    # MUST-NOT-FIRE (over-block dimension): with a resolvable Python skill
    # pending and unloaded, a NON-code-work call must pass — the gate is scoped
    # to genuine Python/Django work. These are symmetric to the must-deny cases
    # above and exist so the over-block recurrence (#107-class) is caught in CI.

    def test_must_not_fire_on_markdown_edit(self, skill_gate: None) -> None:
        self._write("sess-md", "pending", ["code"])
        blocked = handle_enforce_skill_loading(
            {"session_id": "sess-md", "tool_name": "Edit", "tool_input": {"file_path": "README.md"}}
        )
        assert blocked is False, "OVER-BLOCK regression — a markdown edit was gated for a Python skill."

    def test_must_not_fire_on_yaml_edit(self, skill_gate: None) -> None:
        self._write("sess-yml", "pending", ["code"])
        blocked = handle_enforce_skill_loading(
            {"session_id": "sess-yml", "tool_name": "Write", "tool_input": {"file_path": ".github/ci.yml"}}
        )
        assert blocked is False, "OVER-BLOCK regression — a yaml edit was gated for a Python skill."

    def test_must_not_fire_on_git_status(self, skill_gate: None) -> None:
        self._write("sess-gs", "pending", ["code"])
        blocked = handle_enforce_skill_loading(
            {"session_id": "sess-gs", "tool_name": "Bash", "tool_input": {"command": "git status"}}
        )
        assert blocked is False, "OVER-BLOCK regression — git status was gated for a Python skill."

    def test_must_not_fire_on_dotfiles_commit(self, skill_gate: None) -> None:
        self._write("sess-dot", "pending", ["code"])
        blocked = handle_enforce_skill_loading(
            {
                "session_id": "sess-dot",
                "tool_name": "Bash",
                "tool_input": {"command": "git commit -am 'chore: update dotfiles'"},
            }
        )
        assert blocked is False, "OVER-BLOCK regression — a dotfiles commit was gated for a Python skill."

    def test_must_not_fire_on_ask_user_question(self, skill_gate: None) -> None:
        self._write("sess-ask", "pending", ["code"])
        blocked = handle_enforce_skill_loading(
            {"session_id": "sess-ask", "tool_name": "AskUserQuestion", "tool_input": {"questions": []}}
        )
        assert blocked is False, "OVER-BLOCK regression — AskUserQuestion was gated (must NEVER be gated)."


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


class TestPublishPrivacyGatesDoNotOverBlock:
    """The newly-reachable privacy gates (#171) must not lock out clean traffic.

    PR A made the Slack-MCP arm of the #1213 quote-scanner gate reachable (the
    ``mcp__.*[Ss]lack.*`` matcher) and added a TaskCreated dispatch-quote arm.
    Symmetric to the bypass corpus: a regression that DENIES a clean Slack send
    or a clean fan-out task is an OVER-BLOCK lockout. The loop's own user-DMs go
    through the ``t3 ... notify`` CLI / webhook, NOT a ``mcp__*slack*`` write
    tool, so the loop is unaffected; this guards that an ordinary clean
    Slack-MCP send and a clean fan-out brief still pass.
    """

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
        self._home_dir = home

    def _write_toml(self, body: str) -> None:
        (self._home_dir / ".teatree.toml").write_text(body, encoding="utf-8")

    def test_clean_slack_mcp_send_is_not_blocked(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Default-ON Slack-MCP arm + a clean body (no verbatim quote) must pass.
        self._write_toml("[teatree]\nmcp_privacy_gate_enabled = true\n")
        data = {
            "session_id": "sess-corpus",
            "tool_name": "mcp__claude_ai_Slack__slack_send_message",
            "tool_input": {"text": "Routine status update; the sweep is green."},
        }
        verdict = handle_quote_scanner_pretool(data)
        assert verdict is not True, "LOCKOUT regression — clean Slack-MCP send was blocked by the publish-privacy gate."
        assert capsys.readouterr().out.strip() == ""

    def test_clean_fanout_task_is_not_blocked(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Flag enabled + a clean fan-out brief (no verbatim quote) must pass.
        self._write_toml("[teatree]\ndispatch_quote_gate_on_task_create_enabled = true\n")
        data = {
            "session_id": "sess-corpus",
            "task_subject": "implement the export endpoint",
            "task_description": "Wire the export endpoint to the dashboard per the spec.",
        }
        verdict = handle_dispatch_prompt_quote_scanner_on_task_create(data)
        assert verdict is not True, "LOCKOUT regression — clean fan-out task was blocked by the dispatch-quote gate."
        assert capsys.readouterr().out.strip() == ""


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


class TestOrchestratorBoundaryAgentArmDoesNotOverBlock:
    """The orchestrator-boundary Agent arm (#171 PR B) must not wedge the loop.

    The loop dispatches builder/reviewer/resolver sub-agents via the ``Agent``
    tool — sometimes foreground, often background. The foreground-Agent deny
    (#1442) ships default-OFF behind ``orchestrator_boundary_agent_gate_enabled``
    precisely so an unattended run can never be locked out of its own dispatches.
    These rows guard the four safe paths (flag OFF, background, sub-agent context,
    ``[fg-ok: <reason>]`` token) and the one genuine deny (flag ON + bare
    foreground main-agent dispatch with no escape).
    """

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
        monkeypatch.setenv("HOME", str(home))
        self._home_dir = home

    def _enable(self) -> None:
        (self._home_dir / ".teatree.toml").write_text(
            "[teatree]\norchestrator_boundary_agent_gate_enabled = true\n", encoding="utf-8"
        )

    def _agent(self, *, run_in_background: bool = False, prompt: str = "implement", agent_id: str = "") -> dict:
        data: dict = {
            "session_id": "sess-corpus",
            "tool_name": "Agent",
            "tool_input": {"prompt": prompt, "run_in_background": run_in_background},
        }
        if agent_id:
            data["agent_id"] = agent_id
        return data

    def test_foreground_agent_passes_when_flag_off(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Default-OFF: no config file at all → gate inert, foreground dispatch allowed.
        verdict = handle_enforce_orchestrator_boundary(self._agent(run_in_background=False))
        assert verdict is not True, "LOCKOUT regression — foreground Agent dispatch denied while gate default-OFF."
        assert capsys.readouterr().out.strip() == ""

    def test_background_agent_passes_with_flag_on(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._enable()
        verdict = handle_enforce_orchestrator_boundary(self._agent(run_in_background=True))
        assert verdict is not True, "LOCKOUT regression — background Agent dispatch denied even with the flag ON."
        assert capsys.readouterr().out.strip() == ""

    def test_foreground_agent_passes_with_fg_ok_token(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._enable()
        verdict = handle_enforce_orchestrator_boundary(
            self._agent(run_in_background=False, prompt="[fg-ok: attended-debug] implement")
        )
        assert verdict is not True, "LOCKOUT regression — foreground Agent dispatch with a valid [fg-ok:] token denied."
        assert capsys.readouterr().out.strip() == ""

    def test_subagent_context_dispatch_is_exempt(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._enable()
        verdict = handle_enforce_orchestrator_boundary(self._agent(run_in_background=False, agent_id="a-1234"))
        assert verdict is not True, "LOCKOUT regression — a sub-agent's own foreground Agent dispatch was denied."
        assert capsys.readouterr().out.strip() == ""

    def test_bare_foreground_main_agent_dispatch_is_denied_when_on(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._enable()
        verdict = handle_enforce_orchestrator_boundary(self._agent(run_in_background=False))
        out = capsys.readouterr().out.strip()
        assert verdict is True, (
            "BYPASS regression — bare foreground main-agent Agent dispatch was allowed with the gate ON."
        )
        assert out, "an enabled deny must emit a hookSpecificOutput payload"
        assert "[fg-ok:" in out
        assert "run_in_background" in out


class TestValidateMrMetadataMcpArm:
    """Gate 3 (#171 PR B): the glab-MR MCP arm validates the same as the Bash arm.

    Reuses the already-live Bash-arm validator on the ``mcp__glab__glab_mr_*``
    path. A clean title/description must pass (must-ALLOW, no lockout); a
    malformed one must be denied (must-DENY, the validation the matcher unlocks).
    The validator subprocess is pinned so the test never shells a real ``t3``.
    """

    def _pin_validator(self, monkeypatch: pytest.MonkeyPatch, returncode: int, stderr: str = "") -> None:
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        result = subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)
        monkeypatch.setattr(router.subprocess, "run", lambda *a, **k: result)

    def test_clean_mcp_mr_create_passes(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._pin_validator(monkeypatch, returncode=0)
        data = {
            "session_id": "sess-corpus",
            "tool_name": "mcp__glab__glab_mr_create",
            "tool_input": {"title": "feat: add export endpoint", "description": "Closes #4242"},
        }
        verdict = handle_validate_mr_metadata(data)
        assert verdict is not True, "LOCKOUT regression — clean glab-MR MCP create denied by the metadata gate."
        assert capsys.readouterr().out.strip() == ""

    def test_malformed_mcp_mr_create_is_denied(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._pin_validator(monkeypatch, returncode=1, stderr="title must follow the conventional-commit format")
        data = {
            "session_id": "sess-corpus",
            "tool_name": "mcp__glab__glab_mr_create",
            "tool_input": {"title": "", "description": ""},
        }
        verdict = handle_validate_mr_metadata(data)
        assert verdict is True, "BYPASS regression — malformed glab-MR MCP metadata was allowed."
        assert capsys.readouterr().out.strip(), "a deny must emit a hookSpecificOutput payload"
