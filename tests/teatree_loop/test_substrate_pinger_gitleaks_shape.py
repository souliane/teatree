"""Regression: the substrate-pinger notify call must not false-trip gitleaks (#3344).

``gitleaks``' ``generic-api-key`` rule keys off a ``…key=`` prefix and then
captures the following high-entropy-looking text. When the ``notify_with_fallback``
call was one ~105-char line, the capture ran on past ``idempotency_key=`` onto
``audience=NotifyAudience.INTERNAL`` (an enum reference, no secret) and the repo's
own secret gate false-tripped on a clean checkout. Breaking each kwarg onto its own
line (kept expanded by ruff's magic trailing comma) stops the capture running across
kwargs. This test pins the source shape so a future reflow cannot silently
re-collapse the call and reintroduce the finding.
"""

import re
from pathlib import Path

from teatree.loop import substrate_pinger

# A ``…key=`` assignment followed on the SAME physical line by a later
# ``Name=Enum.MEMBER`` reference — the exact one-line shape gitleaks captured.
_GITLEAKS_CAPTURE_SHAPE = re.compile(r"\w*key\s*=\s*\S+.*\b\w+\s*=\s*\w+\.\w+")


def _source_lines() -> list[str]:
    return Path(substrate_pinger.__file__).read_text(encoding="utf-8").splitlines()


class TestSubstratePingerNotifyShape:
    def test_no_line_carries_a_key_assignment_before_an_enum_kwarg(self) -> None:
        offenders = [line for line in _source_lines() if _GITLEAKS_CAPTURE_SHAPE.search(line)]
        assert offenders == [], (
            "A `...key=` assignment shares a physical line with a later "
            "`Name=Enum.MEMBER` kwarg — gitleaks' generic-api-key rule captures "
            f"onto the enum reference (#3344). Offending line(s): {offenders}"
        )

    def test_idempotency_key_and_audience_are_on_separate_lines(self) -> None:
        lines = _source_lines()
        idempotency = next((i for i, line in enumerate(lines) if "idempotency_key=idempotency_key" in line), None)
        audience = next((i for i, line in enumerate(lines) if "audience=NotifyAudience.INTERNAL" in line), None)
        assert idempotency is not None
        assert audience is not None
        assert idempotency != audience, (
            "idempotency_key and audience kwargs collapsed onto one line — this "
            "reintroduces the gitleaks generic-api-key false positive (#3344)."
        )
