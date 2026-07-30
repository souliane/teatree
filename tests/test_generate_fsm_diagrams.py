# test-path: cross-cutting
"""FSM-diagram generation: pure renderer + the spec-driven generate/check hooks.

Spans ``teatree.core.diagrams`` (the pure Mermaid renderer + marker splice), the
``scripts/hooks/fsm_diagram_specs`` registry (which models become diagrams), and
the two drift-pipeline scripts, mirroring the generate-cli-reference /
cli-reference-sync pair. A new model is wired by one ``DiagramSpec``; these tests
assert the loop covers Worktree, PullRequest, and Ticket, and that Task — which
mutates its ``FSMField`` imperatively rather than via ``@transition`` — is
deliberately excluded.
"""

import os
from pathlib import Path
from typing import TYPE_CHECKING, cast

import fsm_diagram_specs as fsm
import pytest
from django.db.models import Model
from django.db.models.fields import NOT_PROVIDED

from scripts.hooks import check_fsm_diagrams_sync as chk
from scripts.hooks import generate_fsm_diagrams as gen
from teatree.core.diagrams import (
    MarkerNotFoundError,
    extract_between_markers,
    fenced_mermaid,
    inject_between_markers,
    render_fsm_mermaid,
)
from teatree.core.models import PullRequest, Task, Ticket, Worktree

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django_fsm import FSMField

WORKTREE_BEGIN = "<!-- BEGIN GENERATED: worktree-fsm -->"
WORKTREE_END = "<!-- END GENERATED: worktree-fsm -->"
PR_BEGIN = "<!-- BEGIN GENERATED: pull-request-fsm -->"
PR_END = "<!-- END GENERATED: pull-request-fsm -->"
TICKET_BEGIN = "<!-- BEGIN GENERATED: ticket-fsm -->"
TICKET_END = "<!-- END GENERATED: ticket-fsm -->"

# The marker-splice tests are model-agnostic; reuse the worktree pair as the generic one.
BEGIN = WORKTREE_BEGIN
END = WORKTREE_END

# (model, field, default-state) for every model whose FSM IS generated.
GENERATED_MODELS = [
    (Worktree, "state", "created"),
    (PullRequest, "state", "open"),
    (Ticket, "state", "not_started"),
]


def _model_edges(model: type[Model], field: str = "state") -> set[tuple[str, str, str]]:
    """The (source, target, name) edge set straight from the FSM field's registry."""
    fsm_field = cast("FSMField", model._meta.get_field(field))
    choices = cast("Iterable[tuple[object, object]]", fsm_field.choices)
    states = [str(value) for value, _label in choices]
    edges: set[tuple[str, str, str]] = set()
    for transition in fsm_field.get_all_transitions(model):
        target = str(getattr(transition.target, "value", transition.target))
        sources = states if transition.source == "*" else [str(getattr(transition.source, "value", transition.source))]
        edges.update((source, target, transition.name) for source in sources)
    return edges


def _diagram_edges(diagram: str) -> set[tuple[str, str, str]]:
    edges: set[tuple[str, str, str]] = set()
    for raw in diagram.splitlines():
        line = raw.strip()
        if "-->" in line and ":" in line:
            arrow, name = line.rsplit(":", 1)
            source, target = arrow.split("-->")
            edges.add((source.strip(), target.strip(), name.strip()))
    return edges


class TestRenderFsmMermaid:
    @pytest.mark.parametrize(("model", "field", "_default"), GENERATED_MODELS)
    def test_render_is_deterministic(self, model: type[Model], field: str, _default: str) -> None:
        assert render_fsm_mermaid(model, field=field) == render_fsm_mermaid(model, field=field)

    @pytest.mark.parametrize(("model", "field", "_default"), GENERATED_MODELS)
    def test_first_line_is_state_diagram_header(self, model: type[Model], field: str, _default: str) -> None:
        assert render_fsm_mermaid(model, field=field).splitlines()[0] == "stateDiagram-v2"

    @pytest.mark.parametrize(("model", "field", "_default"), GENERATED_MODELS)
    def test_contains_exactly_the_model_transitions_no_phantom_edges(
        self, model: type[Model], field: str, _default: str
    ) -> None:
        assert _diagram_edges(render_fsm_mermaid(model, field=field)) == _model_edges(model, field)

    @pytest.mark.parametrize(("model", "field", "default"), GENERATED_MODELS)
    def test_emits_initial_state_edge_from_the_field_default(
        self, model: type[Model], field: str, default: str
    ) -> None:
        assert render_fsm_mermaid(model, field=field).splitlines()[1] == f"    [*] --> {default}"

    def test_pull_request_generated_block_removes_handdrawn_merge_drift(self) -> None:
        """Drift proof: the hand-drawn block named the merge edge ``merge`` and dropped two ``mark_merged`` sources."""
        rendered = render_fsm_mermaid(PullRequest)
        assert "approved --> merged : mark_merged" in rendered
        assert "open --> merged : mark_merged" in rendered
        assert "review_requested --> merged : mark_merged" in rendered
        assert ": merge\n" not in rendered + "\n"

    def test_ticket_wildcard_ignore_edge_expands_to_every_state(self) -> None:
        rendered = render_fsm_mermaid(Ticket)
        for source in ("not_started", "scoped", "started", "planned", "coded", "tested", "reviewed"):
            assert f"{source} --> ignored : ignore" in rendered

    def test_task_fsm_has_no_transitions_so_is_not_single_sourceable(self) -> None:
        """Task mutates ``status`` imperatively (no ``@transition``), so the renderer finds no edges."""
        assert _model_edges(Task, "status") == set()
        rendered = render_fsm_mermaid(Task, field="status")
        assert _diagram_edges(rendered) == set()
        assert rendered.splitlines() == ["stateDiagram-v2", "    [*] --> pending"]

    def test_omits_initial_state_edge_when_field_has_no_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(PullRequest._meta.get_field("state"), "default", NOT_PROVIDED)
        rendered = render_fsm_mermaid(PullRequest)
        assert "[*]" not in rendered
        assert rendered.splitlines()[0] == "stateDiagram-v2"

    def test_title_emits_mermaid_frontmatter(self) -> None:
        rendered = render_fsm_mermaid(PullRequest, title="PullRequest lifecycle")
        assert rendered.startswith("---\ntitle: PullRequest lifecycle\n---\nstateDiagram-v2")


