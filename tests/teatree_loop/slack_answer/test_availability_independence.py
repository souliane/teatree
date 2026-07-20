"""Invariant: the reactive Slack-answer cycle is never gated by availability.

Owner contract I1/I2 (slack-comms design): when the owner writes, the box
answers immediately regardless of availability mode. Availability governs only
the box's OWN outbound questions. This locks that the answer cycle never grows
an availability gate — a regression that imported ``resolve_mode`` /
``defers_questions`` into this package would re-introduce the exact pause the
owner reported (an owner DM in ``autonomous_away`` going unanswered).
"""

import pkgutil
from pathlib import Path

import teatree.loop.slack_answer as slack_answer_pkg

_FORBIDDEN = ("resolve_mode", "defers_questions", "availability")


def _package_sources() -> list[tuple[str, str]]:
    root = Path(slack_answer_pkg.__file__).parent
    sources: list[tuple[str, str]] = []
    for mod in pkgutil.iter_modules([str(root)]):
        path = root / f"{mod.name}.py"
        if path.exists():
            sources.append((mod.name, path.read_text(encoding="utf-8")))
    return sources


class TestReactiveCycleIgnoresAvailability:
    def test_no_module_references_availability(self) -> None:
        offenders: list[str] = [
            f"{name}:{token}" for name, source in _package_sources() for token in _FORBIDDEN if token in source
        ]
        assert offenders == [], f"the reactive Slack-answer cycle must never consult availability — found: {offenders}"
