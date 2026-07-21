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
        {
            "name": "alpha",
            "results": {
                "claude-haiku-4-5": {
                    "passed": true,
                    "skipped": false,
                    "errored": false,
                    "score": 1.0,
                    "trials": 3
                }
            }
        },
        {"name": "beta", "results": {"claude-haiku-4-5": null}}
    ]
}
"""


def _one_cell_json(cell: str) -> str:
    return f'{{"models": [], "scenarios": [{{"name": "alpha", "results": {{"m": {cell}}}}}]}}'


class TestLoadMatrixPayload:
    def test_parses_the_render_matrix_json_shape(self, tmp_path: Path) -> None:
        path = tmp_path / "matrix.json"
        path.write_text(_VALID_JSON, encoding="utf-8")
        payload = load_matrix_payload(path)
        assert payload.models == ["claude-haiku-4-5", "claude-sonnet-5"]
        assert [entry.name for entry in payload.scenarios] == ["alpha", "beta"]
        cell = payload.scenarios[0].results["claude-haiku-4-5"]
        assert cell is not None
        assert cell.passed is True
        assert cell.skipped is False
        assert cell.errored is False
        assert cell.score == pytest.approx(1.0)
        assert cell.trials == 3
        assert payload.scenarios[1].results["claude-haiku-4-5"] is None

    def test_score_and_trials_default_when_absent(self, tmp_path: Path) -> None:
        path = tmp_path / "matrix.json"
        path.write_text(
            _one_cell_json('{"passed": true, "skipped": false, "errored": false}'),
            encoding="utf-8",
        )
        cell = load_matrix_payload(path).scenarios[0].results["m"]
        assert cell is not None
        assert cell.score == pytest.approx(0.0)
        assert cell.trials == 1

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


class TestCellValidation:
    def test_non_object_cell_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "matrix.json"
        path.write_text(_one_cell_json('"pass"'), encoding="utf-8")
        with pytest.raises(MatrixPayloadError, match="must be an object or null"):
            load_matrix_payload(path)

    def test_missing_verdict_field_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "matrix.json"
        path.write_text(_one_cell_json('{"passed": true, "skipped": false}'), encoding="utf-8")
        with pytest.raises(MatrixPayloadError, match="'errored'"):
            load_matrix_payload(path)

    def test_non_boolean_verdict_field_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "matrix.json"
        path.write_text(
            _one_cell_json('{"passed": "yes", "skipped": false, "errored": false}'),
            encoding="utf-8",
        )
        with pytest.raises(MatrixPayloadError, match="'passed'"):
            load_matrix_payload(path)

    def test_non_numeric_score_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "matrix.json"
        path.write_text(
            _one_cell_json('{"passed": true, "skipped": false, "errored": false, "score": "high"}'),
            encoding="utf-8",
        )
        with pytest.raises(MatrixPayloadError, match="'score'"):
            load_matrix_payload(path)

    def test_non_integer_trials_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "matrix.json"
        path.write_text(
            _one_cell_json('{"passed": true, "skipped": false, "errored": false, "trials": 1.5}'),
            encoding="utf-8",
        )
        with pytest.raises(MatrixPayloadError, match="'trials'"):
            load_matrix_payload(path)
