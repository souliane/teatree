from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import Client, TestCase
from django.urls import reverse

from teatree.dash.skill_control import SkillDashboardSource, build_skill_dashboard
from teatree.dash.views.skills import DashboardState
from teatree.harness_skills import SkillsHarness
from teatree.provisioning.declared import DeclaredDependency
from teatree.provisioning.harness_skill_removal import HarnessSkillRemovalError, RemovalStage
from teatree.provisioning.skill_provenance import SkillInstallationFact, SkillInstallKind
from teatree.provisioning.skills_cli import (
    SKILLS_CLI_VERSION,
    SKILLS_RECEIPT_VERSION,
    HarnessSkillInventory,
    InstalledSkill,
    SkillsInventoryReceipt,
)
from teatree.skill_support.demands import SkillDemand
from teatree.skill_support.inventory import EmbeddedSkillClosure, SkillDeclaration, SkillIdentity, SkillInventory


def _state(tmp_path: Path) -> DashboardState:
    code = SkillIdentity("t3", "code")
    inventory = SkillInventory(
        declarations=(SkillDeclaration(code, tmp_path / "code" / "SKILL.md", (), (), ()),),
        missing_references=(),
        cycles=(),
        agent_closures=(EmbeddedSkillClosure("coder", (code,), (), (code,), ()),),
        phase_closures=(EmbeddedSkillClosure("coding", (code,), (), (code,), ()),),
    )
    collision = InstalledSkill(
        "Code",
        "/manager/Code",
        "global",
        ("Claude Code",),
        "acme/skills",
        "https://example.invalid/acme/skills.git",
        "github",
        SkillInstallationFact("/home/.claude/skills/Code", SkillInstallKind.COPY, None, None),
    )
    required = InstalledSkill(
        "writing-plans",
        "/manager/writing-plans",
        "global",
        ("Codex",),
        "obra/superpowers",
        "https://example.invalid/obra/superpowers.git",
        "github",
        SkillInstallationFact("/home/.codex/skills/writing-plans", SkillInstallKind.SYMLINK, "/src/superpowers", None),
    )
    optional = InstalledSkill(
        "optional",
        "/manager/optional",
        "global",
        ("Codex",),
        "acme/skills",
        None,
        "github",
        SkillInstallationFact("/home/.codex/skills/optional", SkillInstallKind.COPY, None, None),
    )
    receipt = SkillsInventoryReceipt(
        SKILLS_RECEIPT_VERSION,
        datetime(2026, 9, 22, 8, 0, tzinfo=UTC),
        SKILLS_CLI_VERSION,
        (
            HarnessSkillInventory(SkillsHarness.CLAUDE_CODE, (collision,)),
            HarnessSkillInventory(SkillsHarness.CODEX, (required, optional)),
        ),
    )
    dependencies = (
        DeclaredDependency("skill", "writing-plans", "apm.yml", "install", "obra/superpowers/writing-plans#abc"),
    )
    demands = (SkillDemand("review_skill", "review"),)
    dashboard = build_skill_dashboard(
        source=SkillDashboardSource(inventory, dependencies, demands, []),
        receipt=receipt,
        receipt_error=None,
        now=datetime(2026, 9, 22, 9, 0, tzinfo=UTC),
    )
    return DashboardState(dashboard, inventory, dependencies, demands, [])


