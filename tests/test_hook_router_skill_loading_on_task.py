"""Tests for the TaskCreated skill-loading gate (#1488).

``ultracode`` and any harness Workflow/Task fan-out spawns sub-agents
through the Task/Workflow vehicle, which **bypasses ``PreToolUse``
hooks** (a known regression from TodoWrite — see
``docs/claude-code-internals.md`` §9). The existing skill-loading gate
(``handle_enforce_skill_loading``) fires only on the ``PreToolUse``
matcher ``Bash|Edit|Write``, so it is never consulted on the fan-out and
sub-agents skip auto-loading the matching teatree lifecycle skill. That
loophole let a bespoke review workflow run instead of ``/t3:review``.

``handle_enforce_skill_loading_on_task_create`` closes it: on the
``TaskCreated`` event (which DOES fire for the fan-out vehicle, with the
schema ``task_id``/``task_subject``/``task_description``), it forces the
matching teatree lifecycle skill + its already-transitive companions onto
the dispatched task. It enforces SKILL-LOADING ONLY — never caps agent
count, token budget, or workflow size (ultracode keeps maximal room).

The deny schema for ``TaskCreated`` is the teammate-stop envelope
``{"continue": false, "stopReason": <reason>}`` (NOT the ``PreToolUse``
``hookSpecificOutput`` deny), translated to ``sys.exit(2)`` by ``main``.

Integration-style: the real handler, real ``STATE_DIR`` on ``tmp_path``,
real fixture skills seeded under a temp ``T3_SKILL_SEARCH_DIRS`` whose
``SKILL.md`` frontmatter carries real ``requires``/``companions`` so the
companion closure resolves through the production resolver.
"""

import io
import json
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
import tomlkit
from typer.testing import CliRunner

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_enforce_skill_loading_on_task_create


