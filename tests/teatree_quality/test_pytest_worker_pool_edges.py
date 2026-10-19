"""Keep the shared worker pool documentation aligned with its implementation."""

from tests.teatree_quality.real_pytest_pool_support import REPO


def test_worker_pool_docs_describe_current_behavior() -> None:
    blueprint = (REPO / "docs/blueprint/admission-governor.md").read_text(encoding="utf-8")
    pool_section = blueprint.split("**Shared pytest worker slots", 1)[1].split("\n\n", 1)[0]
    governor = (REPO / "src/teatree/core/admission_governor.py").read_text(encoding="utf-8")
    assert "TEATREE_PYTEST_WORKER_POOL_ROOT" in pool_section
    assert "capacity.lock" in pool_section
    assert "first pull request" not in pool_section
    assert "live holder guard" not in blueprint
    assert "formerly" not in governor
