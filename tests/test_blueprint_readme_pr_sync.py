"""Doc-drift guards for the BLUEPRINT + README sync against merged PRs.

This suite mechanically enforces that the standing documentation stays
aligned with two merged refactors and with the eval-coverage lane, so the
alignment is a RED test rather than prose vigilance:

* **PR #2401** — ``refactor(pricing): single source for Anthropic cache
    multipliers``. The cache read/write multipliers were triplicated across
    ``core/cost``, ``eval/cost_fit`` and ``eval/models``; they now live in one
    foundation-layer leaf ``src/teatree/pricing.py``. BLUEPRINT §5 (the cost /
    model-pricing prose) must name that SSOT so a future reader does not
    re-introduce a private copy.

* **PR #2404** — ``refactor(utils): extract pure remote-URL parsing into
    git_remote``. The pure remote-URL parsers (``slug_from_remote`` /
    ``web_base_from_remote``) moved out of ``utils/git`` into a cohesive
    ``utils/git_remote`` leaf. BLUEPRINT §3 (the package-structure ``utils/``
    line) must name it so the module map does not go stale.

* **Eval-coverage lane** — the bare ``t3 eval`` suite runs *seven* free
    deterministic lanes, one of which is the per-skill ``skill-coverage`` /
    ``t3 eval coverage`` gate. The README skills catalogue is generated from
    SKILL.md frontmatter, so the ``running-evals`` description (the source of
    the catalogue row) must name the coverage lane, and the generated catalogue
    must carry it through.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = REPO_ROOT / "BLUEPRINT.md"
README = REPO_ROOT / "README.md"
RUNNING_EVALS_SKILL = REPO_ROOT / "skills" / "running-evals" / "SKILL.md"


@pytest.fixture(scope="module")
def blueprint_text() -> str:
    return BLUEPRINT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def readme_text() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def agent_execution_section(blueprint_text: str) -> str:
    """The §5 'Agent Execution' body, up to the §6 heading."""
    start = blueprint_text.index("## 5. Agent Execution")
    rest = blueprint_text[start:]
    end = rest.index("## 6. Overlay System")
    return rest[:end]


@pytest.fixture(scope="module")
def package_structure_section(blueprint_text: str) -> str:
    """The §3 'Package Structure' body, up to the §4 heading."""
    start = blueprint_text.index("## 3. Package Structure")
    rest = blueprint_text[start:]
    end = rest.index("## 4. Domain Models")
    return rest[:end]


class TestPricingSsotDocumented:
    """PR #2401 — the cache-multiplier SSOT must be named in the BLUEPRINT."""

    def test_pricing_module_named_in_agent_execution(self, agent_execution_section: str) -> None:
        assert "pricing.py" in agent_execution_section or "teatree.pricing" in agent_execution_section, (
            "BLUEPRINT §5 must name the pricing SSOT module (PR #2401)"
        )

    def test_cache_multipliers_described_as_single_source(self, agent_execution_section: str) -> None:
        lowered = agent_execution_section.lower()
        assert "cache" in lowered
        # The defining property: ONE source, no longer triplicated.
        assert "single source" in lowered or "ssot" in lowered or "one source" in lowered, (
            "BLUEPRINT §5 must describe the cache multipliers as a single source (PR #2401)"
        )

    def test_pr_2401_cited(self, agent_execution_section: str) -> None:
        assert "#2401" in agent_execution_section


class TestGitRemoteExtractionDocumented:
    """PR #2404 — the git_remote leaf must appear in the package map."""

    def test_git_remote_named_in_utils_line(self, package_structure_section: str) -> None:
        assert "git_remote" in package_structure_section, (
            "BLUEPRINT §3 utils/ line must name the git_remote module (PR #2404)"
        )

    def test_pr_2404_cited(self, package_structure_section: str) -> None:
        assert "#2404" in package_structure_section


class TestEvalCoverageLaneInCatalogue:
    """The eval-coverage lane must be visible in the README skills catalogue."""

    def test_running_evals_skill_frontmatter_names_coverage_lane(self) -> None:
        text = RUNNING_EVALS_SKILL.read_text(encoding="utf-8")
        # The frontmatter description is the source the catalogue row is built
        # from; it must name the coverage lane, not just two of the lanes.
        front = text.split("---", 2)[1].lower()
        assert "coverage" in front, "running-evals frontmatter description must name the eval-coverage lane"

    def test_readme_catalogue_row_names_coverage_lane(self, readme_text: str) -> None:
        begin = readme_text.index("<!-- BEGIN SKILLS -->")
        end = readme_text.index("<!-- END SKILLS -->")
        catalogue = readme_text[begin:end]
        # Locate the running-evals row and assert it carries the coverage lane.
        row_start = catalogue.index("| `running-evals` |")
        row = catalogue[row_start : catalogue.index("\n", row_start)]
        assert "coverage" in row.lower(), "README skills catalogue running-evals row must name the eval-coverage lane"