class TestDiagramSpecRegistry:
    def test_specs_cover_worktree_pull_request_and_ticket(self) -> None:
        by_slug = {spec.slug: spec for spec in fsm.specs()}
        assert by_slug["worktree"].model is Worktree
        assert by_slug["pull-request"].model is PullRequest
        assert by_slug["ticket"].model is Ticket

    def test_task_is_not_a_generated_spec(self) -> None:
        assert Task not in {spec.model for spec in fsm.specs()}

    def test_markers_and_canonical_derive_from_slug(self) -> None:
        spec = next(s for s in fsm.specs() if s.slug == "pull-request")
        assert spec.begin == PR_BEGIN
        assert spec.end == PR_END
        assert spec.canonical == Path("docs/generated/diagrams/pull-request-lifecycle.md")

    def test_block_renders_the_models_fsm(self) -> None:
        spec = next(s for s in fsm.specs() if s.slug == "ticket")
        assert spec.block() == fenced_mermaid(render_fsm_mermaid(Ticket))


class TestMarkerSplice:
    def _doc(self, block: str) -> str:
        return f"prologue\n{BEGIN}\n{block}\n{END}\nepilogue\n"

    def test_inject_is_idempotent(self) -> None:
        once = inject_between_markers(self._doc("OLD"), begin=BEGIN, end=END, block="NEW")
        twice = inject_between_markers(once, begin=BEGIN, end=END, block="NEW")
        assert once == twice

    def test_inject_replaces_existing_block(self) -> None:
        result = inject_between_markers(self._doc("OLD"), begin=BEGIN, end=END, block="NEW")
        assert "OLD" not in result
        assert "NEW" in result
        assert result.startswith("prologue\n")
        assert result.endswith("epilogue\n")

    def test_inject_missing_begin_marker_raises(self) -> None:
        with pytest.raises(MarkerNotFoundError):
            inject_between_markers(f"no markers\n{END}\n", begin=BEGIN, end=END, block="X")

    def test_inject_missing_end_marker_raises(self) -> None:
        with pytest.raises(MarkerNotFoundError):
            inject_between_markers(f"{BEGIN}\nbody\n", begin=BEGIN, end=END, block="X")

    def test_extract_returns_the_injected_block(self) -> None:
        block = fenced_mermaid(render_fsm_mermaid(PullRequest))
        doc = inject_between_markers(self._doc("OLD"), begin=BEGIN, end=END, block=block)
        assert extract_between_markers(doc, begin=BEGIN, end=END) == block

    def test_extract_missing_markers_raises(self) -> None:
        with pytest.raises(MarkerNotFoundError):
            extract_between_markers("no markers here", begin=BEGIN, end=END)


class TestFenced:
    def test_fenced_wraps_in_mermaid_code_block(self) -> None:
        assert fenced_mermaid("BODY") == "```mermaid\nBODY\n```"


class TestSyncGateLogic:
    def test_find_drift_empty_when_every_consumer_matches(self) -> None:
        block = fenced_mermaid(render_fsm_mermaid(PullRequest))
        doc = f"x\n{PR_BEGIN}\n{block}\n{PR_END}\ny\n"
        assert chk.find_drift(block, {"README.md": doc, "canon.md": doc}, begin=PR_BEGIN, end=PR_END) == []

    def test_find_drift_reports_the_mutated_consumer(self) -> None:
        block = fenced_mermaid(render_fsm_mermaid(PullRequest))
        good = f"x\n{PR_BEGIN}\n{block}\n{PR_END}\ny\n"
        stale = f"x\n{PR_BEGIN}\n```mermaid\nstateDiagram-v2\n    stale\n```\n{PR_END}\ny\n"
        assert chk.find_drift(block, {"README.md": good, "canon.md": stale}, begin=PR_BEGIN, end=PR_END) == ["canon.md"]


