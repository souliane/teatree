"""Guard: the test-shard job caches the migrated test-DB template across CI runs.

``tests/conftest.py::django_db_setup`` (perf(ci): skip redundant
per-xdist-worker migrate) builds ONE migrated sqlite template per job and
restores every xdist worker from it instead of re-running migrations. This
pins the complementary CI-level win: the template itself is cached across
separate ``test-shard`` job invocations (all twelve matrix legs share one
key), keyed on exactly the inputs that can change its contents, so a hash
miss on migrations/lockfile/settings never restores a stale/wrong-schema
template — it just falls back to an in-job build, same as today.
"""

from pathlib import Path

import yaml

_CI = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict:
    return yaml.safe_load(_CI.read_text(encoding="utf-8"))


def _test_shard_steps() -> list[dict]:
    return _workflow()["jobs"]["test-shard"]["steps"]


def _cache_step() -> dict:
    for step in _test_shard_steps():
        if step.get("uses", "").startswith("actions/cache@"):
            return step
    msg = "test-shard must have an actions/cache step for the migrated test-DB template"
    raise AssertionError(msg)


class TestTemplateDbCacheStep:
    def test_cache_step_targets_the_template_dir(self) -> None:
        assert _cache_step()["with"]["path"] == ".cache/django-test-template"

    def test_cache_key_is_scoped_to_migrations_lockfile_and_settings(self) -> None:
        key = _cache_step()["with"]["key"]
        assert "hashFiles(" in key
        for source in ("src/**/migrations/*.py", "uv.lock", "tests/django_settings.py"):
            assert source in key, f"cache key must hash {source} — an unrelated change must never invalidate it"

    def test_cache_key_has_no_restore_keys_fallback(self) -> None:
        # A restore-keys fallback would let a hash MISS still restore an older
        # (possibly stale-schema) template. Exact-key-only means a miss always
        # falls back to a correct in-job build instead.
        assert "restore-keys" not in _cache_step()

    def test_cache_step_runs_before_the_shard_test_step(self) -> None:
        steps = _test_shard_steps()
        cache_index = next(i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/cache@"))
        test_index = next(i for i, s in enumerate(steps) if str(s.get("name", "")).lower().startswith("test shard"))
        assert cache_index < test_index

    def test_shard_run_command_points_at_the_cached_template_dir(self) -> None:
        steps = _test_shard_steps()
        test_step = next(s for s in steps if str(s.get("name", "")).lower().startswith("test shard"))
        assert "TEATREE_TEST_DB_TEMPLATE_DIR=/app/.cache/django-test-template" in test_step["run"]
