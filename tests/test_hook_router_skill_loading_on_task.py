"""Tests for the TaskCreated sub-agent skill-loading gate (#1488, #1189).

A sub-agent spawned via the harness Workflow/Task fan-out starts BLANK: it
holds only its task prompt and lacks the ``Skill`` tool, so the teatree skill
injection (which reaches the MAIN agent only) never reaches it. The gate
therefore cannot be satisfied by what the PARENT session loaded — that state
does not transfer to the blank sub-agent. It is satisfied only when the
DISPATCH PROMPT itself instructs the sub-agent to load the demanded skills.

The demand is the parent session's ``<session>.pending`` set — the explicit
cwd/overlay-context skills the UserPromptSubmit hook recorded. There is no
free-text scan of the task description: which skills a task needs is expressed
explicitly, never inferred from prose.

The deny schema for ``TaskCreated`` is the teammate-stop envelope
``{"continue": false, "stopReason": <reason>}`` (NOT the ``PreToolUse``
``hookSpecificOutput`` deny), translated to ``sys.exit(2)`` by ``main``.

Integration-style: the real handler, real ``STATE_DIR`` on ``tmp_path``, real
fixture skills seeded under a temp ``T3_SKILL_SEARCH_DIRS``.
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


def _seed_skill(skills_dir: Path, name: str, *, requires: list[str] | None = None) -> None:
    """Create a ``<skills_dir>/<name>/SKILL.md`` with real frontmatter."""
    skill = skills_dir / name
    skill.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name}", 'description: "Fixture skill for the TaskCreated gate test."']
    if requires:
        lines.append("requires:")
        lines.extend(f"  - {r}" for r in requires)
    lines.extend(["---", f"# {name}", ""])
    (skill / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture
def gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Pin STATE_DIR + T3_SKILL_SEARCH_DIRS at tmp fixture trees."""
    original_state = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)

    skills_dir = tmp_path / "skills"
    _seed_skill(skills_dir, "review", requires=["workspace", "code"])
    _seed_skill(skills_dir, "code", requires=["workspace"])
    _seed_skill(skills_dir, "workspace")
    monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", str(skills_dir))

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
    task creation, so a fail-open skip must stay silent on stderr.
    """
    out = StringIO()
    err = io.StringIO()
    with patch("sys.stdout", out), patch("sys.stderr", err):
        blocked = handle_enforce_skill_loading_on_task_create(data)
    return blocked, err.getvalue()


class TestPendingDemandWithoutReferenceIsDenied:
    """A pending demand the dispatch prompt does not reference is denied."""

    def test_unreferenced_pending_blocks_with_add_lines(self, gate: Path) -> None:
        _write_pending("sess-task", ["review"])
        blocked, payload = _run(_task(description="review the open PR thoroughly"))
        assert blocked is True
        assert payload is not None
        # TaskCreated deny schema — the teammate-stop envelope, not PreToolUse.
        assert payload["continue"] is False
        assert "permissionDecision" not in payload
        reason = payload["stopReason"]
        assert "Read" in reason
        assert "review/SKILL.md" in reason

    def test_pending_demand_only_lists_the_demanded_root(self, gate: Path) -> None:
        # Only the demanded skill is listed — the Skill tool pulls its transitive
        # deps itself, so the deny must NOT enumerate them.
        _write_pending("sess-task", ["review"])
        blocked, payload = _run(_task(description="do neutral work"))
        assert blocked is True
        assert payload is not None
        reason = payload["stopReason"]
        assert "review/SKILL.md" in reason
        assert "code/SKILL.md" not in reason
        assert "workspace/SKILL.md" not in reason

    def test_dispatch_naming_the_demanded_skill_passes(self, gate: Path) -> None:
        _write_pending("sess-task", ["review"])
        blocked, payload = _run(_task(description="Load /t3:review, then review the open PR thoroughly."))
        assert blocked is False
        assert payload is None

    def test_no_pending_demand_passes(self, gate: Path) -> None:
        # With no explicit pending demand there is nothing to enforce — the
        # description is never scanned for a lifecycle.
        blocked, payload = _run(_task(description="review the open PR thoroughly"))
        assert blocked is False
        assert payload is None

    def test_parent_loaded_does_not_satisfy_blank_subagent(self, gate: Path) -> None:
        # THE BUG: the PARENT session has the skills loaded, but the dispatch
        # prompt does not reference them. The blank sub-agent inherits NONE of
        # the parent's loaded state, so it must still be denied with add-lines.
        _write_pending("sess-task", ["review"])
        _write_loaded("sess-task", ["review", "code", "workspace", "t3:review"])
        blocked, payload = _run(_task(description="review the open PR thoroughly"))
        assert blocked is True
        assert payload is not None
        assert "review/SKILL.md" in payload["stopReason"]


class TestPromptReferencingSkillsPasses:
    """A prompt that instructs the sub-agent to load the demanded skills passes."""

    def test_read_skill_md_lines_satisfy_the_gate(self, gate: Path, tmp_path: Path) -> None:
        _write_pending("sess-task", ["review"])
        skills_dir = tmp_path / "skills"
        description = (
            "Read these first, then review the open PR:\n"
            f"  Read {skills_dir / 'review' / 'SKILL.md'}\n"
            "Then leave feedback on the diff."
        )
        blocked, payload = _run(_task(description=description))
        assert blocked is False
        assert payload is None

    def test_slash_token_references_satisfy_the_gate(self, gate: Path) -> None:
        _write_pending("sess-task", ["review", "code"])
        description = "Load /t3:review and /t3:code, then review the open PR."
        blocked, payload = _run(_task(description=description))
        assert blocked is False
        assert payload is None

    def test_partial_reference_still_blocks_on_the_unreferenced(self, gate: Path) -> None:
        _write_pending("sess-task", ["review", "code"])
        description = "Read code/SKILL.md, then review the open PR."
        blocked, payload = _run(_task(description=description))
        assert blocked is True
        assert payload is not None
        reason = payload["stopReason"]
        assert "review/SKILL.md" in reason
        assert "code/SKILL.md" not in reason


class TestKillSwitch:
    """``[teatree] skill_loading_gate_enabled = false`` disables the gate."""

    def test_explicit_false_disables(self, gate: Path) -> None:
        _write_pending("sess-task", ["review"])
        (Path.home() / ".teatree.toml").write_text("[teatree]\nskill_loading_gate_enabled = false\n", encoding="utf-8")
        blocked, payload = _run(_task(description="review the open PR"))
        assert blocked is False
        assert payload is None

    def test_missing_config_fails_open_to_enabled(self, gate: Path) -> None:
        _write_pending("sess-task", ["review"])
        blocked, _ = _run(_task(description="review the open PR"))
        assert blocked is True

    def test_broken_config_fails_open_to_enabled(self, gate: Path) -> None:
        _write_pending("sess-task", ["review"])
        (Path.home() / ".teatree.toml").write_text("not = valid = toml [[[", encoding="utf-8")
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


class TestFailOpen:
    """Missing session id and unresolvable skills never lock out the task."""

    def test_missing_session_id_fails_open(self, gate: Path) -> None:
        blocked, payload = _run(_task(session_id="", description="review the open PR"))
        assert blocked is False
        assert payload is None

    def test_stale_pending_name_fails_open_silently(self, gate: Path) -> None:
        # ``ac-exporting-webhook-mapping`` does not resolve in the fixture tree,
        # so it is dropped (fail-open) — nothing is demanded.
        _write_pending("sess-task", ["ac-exporting-webhook-mapping"])
        blocked, stderr = _run_capturing_stderr(_task(description="do some neutral work"))
        assert blocked is False
        assert stderr == ""

    def test_unresolvable_alongside_resolvable_blocks_silently_on_the_stale_one(self, gate: Path) -> None:
        _write_pending("sess-task", ["review", "ac-exporting-webhook-mapping"])
        blocked, stderr = _run_capturing_stderr(_task(description="do some neutral work"))
        assert blocked is True
        assert stderr == ""


class TestNegatedReferenceDoesNotSatisfy:
    """A negated skill mention does not falsely satisfy the gate (#4)."""

    def test_do_not_load_phrasing_still_blocks(self, gate: Path) -> None:
        _write_pending("sess-task", ["review"])
        blocked, payload = _run(_task(description="do not load the review skill; just summarize the diff briefly"))
        assert blocked is True
        assert payload is not None
        assert "review/SKILL.md" in payload["stopReason"]

    def test_skip_phrasing_still_blocks(self, gate: Path) -> None:
        _write_pending("sess-task", ["review"])
        blocked, payload = _run(_task(description="skip the review skill and just read the file then report back"))
        assert blocked is True
        assert payload is not None
        assert "review/SKILL.md" in payload["stopReason"]

    def test_positive_reference_after_negated_clause_passes(self, gate: Path) -> None:
        _write_pending("sess-task", ["review"])
        description = "Do not skip steps. Load /t3:review via the Skill tool, then review the open PR."
        blocked, payload = _run(_task(description=description))
        assert blocked is False
        assert payload is None

    def test_emphatic_positive_after_colon_passes(self, gate: Path) -> None:
        _write_pending("sess-task", ["review"])
        for description in (
            "This is not optional: load /t3:review then review the open PR thoroughly.",
            "No shortcuts: load /t3:review via the Skill tool then review the open PR.",
        ):
            blocked, payload = _run(_task(description=description))
            assert blocked is False, description
            assert payload is None

    def test_negation_after_colon_still_blocks(self, gate: Path) -> None:
        _write_pending("sess-task", ["review"])
        blocked, payload = _run(_task(description="Note: do not load the review skill. Just read the diff please."))
        assert blocked is True
        assert payload is not None
        assert "review/SKILL.md" in payload["stopReason"]

    def test_comma_inside_negated_imperative_still_blocks(self, gate: Path) -> None:
        _write_pending("sess-task", ["review"])
        blocked, payload = _run(
            _task(description="Do not, under any circumstances, load the review skill; just read the diff.")
        )
        assert blocked is True
        assert payload is not None
        assert "review/SKILL.md" in payload["stopReason"]


class TestPathologicalPendingNameFailsOpenSilently:
    """A 255+ byte pending skill name fails OPEN — never aborts TaskCreated (#3)."""

    def test_overlong_pending_name_does_not_abort(self, gate: Path) -> None:
        _write_pending("sess-task", ["x" * 300])
        blocked, stderr = _run_capturing_stderr(_task(description="do some neutral work"))
        assert blocked is False
        assert stderr == ""

    def test_overlong_name_alongside_real_demand_still_blocks_silently(self, gate: Path) -> None:
        _write_pending("sess-task", ["review", "y" * 300])
        blocked, stderr = _run_capturing_stderr(_task(description="do some neutral work"))
        assert blocked is True
        assert stderr == ""
        _, payload = _run(_task(description="do some neutral work", session_id="sess-task"))
        assert payload is not None
        assert "review/SKILL.md" in payload["stopReason"]


class TestIgnoresFanoutShape:
    """The gate enforces skill-loading ONLY — never agent-count/budget/size caps."""

    def test_huge_fanout_with_referenced_skills_passes(self, gate: Path) -> None:
        _write_pending("sess-task", ["review", "code"])
        description = "Load /t3:review /t3:code, then review the PR."
        blocked, payload = _run(
            _task(
                description=description,
                extra={"agent_count": 64, "run_in_background": True, "token_budget": 9_000_000},
            )
        )
        assert blocked is False
        assert payload is None

    def test_huge_fanout_without_reference_blocks_on_skill_only(self, gate: Path) -> None:
        _write_pending("sess-task", ["review"])
        blocked, payload = _run(
            _task(description="review the open PR thoroughly", extra={"agent_count": 64, "token_budget": 9_000_000})
        )
        assert blocked is True
        assert payload is not None
        reason = payload["stopReason"].lower()
        assert "shrink" not in reason
        assert "fewer agent" not in reason


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
