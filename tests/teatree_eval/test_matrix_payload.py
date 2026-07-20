"""Parse a ``t3 eval run --models ... --format json`` matrix payload.

Pure structural validation — the reader counterpart of
``teatree.eval.matrix.render_matrix_json``.
"""

from pathlib import Path

import pytest

from teatree.eval.matrix_payload import MatrixPayloadError, load_matrix_payload

_VALID_JSON = """
{
  "models": ["claude-haiku-4-5", "claude-sonnet-5"],
  "scenarios": [
    {"name": "alpha", "results": {"claude-haiku-4-5": {"passed": true, "skipped": false, "errored": false}}},
    {"name": "beta", "results": {"claude-haiku-4-5": null}}
  ]
}
"""


class TestLoadMatrixPayload:
    def test_parses_the_render_matrix_json_shape(self, tmp_path: Path) -> None:
        path = tmp_path / "matrix.json"
        path.write_text(_VALID_JSON, encoding="utf-8")
        payload = load_matrix_payload(path)
        assert payload.models == ["claude-haiku-4-5", "claude-sonnet-5"]
        assert [entry.name for entry in payload.scenarios] == ["alpha", "beta"]
        assert payload.scenarios[0].results["claude-haiku-4-5"] == {
            "passed": True,
            "skipped": False,
            "errored": False,
        }
        assert payload.scenarios[1].results["claude-haiku-4-5"] is None

    def test_not_valid_json_is_fail_loud(self, tmp_path: Path) -> None:
        path = tmp_path / "matrix.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(MatrixPayloadError, match="not valid JSON"):
            load_matrix_payload(path)

    def test_non_object_top_level_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "matrix.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(MatrixPayloadError, match="must be an object"):
            load_matrix_payload(path)

    def test_missing_models_key_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "matrix.json"
        path.write_text('{"scenarios": []}', encoding="utf-8")
        with pytest.raises(MatrixPayloadError, match="'models'"):
            load_matrix_payload(path)

    def test_non_list_scenarios_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "matrix.json"
        path.write_text('{"models": [], "scenarios": {}}', encoding="utf-8")
        with pytest.raises(MatrixPayloadError, match="'scenarios'"):
            load_matrix_payload(path)

    def test_scenario_entry_missing_name_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "matrix.json"
        path.write_text('{"models": [], "scenarios": [{"results": {}}]}', encoding="utf-8")
        with pytest.raises(MatrixPayloadError, match="'name'"):
            load_matrix_payload(path)

    def test_scenario_entry_missing_results_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "matrix.json"
        path.write_text('{"models": [], "scenarios": [{"name": "alpha"}]}', encoding="utf-8")
        with pytest.raises(MatrixPayloadError, match="'results'"):
            load_matrix_payload(path)
