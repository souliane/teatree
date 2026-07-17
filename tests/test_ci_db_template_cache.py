"""Pin the CI cache wiring for the migrated-DB template (W7-PR2).

The ``test-shard`` job runs 12 matrix jobs, each spinning up ``-n auto`` xdist
workers that share ONE bind-mounted checkout (``docker run -v "$PWD":/app``).
Caching ``.pytest-db-template/`` across runs via ``actions/cache`` lets the
FIRST worker of a fresh runner restore a still-valid template from a prior run
instead of building one from scratch. A stale cache must never be silently
reused: the key has NO ``restore-keys`` fallback, so a hash miss is a clean
rebuild — never a fuzzy restore of an outdated schema.
"""

import re
from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

_CACHE_PATH = ".pytest-db-template"
_CACHE_KEY_INPUTS = (
    "src/**/migrations/*.py",
    "uv.lock",
    "tests/django_settings.py",
    "dev/Dockerfile.test",
)


def _ci_jobs() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"])


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in job.get("steps", []) if isinstance(s, dict)]


def _cache_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in _steps(job) if str(s.get("uses", "")).startswith("actions/cache@")]


class TestDbTemplateCacheStep:
    def test_test_shard_job_has_exactly_one_cache_step(self) -> None:
        job = _ci_jobs()["test-shard"]
        cache_steps = _cache_steps(job)
        assert len(cache_steps) == 1, (
            f"expected exactly one 'actions/cache@...' step in test-shard; found {len(cache_steps)}"
        )

    def test_cache_action_is_pinned_by_full_commit_sha(self) -> None:
        (step,) = _cache_steps(_ci_jobs()["test-shard"])
        uses = step["uses"]
        assert re.fullmatch(r"actions/cache@[0-9a-f]{40}", uses), (
            f"actions/cache must be pinned by a full 40-char commit SHA like every other action in ci.yml; got {uses!r}"
        )

    def test_cache_path_is_the_db_template_dir(self) -> None:
        (step,) = _cache_steps(_ci_jobs()["test-shard"])
        assert step["with"]["path"] == _CACHE_PATH

    def test_cache_key_hashes_every_schema_shaping_input(self) -> None:
        (step,) = _cache_steps(_ci_jobs()["test-shard"])
        key = step["with"]["key"]
        assert "hashFiles(" in key
        for glob in _CACHE_KEY_INPUTS:
            assert glob in key, f"cache key must hash {glob!r} (an input to schema_hash()); got {key!r}"

    def test_cache_key_has_no_restore_keys_fallback(self) -> None:
        (step,) = _cache_steps(_ci_jobs()["test-shard"])
        assert "restore-keys" not in step["with"], (
            "a stale template must never be silently restored on a key miss — exact key only, no restore-keys fallback"
        )

    def test_cache_step_runs_before_the_image_pull_step(self) -> None:
        job = _ci_jobs()["test-shard"]
        steps = _steps(job)
        cache_idx = next(i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/cache@"))
        image_idx = next(i for i, s in enumerate(steps) if s.get("name") == "Obtain the prebuilt test image")
        assert cache_idx < image_idx, "the template cache must be restored before the shard runs pytest"
