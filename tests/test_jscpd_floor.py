"""Pin the jscpd duplication-floor calibration (#1984).

At the old floor (minLines 50 / minTokens 300) the gate caught zero clones —
no small-helper copy-paste is 50 lines long. The anti-slop calibration is
minLines 6 / minTokens 40. The 2% warn-tolerance keeps the gate report-only
in this PR (the ~124-clone dedup burst is cleared in the follow-up PR, which
flips the tolerance back to 0 = block).
"""

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_JSCPD_CONFIG = _REPO_ROOT / ".jscpd.json"

MAX_MIN_LINES = 6
MAX_MIN_TOKENS = 40


class TestJscpdFloor:
    def test_min_lines_floor_catches_small_helpers(self) -> None:
        config = json.loads(_JSCPD_CONFIG.read_text(encoding="utf-8"))
        assert config["minLines"] <= MAX_MIN_LINES, (
            f"jscpd minLines is {config['minLines']}; small-helper copy-paste is "
            f"under {MAX_MIN_LINES} lines, so a higher floor makes the gate vacuous."
        )

    def test_min_tokens_floor_catches_small_helpers(self) -> None:
        config = json.loads(_JSCPD_CONFIG.read_text(encoding="utf-8"))
        assert config["minTokens"] <= MAX_MIN_TOKENS, (
            f"jscpd minTokens is {config['minTokens']}; raising it past "
            f"{MAX_MIN_TOKENS} re-vacuates the small-helper duplication gate."
        )

    def test_threshold_present(self) -> None:
        config = json.loads(_JSCPD_CONFIG.read_text(encoding="utf-8"))
        assert "threshold" in config, "jscpd threshold must be declared explicitly."
