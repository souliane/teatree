"""Tests for the TaskCreated sub-agent skill-loading gate (#1488).

A sub-agent spawned via the harness Workflow/Task fan-out starts BLANK: it
holds only its task prompt and lacks the ``Skill`` tool, so the teatree skill
injection (which reaches the MAIN agent only) never reaches it. The gate
therefore cannot be satisfied by what the PARENT session loaded — that state
does not transfer to the blank sub-agent. It is satisfied only when the
DISPATCH PROMPT itself instructs the sub-agent to load the required skills.

``handle_enforce_skill_loading_on_task_create`` rides the ``TaskCreated`` event
(which DOES fire for the fan-out, unlike ``PreToolUse``): it computes the
required skills from the task DESCRIPTION (the lifecycle skill + its transitive
companions + the active overlay's companions for that lifecycle), drops any the
prompt already references, and denies with the exact ``Read …/SKILL.md`` lines
to ADD so the orchestrator embeds skill-loading in the dispatch.

The deny schema for ``TaskCreated`` is the teammate-stop envelope
``{"continue": false, "stopReason": <reason>}`` (NOT the ``PreToolUse``
``hookSpecificOutput`` deny), translated to ``sys.exit(2)`` by ``main``.

Integration-style: the real handler, real ``STATE_DIR`` on ``tmp_path``, real
fixture skills seeded under a temp ``T3_SKILL_SEARCH_DIRS`` whose ``SKILL.md``
frontmatter carries real ``requires``/``companions`` so the companion closure
resolves through the production resolver.
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
    """Create a ``<skills_dir>/<name>/SKILL.md`` with real frontmatter."""
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
    """Pin STATE_DIR + T3_SKILL_SEARCH_DIRS at tmp fixture trees.

    Seeds the real ``review``/``code``/``workspace`` lifecycle skills with the
    same ``requires``/``companions`` shape the shipped skills carry (``review``
    requires ``workspace`` + ``code``; ``code`` requires ``workspace``) so the
    gate's companion closure for a review task transitively pulls in ``code``
    and ``workspace`` exactly as in production.
    """
    original_state = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)

    skills_dir = tmp_path / "skills"
    _seed_skill(skills_dir, "review", requires=["workspace", "code"])
    _seed_skill(skills_dir, "code", requires=["workspace"])
    _seed_skill(skills_dir, "workspace")
    # ``debug``/``ship`` are seeded so the trivial-task examples (``fix the typo``
    # → ``debug``, ``push the branch`` → ``ship``) WOULD resolve a demand without
    # the trivial guard — keeping those tests anti-vacuous.
    _seed_skill(skills_dir, "debug", requires=["workspace"])
    _seed_skill(skills_dir, "ship", requires=["workspace"])
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


class TestPromptWithoutSkillReferenceIsDenied:
    """A code/review fan-out whose prompt doesn't reference the skills is denied."""

    def test_task_without_skill_reference_blocks_with_add_lines(self, gate: Path) -> None:
        # The description maps to a lifecycle but never tells the sub-agent to
        # load any skill — the blank sub-agent would run skill-less.
        blocked, payload = _run(_task(description="review the open PR thoroughly"))
        assert blocked is True
        assert payload is not None
        # TaskCreated deny schema — the teammate-stop envelope, not PreToolUse.
        assert payload["continue"] is False
        assert "permissionDecision" not in payload
        reason = payload["stopReason"]
        # The deny lists the exact Read line for the ROOT skill to ADD.
        assert "Read" in reason
        assert "review/SKILL.md" in reason

    def test_roots_only_does_not_demand_transitive_closure(self, gate: Path) -> None:
        # The fix: only the un-derivable ROOTS are demanded. ``review`` requires
        # ``code`` → ``workspace`` transitively, but the Skill tool pulls those
        # itself, so the deny must NOT list them (the over-block #1 defect).
        blocked, payload = _run(_task(description="review the open PR thoroughly"))
        assert blocked is True
        assert payload is not None
        reason = payload["stopReason"]
        assert "code/SKILL.md" not in reason
        assert "workspace/SKILL.md" not in reason

    def test_dispatch_naming_only_the_root_passes(self, gate: Path) -> None:
        # A dispatch that names the root lifecycle skill but NOT its transitive
        # deps must pass — this is the reviewer-dispatch shape the old closure
        # gate wrongly denied (#1).
        blocked, payload = _run(_task(description="Load /t3:review, then review the open PR thoroughly."))
        assert blocked is False
        assert payload is None

    def test_pending_demand_without_reference_blocks(self, gate: Path) -> None:
        # Even with a neutral description, an explicit ``<session>.pending``
        # demand the prompt does not reference is denied.
        _write_pending("sess-task", ["review"])
        blocked, payload = _run(_task(description="do some neutral work"))
        assert blocked is True
        assert payload is not None
        assert "review/SKILL.md" in payload["stopReason"]

    def test_parent_loaded_does_not_satisfy_blank_subagent(self, gate: Path) -> None:
        # THE BUG: the PARENT session has the skills loaded, but the dispatch
        # prompt does not reference them. The blank sub-agent inherits NONE of
        # the parent's loaded state, so it must still be denied with add-lines.
        # The old gate keyed on ``<session>.skills`` and PASSED here (the bug).
        _write_loaded("sess-task", ["review", "code", "workspace", "t3:review", "t3:code", "t3:workspace"])
        blocked, payload = _run(_task(description="review the open PR thoroughly"))
        assert blocked is True
        assert payload is not None
        assert "review/SKILL.md" in payload["stopReason"]