def _seed_skill(
    skills_dir: Path, name: str, *, requires: list[str] | None = None, companions: list[str] | None = None
) -> None:
    """Create a ``<skills_dir>/<name>/SKILL.md`` with real frontmatter.

    The ``requires``/``companions`` lists are written into YAML
    frontmatter so the production trigger-index builder + companion
    resolver expand the closure exactly as they do for installed skills.
    """
    skill = skills_dir / name
    skill.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name}", 'description: "Fixture skill for the TaskCreated gate test."']
    if requires:
        lines.append("requires:")
        lines.extend(f"  - {r}" for r in requires)
    if companions:
        lines.append("companions:")
        lines.extend(f"  - {c}" for c in companions)
    lines.extend(["---", f"# {name}", ""])
    (skill / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture
def gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Pin STATE_DIR + T3_SKILL_SEARCH_DIRS + HOME at tmp fixture trees.

    Seeds the real ``review``/``code``/``workspace`` lifecycle skills with
    the same ``requires``/``companions`` shape the shipped skills carry
    (``review`` requires ``workspace`` + ``code``; ``code`` requires
    ``workspace``) so the gate's companion closure for a review task
    transitively pulls in ``code`` exactly as in production.
    """
    original_state = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)

    skills_dir = tmp_path / "skills"
    _seed_skill(skills_dir, "review", requires=["workspace", "code"])
    _seed_skill(skills_dir, "code", requires=["workspace"])
    _seed_skill(skills_dir, "workspace")
    monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", str(skills_dir))
    # ~/.teatree.toml lives under the conftest-isolated temp HOME; ensure no
    # leftover kill-switch file disables the gate by default.
    monkeypatch.delenv("TEATREE_PLAN_GATE_WINDOW_MINUTES", raising=False)

    yield skills_dir

    router.STATE_DIR = original_state


def _write_pending(session_id: str, skills: list[str]) -> None:
    (router.STATE_DIR / f"{session_id}.pending").write_text("\n".join(skills) + "\n", encoding="utf-8")


def _write_loaded(session_id: str, skills: list[str]) -> None:
    (router.STATE_DIR / f"{session_id}.skills").write_text("\n".join(skills) + "\n", encoding="utf-8")


def _task(
    *,
    session_id: str = "sess-task",
    subject: str = "do work",
    description: str = "do some work",
    extra: dict | None = None,
) -> dict:
    data = {
        "session_id": session_id,
        "hook_event_name": "TaskCreated",
        "task_id": "task-1",
        "task_subject": subject,
        "task_description": description,
    }
    if extra:
        data.update(extra)
    return data


def _run(data: dict) -> tuple[bool, dict | None]:
    """Invoke the gate, capturing its ``continue:false`` stop envelope (stdout)."""
    out = StringIO()
    with patch("sys.stdout", out):
        blocked = handle_enforce_skill_loading_on_task_create(data)
    payload = None
    raw = out.getvalue().strip()
    if raw:
        payload = json.loads(raw)
    return blocked, payload


def _run_capturing_stderr(data: dict) -> tuple[bool, str]:
    """Invoke the gate, returning ``(blocked, stderr_text)``.

    The harness treats any ``TaskCreated`` hook stderr as an error and aborts
    task creation, so a fail-open skip of an unresolvable skill must stay
    silent on stderr.
    """
    out = StringIO()
    err = io.StringIO()
    with patch("sys.stdout", out), patch("sys.stderr", err):
        blocked = handle_enforce_skill_loading_on_task_create(data)
    return blocked, err.getvalue()


class TestBlocksUnloadedReviewFanout:
    """A review fan-out with the matching skill unloaded is denied (RED on main)."""

    def test_pending_review_unloaded_blocks(self, gate: Path) -> None:
        _write_pending("sess-task", ["review"])
        blocked, payload = _run(_task(description="review the open PR"))
        assert blocked is True
        assert payload is not None
        # TaskCreated deny schema — the teammate-stop envelope, not PreToolUse.
        assert payload["continue"] is False
        assert "stopReason" in payload
        assert "permissionDecision" not in payload
        assert "/review" in payload["stopReason"]
        # The reason names the single Skill call that clears it.
        assert "Skill" in payload["stopReason"]


class TestDescriptionDrivenDetection:
    """The task DESCRIPTION drives lifecycle detection + transitive companions."""

    def test_review_description_forces_review_and_transitive_code(self, gate: Path) -> None:
        # No pending file at all — detection comes purely from the description.
        blocked, payload = _run(_task(description="please review the open PR and leave feedback"))
        assert blocked is True
        assert payload is not None
        reason = payload["stopReason"]
        assert "/review" in reason
        # ``review`` → requires ``code`` transitively; the closure must demand it.
        assert "/code" in reason

    def test_subject_alone_does_not_drive_detection(self, gate: Path) -> None:
        # Only the description is fed through ``lifecycle_for_task_text``; a
        # bare subject with a neutral description demands nothing.
        blocked, payload = _run(_task(subject="review", description="touch up a comment"))
        assert blocked is False
        assert payload is None


class TestPassesOnceLoaded:
    """Once every demanded skill is loaded, the gate passes through."""

    def test_review_loaded_with_companions_passes(self, gate: Path) -> None:
        _write_pending("sess-task", ["review"])
        _write_loaded("sess-task", ["review", "code", "workspace"])
        blocked, payload = _run(_task(description="review the open PR"))
        assert blocked is False
        assert payload is None


class TestCanonicalNamespaceMatching:
    """The ``(pending minus skills)`` minus-set matches by fully-qualified canonical.

    ``<session>.skills`` / ``<session>.pending`` record a skill VERBATIM:
    the Skill-tool PostToolUse records the namespaced form (``t3:review``)
    while InstructionsLoaded / the loader's pending writer record the bare
    form (``review``). The minus-set computation must treat a bare pending
    demand as satisfied when only the namespaced loaded form is present (and
    vice versa) — the same deadlock the PreToolUse gate had. It normalizes
    UP to the qualified canonical (``review`` → ``t3:review`` for a
    plugin-owned skill), NOT down to the bare segment, so distinct
    namespaces are never conflated. ``review``/``code``/``workspace`` are
    real plugin-owned skills, so they canonicalize to ``t3:*``.
    """

    def test_bare_pending_satisfied_by_namespaced_loaded(self, gate: Path) -> None:
        # pending bare ``review``, loaded only namespaced ``t3:review`` (plus
        # its companions in namespaced form) → the minus-set is empty and the
        # neutral description detects no new lifecycle, so the gate passes.
        _write_pending("sess-task", ["review"])
        _write_loaded("sess-task", ["t3:review", "t3:code", "t3:workspace"])
        blocked, payload = _run(_task(description="do some neutral work"))
        assert blocked is False
        assert payload is None

    def test_distinct_namespaces_are_not_conflated(self, gate: Path) -> None:
        # A demand for ``t3:review`` is NOT satisfied by a loaded
        # ``other:review`` — the bare-strip approach would wrongly clear it.
        _write_pending("sess-task", ["review"])
        _write_loaded("sess-task", ["other:review", "other:code", "other:workspace"])
        blocked, payload = _run(_task(description="do some neutral work"))
        assert blocked is True
        assert payload is not None
        assert "/review" in payload["stopReason"]

    def test_legacy_mixed_state_with_both_spellings(self, gate: Path) -> None:
        # Legacy ``.skills`` carrying the same skill under both spellings:
        # canonicalization collapses both onto ``t3:review`` so a bare demand
        # is satisfied.
        _write_pending("sess-task", ["review"])
        _write_loaded("sess-task", ["review", "t3:review", "t3:code", "t3:workspace"])
        blocked, payload = _run(_task(description="do some neutral work"))
        assert blocked is False
        assert payload is None

    def test_genuinely_unloaded_skill_still_blocks(self, gate: Path) -> None:
        # The fix must not defang the gate: a resolvable bare demand with NO
        # matching loaded form (bare or namespaced) still blocks.
        _write_pending("sess-task", ["review"])
        _write_loaded("sess-task", ["t3:code"])
        blocked, payload = _run(_task(description="do some neutral work"))
        assert blocked is True
        assert payload is not None
        assert "/review" in payload["stopReason"]


class TestFailOpenOnStaleName:
    """An unresolvable demanded skill never blocks (fail-open on rename/removal)."""

    def test_stale_pending_name_fails_open(self, gate: Path) -> None:
        # ``ac-exporting-webhook-mapping`` does not resolve in the fixture
        # tree — the auto-loader's stale demand must not lock out the task.
        _write_pending("sess-task", ["ac-exporting-webhook-mapping"])
        blocked, payload = _run(_task(description="do some neutral work"))
        assert blocked is False
        assert payload is None

    def test_stale_pending_name_is_silent_on_stderr(self, gate: Path) -> None:
        # The harness aborts task creation on ANY TaskCreated-hook stderr, so a
        # fail-open skip of an unresolvable skill (e.g. a keyword→skill map that
        # points at a non-existent skill) must emit nothing on stderr — the task
        # still gets created.
        _write_pending("sess-task", ["ac-exporting-webhook-mapping"])
        blocked, stderr = _run_capturing_stderr(_task(description="do some neutral work"))
        assert blocked is False
        assert stderr == ""

    def test_unresolvable_alongside_resolvable_blocks_silently_on_the_stale_one(self, gate: Path) -> None:
        # A resolvable demand still blocks, but the unresolvable sibling must not
        # leak onto stderr (which would abort the very task we are blocking).
        _write_pending("sess-task", ["review", "ac-exporting-webhook-mapping"])
        blocked, stderr = _run_capturing_stderr(_task(description="do some neutral work"))
        assert blocked is True
        assert stderr == ""


class TestKillSwitch:
    """``[teatree] skill_loading_gate_enabled = false`` disables the gate."""

    def test_explicit_false_disables(self, gate: Path) -> None:
        (Path.home() / ".teatree.toml").write_text("[teatree]\nskill_loading_gate_enabled = false\n", encoding="utf-8")
        _write_pending("sess-task", ["review"])
        blocked, payload = _run(_task(description="review the open PR"))
        assert blocked is False
        assert payload is None

    def test_missing_config_fails_open_to_enabled(self, gate: Path) -> None:
        # No ~/.teatree.toml → gate stays enabled (protective default).
        _write_pending("sess-task", ["review"])
        blocked, _ = _run(_task(description="review the open PR"))
        assert blocked is True

    def test_broken_config_fails_open_to_enabled(self, gate: Path) -> None:
        (Path.home() / ".teatree.toml").write_text("not = valid = toml [[[", encoding="utf-8")
        _write_pending("sess-task", ["review"])
        blocked, _ = _run(_task(description="review the open PR"))
        assert blocked is True


class TestSkipToken:
    """An explicit ``[skip-skill-gate: <reason>]`` token unblocks; empty reason blocks."""

    def test_skip_token_in_description_passes(self, gate: Path) -> None:
        _write_pending("sess-task", ["review"])
        blocked, payload = _run(_task(description="[skip-skill-gate: bespoke-eval-harness] review the PR"))
        assert blocked is False
        assert payload is None

    def test_skip_token_in_subject_passes(self, gate: Path) -> None:
        _write_pending("sess-task", ["review"])
        blocked, payload = _run(_task(subject="[skip-skill-gate: hotfix]", description="review the PR"))
        assert blocked is False
        assert payload is None

    def test_empty_reason_still_blocks(self, gate: Path) -> None:
        _write_pending("sess-task", ["review"])
        blocked, payload = _run(_task(description="[skip-skill-gate: ] review the PR"))
        assert blocked is True
        assert payload is not None


class TestIgnoresFanoutShape:
    """The gate enforces skill-loading ONLY — never agent-count/budget/size caps."""

    def test_agent_count_and_budget_fields_ignored_when_loaded(self, gate: Path) -> None:
        _write_pending("sess-task", ["review"])
        _write_loaded("sess-task", ["review", "code", "workspace"])
        blocked, payload = _run(
            _task(
                description="review the PR",
                extra={
                    "agent_count": 64,
                    "run_in_background": True,
                    "token_budget": 9_000_000,
                    "max_agents": 999,
                    "workflow_size": "maximal",
                },
            )
        )
        # No fan-out-shape field can trip the gate — only skill-loading matters.
        assert blocked is False
        assert payload is None

    def test_huge_fanout_with_unloaded_skill_blocks_on_skill_only(self, gate: Path) -> None:
        _write_pending("sess-task", ["review"])
        blocked, payload = _run(
            _task(description="review the PR", extra={"agent_count": 64, "token_budget": 9_000_000})
        )
        assert blocked is True
        assert payload is not None
        # The remediation is the Skill call — never "shrink the workflow".
        reason = payload["stopReason"].lower()
        assert "shrink" not in reason
        assert "fewer agent" not in reason
        assert "reduce" not in reason


class TestCliSelfRescue:
    """``t3 <overlay> gate skill-loading disable`` writes ``= false`` (self-rescue)."""

    def test_disable_writes_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        from teatree.cli.overlay import OverlayAppBuilder  # noqa: PLC0415
        from teatree.cli.teatree_gate import SKILL_GATE_KEY, skill_loading_gate_is_enabled  # noqa: PLC0415

        app = OverlayAppBuilder(overlay_name="acme", project_path=None).build()
        result = CliRunner().invoke(app, ["gate", "skill-loading", "disable"])
        assert result.exit_code == 0, result.output

        document = tomlkit.parse((tmp_path / ".teatree.toml").read_text(encoding="utf-8"))
        assert document["teatree"][SKILL_GATE_KEY] is False
        assert skill_loading_gate_is_enabled() is False

    def test_status_and_enable_round_trip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        from teatree.cli.overlay import OverlayAppBuilder  # noqa: PLC0415
        from teatree.cli.teatree_gate import skill_loading_gate_is_enabled  # noqa: PLC0415

        runner = CliRunner()
        app = OverlayAppBuilder(overlay_name="acme", project_path=None).build()
        assert "ENABLED" in runner.invoke(app, ["gate", "skill-loading", "status"]).output
        assert runner.invoke(app, ["gate", "skill-loading", "disable"]).exit_code == 0
        assert skill_loading_gate_is_enabled() is False
        assert runner.invoke(app, ["gate", "skill-loading", "enable"]).exit_code == 0
        assert skill_loading_gate_is_enabled() is True
