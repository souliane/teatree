"""No source file may describe a feature flag with a stage the registry contradicts."""

from pathlib import Path

import pytest

from teatree.config import feature_flags
from teatree.config.feature_flags import FEATURE_FLAGS, FlagStage
from teatree.quality.flag_stage_prose import scan_file, scan_tree

_SRC = Path(__file__).resolve().parents[2] / "src" / "teatree"

#: The module whose ``stage`` field IS the answer, so its prose cannot contradict it: it
#: legitimately names which flags left ``DARK``, and ``tests/config/test_feature_flags.py``
#: is what keeps it honest.
_REGISTRY = Path(feature_flags.__file__)

_NON_DARK = {name: flag.stage.value for name, flag in FEATURE_FLAGS.items() if flag.stage is not FlagStage.DARK}
_SETTLING_FIXTURE = {"example_setting": FlagStage.SETTLING.value}


def _write(tmp_path: Path, body: str) -> Path:
    module = tmp_path / "module.py"
    module.write_text(body, encoding="utf-8")
    return module


class TestScanFile:
    def test_a_settling_flag_called_dark_is_a_claim(self, tmp_path: Path) -> None:
        source = _write(tmp_path, '"""Armed by the ``example_setting`` DARK flag."""\n')
        (claim,) = scan_file(source, _SETTLING_FIXTURE)
        assert claim.flag == "example_setting"
        assert claim.stage == FlagStage.SETTLING.value
        assert "example_setting" in str(claim)

    def test_the_word_reaches_across_a_docstring_line_break(self, tmp_path: Path) -> None:
        body = '"""A no-op while the flag is dark,\nso ``example_setting`` gates nothing."""\n'
        assert len(scan_file(_write(tmp_path, body), _SETTLING_FIXTURE)) == 1

    def test_a_genuinely_dark_flag_is_left_alone(self, tmp_path: Path) -> None:
        source = _write(tmp_path, '"""The canonical ``example_setting`` DARK flag."""\n')
        non_dark_flags = {"another_setting": FlagStage.SETTLING.value}
        assert scan_file(source, non_dark_flags) == []

    def test_a_graduation_phrase_is_not_a_claim(self, tmp_path: Path) -> None:
        source = _write(tmp_path, '"""``example_setting``: graduated DARK->SETTLING by #3895."""\n')
        assert scan_file(source, _SETTLING_FIXTURE) == []

    @pytest.mark.parametrize("word", ["darkroom", "go-dark-mode", "darkly"])
    def test_the_word_must_stand_alone(self, tmp_path: Path, word: str) -> None:
        source = _write(tmp_path, f'"""``example_setting`` and the {word} theme."""\n')
        assert scan_file(source, _SETTLING_FIXTURE) == []

    def test_a_string_literal_is_not_prose(self, tmp_path: Path) -> None:
        # A message body or prompt template names symbols it does not describe.
        source = _write(tmp_path, 'BANNER = "example_setting is dark"\n')
        assert scan_file(source, _SETTLING_FIXTURE) == []


def test_the_live_tree_carries_no_stale_stage_claim() -> None:
    claims = scan_tree(_SRC, _NON_DARK, exclude=_REGISTRY)
    assert claims == [], "stale flag-stage prose:\n" + "\n".join(str(claim) for claim in claims)