class TestPromptReferencingSkillsPasses:
    """A prompt that instructs the sub-agent to load the required skills passes."""

    def test_read_skill_md_lines_satisfy_the_gate(self, gate: Path, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        description = (
            "Read these first, then review the open PR:\n"
            f"  Read {skills_dir / 'review' / 'SKILL.md'}\n"
            f"  Read {skills_dir / 'code' / 'SKILL.md'}\n"
            f"  Read {skills_dir / 'workspace' / 'SKILL.md'}\n"
            "Then leave feedback on the diff."
        )
        blocked, payload = _run(_task(description=description))
        assert blocked is False
        assert payload is None

    def test_slash_token_references_satisfy_the_gate(self, gate: Path) -> None:
        description = "Load /t3:review and /t3:code and /t3:workspace, then review the open PR."
        blocked, payload = _run(_task(description=description))
        assert blocked is False
        assert payload is None

    def test_unreferenced_root_still_blocks_listing_only_the_root(self, gate: Path) -> None:
        # The prompt references the transitive deps ``code`` + ``workspace`` but
        # not the ``review`` ROOT itself → still denied, listing ONLY the root
        # (the transitive deps were never demanded — roots-only #1).
        description = "Read code/SKILL.md and workspace/SKILL.md, then review the open PR."
        blocked, payload = _run(_task(description=description))
        assert blocked is True
        assert payload is not None
        reason = payload["stopReason"]
        assert "review/SKILL.md" in reason
        assert "code/SKILL.md" not in reason
        assert "workspace/SKILL.md" not in reason


class TestReviewLifecycleUnionsOverlayCompanions:
    """A review task unions the active overlay's review companions into the demand."""

    @staticmethod
    def _patch_roots(monkeypatch: pytest.MonkeyPatch, roots: list[str]) -> None:
        import subagent_skill_gate  # noqa: PLC0415

        monkeypatch.setattr(subagent_skill_gate, "required_skills_for_task", lambda description, search_dirs: roots)

    def test_overlay_review_companion_is_demanded_when_unreferenced(
        self, gate: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_skill(gate, "code-review")
        self._patch_roots(monkeypatch, ["review", "code-review"])
        blocked, payload = _run(_task(description="review the open PR thoroughly"))
        assert blocked is True
        assert payload is not None
        assert "code-review/SKILL.md" in payload["stopReason"]

    def test_overlay_review_companion_referenced_passes(self, gate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_skill(gate, "code-review")
        self._patch_roots(monkeypatch, ["review", "code-review"])
        description = "Load /review and /code-review, then review the open PR thoroughly."
        blocked, payload = _run(_task(description=description))
        assert blocked is False
        assert payload is None


class TestKillSwitch:
    """``[teatree] skill_loading_gate_enabled = false`` disables the gate."""

    def test_explicit_false_disables(self, gate: Path) -> None:
        (Path.home() / ".teatree.toml").write_text("[teatree]\nskill_loading_gate_enabled = false\n", encoding="utf-8")
        blocked, payload = _run(_task(description="review the open PR"))
        assert blocked is False
        assert payload is None

    def test_missing_config_fails_open_to_enabled(self, gate: Path) -> None:
        blocked, _ = _run(_task(description="review the open PR"))
        assert blocked is True

    def test_broken_config_fails_open_to_enabled(self, gate: Path) -> None:
        (Path.home() / ".teatree.toml").write_text("not = valid = toml [[[", encoding="utf-8")
        blocked, _ = _run(_task(description="review the open PR"))
        assert blocked is True


class TestSkipToken:
    """An explicit ``[skip-skill-gate: <reason>]`` token unblocks; empty reason blocks."""

    def test_skip_token_in_description_passes(self, gate: Path) -> None:
        blocked, payload = _run(_task(description="[skip-skill-gate: bespoke-eval-harness] review the PR"))
        assert blocked is False
        assert payload is None

    def test_skip_token_in_subject_passes(self, gate: Path) -> None:
        blocked, payload = _run(_task(subject="[skip-skill-gate: hotfix]", description="review the PR"))
        assert blocked is False
        assert payload is None

    def test_empty_reason_still_blocks(self, gate: Path) -> None:
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
        # ``ac-exporting-webhook-mapping`` does not resolve in the fixture tree;
        # a neutral description detects no lifecycle, so nothing is demanded.
        _write_pending("sess-task", ["ac-exporting-webhook-mapping"])
        blocked, stderr = _run_capturing_stderr(_task(description="do some neutral work"))
        assert blocked is False
        assert stderr == ""

    def test_unresolvable_alongside_resolvable_blocks_silently_on_the_stale_one(self, gate: Path) -> None:
        _write_pending("sess-task", ["review", "ac-exporting-webhook-mapping"])
        blocked, stderr = _run_capturing_stderr(_task(description="do some neutral work"))
        assert blocked is True
        assert stderr == ""


class TestTrivialTasksAreNotForced:
    """Trivial / ambiguous fan-outs are not forced to slash-list skills (#2)."""

    def test_fix_the_typo_in_readme_passes(self, gate: Path) -> None:
        # ``fix`` maps to the ``debug`` lifecycle, but a README typo fix is not a
        # substantive debug dispatch — must not be forced.
        blocked, payload = _run(_task(description="fix the typo in the README"))
        assert blocked is False
        assert payload is None

    def test_push_the_branch_passes(self, gate: Path) -> None:
        # ``push`` maps to ``ship``; a bare three-word imperative is trivial.
        blocked, payload = _run(_task(description="push the branch"))
        assert blocked is False
        assert payload is None

    def test_investigate_why_the_build_is_broken_passes(self, gate: Path) -> None:
        # ``broken`` maps to ``debug``; an investigation is ambiguous, not forced.
        blocked, payload = _run(_task(description="investigate why the build is broken"))
        assert blocked is False
        assert payload is None

    def test_substantive_review_dispatch_still_blocks(self, gate: Path) -> None:
        # Anti-vacuous: a substantive lifecycle dispatch IS still demanded, so
        # the relaxation did not disable the gate wholesale.
        blocked, payload = _run(_task(description="review the open PR thoroughly"))
        assert blocked is True
        assert payload is not None
        assert "review/SKILL.md" in payload["stopReason"]

    def test_long_dispatch_with_weak_marker_still_enforces(self, gate: Path) -> None:
        # A weak marker (``review``/``why``) in a LONG substantive dispatch must
        # NOT suppress the demand — the weak-marker triviality is short-only.
        description = "review the open PR and explain why each change was made, then leave detailed feedback"
        blocked, payload = _run(_task(description=description))
        assert blocked is True
        assert payload is not None
        assert "review/SKILL.md" in payload["stopReason"]


class TestNegatedReferenceDoesNotSatisfy:
    """A negated skill mention does not falsely satisfy the gate (#4)."""

    def test_do_not_load_phrasing_still_blocks(self, gate: Path) -> None:
        # ``do not load the review skill`` is a NEGATED mention — it must not
        # count as a reference, so the demand stands.
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
        # Anti-vacuous: a genuine positive reference outside the negation window
        # still satisfies the gate — the guard does not over-block.
        _write_pending("sess-task", ["review"])
        description = "Do not skip steps. Load /t3:review via the Skill tool, then review the open PR."
        blocked, payload = _run(_task(description=description))
        assert blocked is False
        assert payload is None

    def test_emphatic_positive_after_colon_passes(self, gate: Path) -> None:
        # A negation before a colon scopes to its own clause: an emphatic
        # positive instruction after the boundary is NOT negated.
        _write_pending("sess-task", ["review"])
        for description in (
            "This is not optional: load /t3:review then review the open PR thoroughly.",
            "No shortcuts: load /t3:review via the Skill tool then review the open PR.",
        ):
            blocked, payload = _run(_task(description=description))
            assert blocked is False, description
            assert payload is None

    def test_negation_after_colon_still_blocks(self, gate: Path) -> None:
        # The colon boundary must not let a genuine negation AFTER it escape:
        # ``Note: do not load …`` still negates the in-clause reference.
        _write_pending("sess-task", ["review"])
        blocked, payload = _run(_task(description="Note: do not load the review skill. Just read the diff please."))
        assert blocked is True
        assert payload is not None
        assert "review/SKILL.md" in payload["stopReason"]

    def test_comma_inside_negated_imperative_still_blocks(self, gate: Path) -> None:
        # The comma is NOT a clause boundary: it appears WITHIN a single negated
        # imperative, so a negation must not escape across it (under-block guard).
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
        # ``is_file`` raises OSError ("File name too long") on a 255+ byte
        # segment; the handler must catch it and fail OPEN with no stderr (the
        # harness aborts TaskCreated on ANY handler stderr → lockout).
        _write_pending("sess-task", ["x" * 300])
        blocked, stderr = _run_capturing_stderr(_task(description="do some neutral work"))
        assert blocked is False
        assert stderr == ""

    def test_overlong_name_alongside_real_demand_still_blocks_silently(self, gate: Path) -> None:
        # A real unreferenced demand still blocks even when a pathological name
        # sits beside it — the fail-open is per-error, not a blanket pass.
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
        description = "Load /t3:review /t3:code /t3:workspace, then review the PR."
        blocked, payload = _run(
            _task(
                description=description,
                extra={"agent_count": 64, "run_in_background": True, "token_budget": 9_000_000},
            )
        )
        assert blocked is False
        assert payload is None

    def test_huge_fanout_without_reference_blocks_on_skill_only(self, gate: Path) -> None:
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