def _empty_markers(*pairs: tuple[str, str]) -> str:
    body = "\n\n".join(f"{begin}\n{end}" for begin, end in pairs)
    return f"intro\n\n{body}\n\noutro\n"


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "docs" / "generated" / "diagrams").mkdir(parents=True)
    (tmp_path / "skills" / "workspace").mkdir(parents=True)
    (tmp_path / "README.md").write_text(
        _empty_markers((WORKTREE_BEGIN, WORKTREE_END), (PR_BEGIN, PR_END), (TICKET_BEGIN, TICKET_END)),
        encoding="utf-8",
    )
    (tmp_path / "skills" / "workspace" / "SKILL.md").write_text(
        _empty_markers((WORKTREE_BEGIN, WORKTREE_END)), encoding="utf-8"
    )
    monkeypatch.setattr(fsm, "repo_root", lambda: tmp_path)
    monkeypatch.setenv("FSM_DIAGRAMS_NO_STAGE", "1")
    return tmp_path


class TestGeneratorAndCheckAgainstFakeRepo:
    def test_generator_populates_every_consumer_in_sync(self, fake_repo: Path) -> None:
        assert gen.main() == 0
        readme = (fake_repo / "README.md").read_text("utf-8")
        skill = (fake_repo / "skills" / "workspace" / "SKILL.md").read_text("utf-8")
        # Worktree's drift proof from PR-a, plus each new model's signature edge.
        assert "services_up --> provisioned : stop_services" in readme
        assert "services_up --> provisioned : stop_services" in skill
        assert "open --> merged : mark_merged" in readme
        assert "not_started --> scoped : scope" in readme
        for slug in ("worktree", "pull-request", "ticket"):
            canon = (fake_repo / "docs" / "generated" / "diagrams" / f"{slug}-lifecycle.md").read_text("utf-8")
            assert "stateDiagram-v2" in canon
        assert chk.main() == 0

    def test_generator_run_twice_is_a_noop(self, fake_repo: Path) -> None:
        gen.main()
        snapshot = {p: p.read_text("utf-8") for p in fake_repo.rglob("*.md")}
        gen.main()
        assert {p: p.read_text("utf-8") for p in fake_repo.rglob("*.md")} == snapshot

    @pytest.mark.parametrize("needle", ["stop_services", "mark_merged", "not_started --> scoped"])
    def test_check_detects_a_mutated_consumer(self, fake_repo: Path, needle: str) -> None:
        gen.main()
        readme = fake_repo / "README.md"
        readme.write_text(readme.read_text("utf-8").replace(needle, "TAMPERED"), encoding="utf-8")
        assert chk.main() == 1

    def test_generator_fails_loud_on_a_consumer_missing_markers(self, fake_repo: Path) -> None:
        (fake_repo / "README.md").write_text("no markers at all\n", encoding="utf-8")
        assert gen.main() == 1

    def test_generator_fails_loud_when_a_new_diagrams_markers_are_absent(self, fake_repo: Path) -> None:
        readme = fake_repo / "README.md"
        readme.write_text(_empty_markers((WORKTREE_BEGIN, WORKTREE_END)), encoding="utf-8")  # PR + ticket markers gone
        assert gen.main() == 1

    def test_generator_stages_changed_files_when_not_suppressed(
        self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FSM_DIAGRAMS_NO_STAGE", raising=False)
        staged: list[list[str]] = []
        monkeypatch.setattr(gen.subprocess, "run", lambda cmd, **_: staged.append(cmd))
        gen.main()
        assert any(cmd[:2] == ["git", "add"] for cmd in staged)


class TestRealRepoConsumersInSync:
    """The committed consumers must already carry the generated blocks (guards the wired gate)."""

    def test_real_repo_passes_the_sync_gate(self) -> None:
        previous = os.environ.pop("FSM_DIAGRAMS_NO_STAGE", None)
        try:
            assert chk.main() == 0
        finally:
            if previous is not None:
                os.environ["FSM_DIAGRAMS_NO_STAGE"] = previous

    def test_real_readme_pull_request_block_is_generated_not_handdrawn(self) -> None:
        readme = (fsm.repo_root() / "README.md").read_text("utf-8")
        block = extract_between_markers(readme, begin=PR_BEGIN, end=PR_END)
        assert block == fenced_mermaid(render_fsm_mermaid(PullRequest))
        assert _diagram_edges(block) == _model_edges(PullRequest)
        assert "mark_merged" in block  # the model's real transition name, not the hand-drawn ``merge``

    def test_real_readme_ticket_block_matches_the_model(self) -> None:
        readme = (fsm.repo_root() / "README.md").read_text("utf-8")
        block = extract_between_markers(readme, begin=TICKET_BEGIN, end=TICKET_END)
        assert _diagram_edges(block) == _model_edges(Ticket)

    def test_real_repo_task_diagram_is_still_hand_drawn(self) -> None:
        readme = (fsm.repo_root() / "README.md").read_text("utf-8")
        assert "BEGIN GENERATED: task-fsm" not in readme
        assert "claimed --> completed: success" in readme  # the hand-drawn block survives untouched
