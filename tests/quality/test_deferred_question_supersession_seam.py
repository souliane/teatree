"""Ratchet: no supersession query bypasses ``DeferredQuestion.supersedable`` (#4749).

``supersedable()`` exists to carry three narrowings a bare ``pending()`` does not — never
supersede a DELIVERED row, scope by audience, and match nothing rather than widen to the
whole session when the run is unnameable. ``pending().filter(session_id=…, run_id=…)``
reads as equivalent and is not, and it regressed in a NEW function within hours of the
first fix, erasing a loop-driven question before delivery.

The assertion is set equality, so both directions fail on purpose: a NEW bypass is not in
:data:`ACCEPTED` and turns this red, and an accepted line whose call site was converted
also turns it red, so the ledger can only shrink. :data:`ACCEPTED` is empty — the tree is
clean as far as this detector reads — and nothing may be added to it.
"""

# test-path: cross-cutting — a whole-tree quality gate over every supersession call site.
from pathlib import Path

import pytest

from teatree.quality.deferred_question_supersession import SupersessionBypass, scan_bypasses

_REPO = Path(__file__).resolve().parents[2]
_ROOTS = (_REPO / "src" / "teatree", _REPO / "hooks" / "scripts")

#: Deliberate exceptions, each one a call site reviewed and kept. Shrink-only: never add.
ACCEPTED: frozenset[str] = frozenset()


@pytest.fixture(scope="module")
def bypasses() -> tuple[SupersessionBypass, ...]:
    return tuple(scan_bypasses(*_ROOTS))


class TestSupersessionRatchet:
    def test_no_unlisted_bypass(self, bypasses: tuple[SupersessionBypass, ...]) -> None:
        unlisted = sorted(b.key for b in bypasses if b.key not in ACCEPTED)

        assert unlisted == [], (
            "pending().filter(session_id=…/run_id=…) bypasses DeferredQuestion.supersedable(), "
            f"dropping the delivered/audience/unnameable-run guards: {unlisted}"
        )

    def test_the_ledger_carries_no_converted_call_site(self, bypasses: tuple[SupersessionBypass, ...]) -> None:
        stale = sorted(ACCEPTED - {b.key for b in bypasses})

        assert stale == [], f"converted call sites still in ACCEPTED — delete these lines: {stale}"

    def test_the_seam_itself_is_still_reachable(self) -> None:
        # A detector that found the tree clean because it scanned nothing proves nothing.
        assert any(root.is_dir() for root in _ROOTS)
        assert (_REPO / "src" / "teatree" / "core" / "models" / "deferred_question.py").read_text().count(
            "def supersedable"
        ) == 1