class SkillsPageTestCase(TestCase):
    def test_collision_rows_and_nav_badge_have_danger_styling(self) -> None:
        stylesheet = (
            Path(__file__).resolve().parents[3] / "src" / "teatree" / "dash" / "static" / "dash" / "css" / "dash.css"
        ).read_text()

        assert ".skill-row.danger" in stylesheet
        assert ".nav-alert" in stylesheet
        assert ".skill-summary-card.danger" in stylesheet

    def test_get_is_cached_only_and_renders_accessible_collision_and_dependency_detail(self) -> None:
        state = _state(Path("/tmp/skills-page"))
        with (
            patch("teatree.dash.views.skills._load_state", return_value=state),
            patch("teatree.dash.views.skills.refresh_inventory_receipt", side_effect=AssertionError("GET refreshed")),
        ):
            response = self.client.get(reverse("dash:skills"))

        assert response.status_code == 200
        html = response.content.decode()
        assert '<h1 class="dash-h1">Skills</h1>' in html
        assert 'aria-label="Skills collision count"' in html
        assert ">1</span>" in html
        assert html.count('class="skill-row danger"') == 2
        assert "t3:code" in html
        assert "acme/skills:Code" in html
        assert "/tmp/skills-page/code/SKILL.md" in html
        assert "coder" in html
        assert "coding" in html
        assert "writing-plans" in html
        assert "apm.yml" in html
        assert "Refresh inventory" in html
        assert '<th scope="col">' in html
        assert "Read only" in html
        assert "Remove optional from Codex?" in html

    def test_get_filters_server_side_without_hiding_the_nav_collision_badge(self) -> None:
        state = _state(Path("/tmp/skills-page"))
        with patch("teatree.dash.views.skills._load_state", return_value=state):
            response = self.client.get(reverse("dash:skills"), {"q": "optional"})

        html = response.content.decode()
        assert response.status_code == 200
        assert "acme/skills:optional" in html
        assert "acme/skills:Code" not in html
        assert 'aria-label="Skills collision count">1</span>' in html

    def test_missing_and_malformed_receipts_render_stale_page_without_subprocess(self) -> None:
        paths = [Path("/tmp/absent-skills-receipt.json"), Path("/tmp/malformed-skills-receipt.json")]
        paths[0].unlink(missing_ok=True)
        paths[1].write_text("not-json")
        self.addCleanup(paths[1].unlink, missing_ok=True)

        for path in paths:
            with (
                self.subTest(path=str(path)),
                patch("teatree.dash.views.skills._receipt_path", return_value=path),
                patch("teatree.dash.views.skills.SkillInventory.load", return_value=SkillInventory((), (), (), (), ())),
                patch("teatree.dash.views.skills.skills_declared_in_apm_manifest", return_value=[]),
                patch("teatree.dash.views.skills.skill_demands_by_overlay", return_value={}),
                patch("teatree.provisioning.skills_cli._run_command", side_effect=AssertionError("GET ran CLI")),
                patch("teatree.provisioning.skill_provenance._run_command", side_effect=AssertionError("GET ran git")),
            ):
                response = self.client.get(reverse("dash:skills"))

            assert response.status_code == 200
            assert b"Stale" in response.content
            assert b"Refresh inventory" in response.content or b"invalid" in response.content.lower()

    def test_refresh_is_post_only_and_invokes_explicit_receipt_refresh(self) -> None:
        with patch("teatree.dash.views.skills.refresh_inventory_receipt") as refresh:
            get_response = self.client.get(reverse("dash:skills_refresh"))
            post_response = self.client.post(reverse("dash:skills_refresh"))

        assert get_response.status_code == 405
        assert post_response.status_code == 302
        refresh.assert_called_once()

    def test_required_row_is_read_only_before_removal_subprocess(self) -> None:
        state = _state(Path("/tmp/skills-page"))
        with (
            patch("teatree.dash.views.skills._load_state", return_value=state),
            patch("teatree.dash.views.skills.HarnessSkillRemovalService") as service,
        ):
            response = self.client.post(
                reverse("dash:skills_remove", args=["codex", "writing-plans"]),
            )

        assert response.status_code == 400
        assert b"read-only" in response.content
        service.assert_not_called()

    def test_optional_removal_uses_service_with_exact_harness_and_name(self) -> None:
        state = _state(Path("/tmp/skills-page"))
        removal = Mock()
        with (
            patch("teatree.dash.views.skills._load_state", return_value=state),
            patch("teatree.dash.views.skills.HarnessSkillRemovalService", return_value=removal),
        ):
            response = self.client.post(reverse("dash:skills_remove", args=["codex", "optional"]))

        assert response.status_code == 302
        call = removal.remove_optional.call_args
        assert call.args == (SkillsHarness.CODEX, "optional")
        assert call.kwargs["load_current_exclusions"]() == []

    def test_partial_removal_failure_reports_the_persisted_state_truthfully(self) -> None:
        state = _state(Path("/tmp/skills-page"))
        target = Mock(exclusion="codex:optional")
        removal = Mock()
        removal.remove_optional.side_effect = HarnessSkillRemovalError(
            RemovalStage.REFRESH,
            target,
            RuntimeError("offline"),
        )
        with (
            patch("teatree.dash.views.skills._load_state", return_value=state),
            patch("teatree.dash.views.skills.HarnessSkillRemovalService", return_value=removal),
        ):
            response = self.client.post(reverse("dash:skills_remove", args=["codex", "optional"]))

        assert response.status_code == 502
        assert b"skill was removed and excluded, but the cached inventory is stale" in response.content

    def test_remove_requires_csrf(self) -> None:
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse("dash:skills_remove", args=["codex", "optional"]))
        assert response.status_code == 403
