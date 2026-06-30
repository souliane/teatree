# test-path: cross-cutting
"""Worktree-FSM diagram generation: pure renderer + generate/check hooks.

Spans ``teatree.core.diagrams`` (the pure Mermaid renderer + marker splice) and
the two ``scripts/hooks`` drift-pipeline scripts, mirroring the
generate-cli-reference / cli-reference-sync pair.
"""

import os
from pathlib import Path

import pytest
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
from teatree.core.models import Worktree

BEGIN = "<!-- BEGIN GENERATED: worktree-fsm -->"
END = "<!-- END GENERATED: worktree-fsm -->"


def _model_edges(model: type, field: str = "state") -> set[tuple[str, str, str]]:
    """The (source, target, name) edge set straight from the FSM field's registry."""
    fsm = model._meta.get_field(field)
    states = [str(value) for value, _label in fsm.choices]
    edges: set[tuple[str, str, str]] = set()
    for transition in fsm.get_all_transitions(model):
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
    def test_render_is_deterministic(self) -> None:
        assert render_fsm_mermaid(Worktree) == render_fsm_mermaid(Worktree)

    def test_first_line_is_state_diagram_header(self) -> None:
        assert render_fsm_mermaid(Worktree).splitlines()[0] == "stateDiagram-v2"

    def test_contains_exactly_the_model_transitions_no_phantom_edges(self) -> None:
        assert _diagram_edges(render_fsm_mermaid(Worktree)) == _model_edges(Worktree)

    def test_includes_previously_omitted_stop_services_edges(self) -> None:
        """The drift proof: both hand-drawn consumers omitted these SERVICES_UP/READY -> PROVISIONED edges."""
        rendered = render_fsm_mermaid(Worktree)
        assert "services_up --> provisioned : stop_services" in rendered
        assert "ready --> provisioned : stop_services" in rendered

    def test_includes_wildcard_expanded_teardown_edges(self) -> None:
        rendered = render_fsm_mermaid(Worktree)
        for source in ("created", "provisioned", "services_up", "ready"):
            assert f"{source} --> created : teardown" in rendered

    def test_emits_initial_state_edge_from_the_field_default(self) -> None:
        lines = render_fsm_mermaid(Worktree).splitlines()
        assert lines[1] == "    [*] --> created"

    def test_omits_initial_state_edge_when_field_has_no_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Worktree._meta.get_field("state"), "default", NOT_PROVIDED)
        rendered = render_fsm_mermaid(Worktree)
        assert "[*]" not in rendered
        assert rendered.splitlines()[0] == "stateDiagram-v2"

    def test_title_emits_mermaid_frontmatter(self) -> None:
        rendered = render_fsm_mermaid(Worktree, title="Worktree lifecycle")
        assert rendered.startswith("---\ntitle: Worktree lifecycle\n---\nstateDiagram-v2")

    def test_no_title_emits_no_frontmatter(self) -> None:
        rendered = render_fsm_mermaid(Worktree)
        assert rendered.startswith("stateDiagram-v2")
        assert "---" not in rendered


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
        block = fenced_mermaid(render_fsm_mermaid(Worktree))
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
        block = fenced_mermaid(render_fsm_mermaid(Worktree))
        doc = f"x\n{BEGIN}\n{block}\n{END}\ny\n"
        assert chk.find_drift(block, {"README.md": doc, "SKILL.md": doc}, begin=BEGIN, end=END) == []

    def test_find_drift_reports_the_mutated_consumer(self) -> None:
        block = fenced_mermaid(render_fsm_mermaid(Worktree))
        good = f"x\n{BEGIN}\n{block}\n{END}\ny\n"
        stale = f"x\n{BEGIN}\n```mermaid\nstateDiagram-v2\n    stale\n```\n{END}\ny\n"
        assert chk.find_drift(block, {"README.md": good, "SKILL.md": stale}, begin=BEGIN, end=END) == ["SKILL.md"]


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "docs" / "generated" / "diagrams").mkdir(parents=True)
    (tmp_path / "skills" / "workspace").mkdir(parents=True)
    empty = f"intro\n\n{BEGIN}\n{END}\n\noutro\n"
    (tmp_path / "README.md").write_text(empty, encoding="utf-8")
    (tmp_path / "skills" / "workspace" / "SKILL.md").write_text(empty, encoding="utf-8")
    monkeypatch.setattr(gen, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(chk, "_repo_root", lambda: tmp_path)
    monkeypatch.setenv("FSM_DIAGRAMS_NO_STAGE", "1")
    return tmp_path


class TestGeneratorAndCheckAgainstFakeRepo:
    def test_generator_populates_all_consumers_in_sync(self, fake_repo: Path) -> None:
        assert gen.main() == 0
        canonical = (fake_repo / "docs" / "generated" / "diagrams" / "worktree-lifecycle.md").read_text("utf-8")
        readme = (fake_repo / "README.md").read_text("utf-8")
        skill = (fake_repo / "skills" / "workspace" / "SKILL.md").read_text("utf-8")
        for text in (canonical, readme, skill):
            assert "services_up --> provisioned : stop_services" in text
        assert chk.main() == 0

    def test_generator_run_twice_is_a_noop(self, fake_repo: Path) -> None:
        gen.main()
        snapshot = {p: p.read_text("utf-8") for p in fake_repo.rglob("*.md")}
        gen.main()
        assert {p: p.read_text("utf-8") for p in fake_repo.rglob("*.md")} == snapshot

    def test_check_detects_a_mutated_consumer(self, fake_repo: Path) -> None:
        gen.main()
        readme = fake_repo / "README.md"
        readme.write_text(readme.read_text("utf-8").replace("stop_services", "TAMPERED"), encoding="utf-8")
        assert chk.main() == 1

    def test_generator_fails_loud_on_a_consumer_missing_markers(self, fake_repo: Path) -> None:
        (fake_repo / "README.md").write_text("no markers at all\n", encoding="utf-8")
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
    """The committed consumers must already carry the generated block (guards the wired gate)."""

    def test_real_repo_passes_the_sync_gate(self) -> None:
        previous = os.environ.pop("FSM_DIAGRAMS_NO_STAGE", None)
        try:
            assert chk.main() == 0
        finally:
            if previous is not None:
                os.environ["FSM_DIAGRAMS_NO_STAGE"] = previous
