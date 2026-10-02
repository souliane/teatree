"""No source file may describe a feature flag with a stage the registry contradicts.

The graduation that motivated this shipped in ``FEATURE_FLAGS`` and nowhere else, so
seven modules went on calling ``directive_loop_enabled`` DARK — including a remediation
string telling an operator to turn on a setting that was already on.
"""

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


def _write(tmp_path: Path, body: str) -> Path:
    module = tmp_path / "module.py"
    module.write_text(body, encoding="utf-8")
    return module


class TestScanFile:
    def test_a_settling_flag_called_dark_is_a_claim(self, tmp_path: Path) -> None:
        source = _write(tmp_path, '"""Armed by the ``directive_loop_enabled`` DARK flag."""\n')
        (claim,) = scan_file(source, _NON_DARK)
        assert claim.flag == "directive_loop_enabled"
        assert claim.stage == FlagStage.SETTLING.value
        assert "directive_loop_enabled" in str(claim)

    def test_the_word_reaches_across_a_docstring_line_break(self, tmp_path: Path) -> None:
        body = '"""A no-op while the flag is dark,\nso ``directive_loop_enabled`` gates nothing."""\n'
        assert len(scan_file(_write(tmp_path, body), _NON_DARK)) == 1

    def test_a_genuinely_dark_flag_is_left_alone(self, tmp_path: Path) -> None:
        source = _write(tmp_path, '"""The canonical ``outer_loop_enabled`` DARK flag."""\n')
        assert FEATURE_FLAGS["outer_loop_enabled"].stage is FlagStage.DARK
        assert scan_file(source, _NON_DARK) == []

    def test_a_graduation_phrase_is_not_a_claim(self, tmp_path: Path) -> None:
        source = _write(tmp_path, '"""``directive_loop_enabled``: graduated DARK->SETTLING by #3895."""\n')
        assert scan_file(source, _NON_DARK) == []

    @pytest.mark.parametrize("word", ["darkroom", "go-dark-mode", "darkly"])
    def test_the_word_must_stand_alone(self, tmp_path: Path, word: str) -> None:
        source = _write(tmp_path, f'"""``directive_loop_enabled`` and the {word} theme."""\n')
        assert scan_file(source, _NON_DARK) == []

    def test_a_string_literal_is_not_prose(self, tmp_path: Path) -> None:
        # A message body or prompt template names symbols it does not describe.
        source = _write(tmp_path, 'BANNER = "directive_loop_enabled is dark"\n')
        assert scan_file(source, _NON_DARK) == []


def test_the_live_tree_carries_no_stale_stage_claim() -> None:
    claims = scan_tree(_SRC, _NON_DARK, exclude=_REGISTRY)
    assert claims == [], "stale flag-stage prose:\n" + "\n".join(str(claim) for claim in claims)
