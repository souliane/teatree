"""Loader-doctrine tests for the committed factory-score recipe (SIG-PR-2).

Pins every fake-green escape closed: a missing/malformed file, weights that do
not sum to 1.0, an unknown/missing SIGNALS id, and a missing/forbidden cap all
raise :class:`RecipeError` at load time — never a default recipe, never a
silently-dropped weight. A checked-in-file conformance test loads the REAL
``evals/recipe.yaml`` and asserts it names exactly the SIGNALS registry ids, so
the committed file can never drift from the code.
"""

from pathlib import Path

import pytest
import yaml

from teatree.core.factory.factory_recipe import (
    CAP_REQUIRED_IDS,
    RECIPE_PATH,
    Recipe,
    RecipeError,
    load_recipe,
    recipe_sha,
)
from teatree.core.factory.factory_signals import SIGNALS

_REGISTRY_IDS = frozenset(spec.provider_id for spec in SIGNALS)


def _valid_payload() -> dict:
    return {
        "version": 1,
        "coverage_floor": 0.6,
        "signals": {
            "first_try_green": {"weight": 0.25, "red_when": 0.5},
            "defect_escape": {"weight": 0.25, "red_when": 0.5},
            "review_catch": {"weight": 0.20, "red_when": 0.0},
            "merge_latency": {"weight": 0.15, "cap": 48.0},
            "repair_burn": {"weight": 0.15, "cap": 5.0},
        },
    }


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "recipe.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


class TestCommittedRecipeConformance:
    def test_committed_recipe_loads(self) -> None:
        recipe = load_recipe()
        assert isinstance(recipe, Recipe)
        assert recipe.version >= 1

    def test_committed_recipe_names_exactly_the_registry_ids(self) -> None:
        # The checked-in file cannot drift from the SIGNALS registry.
        recipe = load_recipe()
        assert recipe.provider_ids == _REGISTRY_IDS

    def test_recipe_sha_is_stable_and_matches_loaded(self) -> None:
        assert recipe_sha() == recipe_sha()
        assert load_recipe().recipe_sha == recipe_sha()

    def test_recipe_sha_changes_when_the_file_changes(self, tmp_path: Path) -> None:
        payload = _valid_payload()
        first = recipe_sha(_write(tmp_path, payload))
        payload["signals"]["first_try_green"]["weight"] = 0.30
        payload["signals"]["defect_escape"]["weight"] = 0.20
        second = recipe_sha(_write(tmp_path, payload))
        assert first != second


class TestLoaderFailsLoud:
    def test_missing_file_is_recipe_error_not_default(self, tmp_path: Path) -> None:
        with pytest.raises(RecipeError, match="missing"):
            load_recipe(tmp_path / "absent.yaml")

    def test_missing_file_recipe_sha_is_recipe_error(self, tmp_path: Path) -> None:
        with pytest.raises(RecipeError, match="missing"):
            recipe_sha(tmp_path / "absent.yaml")

    def test_non_mapping_top_level_is_error(self, tmp_path: Path) -> None:
        with pytest.raises(RecipeError, match="top-level mapping"):
            load_recipe(_write(tmp_path, [1, 2, 3]))

    def test_weights_not_summing_to_one_is_error(self, tmp_path: Path) -> None:
        payload = _valid_payload()
        payload["signals"]["first_try_green"]["weight"] = 0.90
        with pytest.raises(RecipeError, match="sum to"):
            load_recipe(_write(tmp_path, payload))

    def test_unknown_provider_id_is_error(self, tmp_path: Path) -> None:
        payload = _valid_payload()
        payload["signals"]["not_a_signal"] = {"weight": 0.0}
        with pytest.raises(RecipeError, match="unknown signal id"):
            load_recipe(_write(tmp_path, payload))

    def test_missing_registry_id_is_error(self, tmp_path: Path) -> None:
        payload = _valid_payload()
        del payload["signals"]["review_catch"]
        with pytest.raises(RecipeError, match="must name every SIGNALS"):
            load_recipe(_write(tmp_path, payload))

    @pytest.mark.parametrize("capped_id", sorted(CAP_REQUIRED_IDS))
    def test_missing_cap_on_magnitude_signal_is_error(self, tmp_path: Path, capped_id: str) -> None:
        payload = _valid_payload()
        del payload["signals"][capped_id]["cap"]
        with pytest.raises(RecipeError, match="requires a 'cap'"):
            load_recipe(_write(tmp_path, payload))

    def test_forbidden_cap_on_rate_signal_is_error(self, tmp_path: Path) -> None:
        payload = _valid_payload()
        payload["signals"]["first_try_green"]["cap"] = 1.0
        with pytest.raises(RecipeError, match="must not carry a 'cap'"):
            load_recipe(_write(tmp_path, payload))

    def test_missing_weight_is_error(self, tmp_path: Path) -> None:
        payload = _valid_payload()
        del payload["signals"]["first_try_green"]["weight"]
        # Redistribute so it is the missing-weight, not the sum, that trips.
        payload["signals"]["defect_escape"]["weight"] = 0.50
        with pytest.raises(RecipeError, match="missing required 'weight'"):
            load_recipe(_write(tmp_path, payload))

    def test_coverage_floor_out_of_unit_range_is_error(self, tmp_path: Path) -> None:
        payload = _valid_payload()
        payload["coverage_floor"] = 1.5
        with pytest.raises(RecipeError, match="within \\[0, 1\\]"):
            load_recipe(_write(tmp_path, payload))

    def test_committed_path_points_at_evals_recipe_yaml(self) -> None:
        assert RECIPE_PATH.name == "recipe.yaml"
        assert RECIPE_PATH.parent.name == "evals"
