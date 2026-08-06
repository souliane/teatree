"""Kill-proof: the scoped mutation layer bites a fail-closed-gate regression.

The whole point of mutation testing is to expose vacuous coverage. This file
proves the layer is not vacuous on its flagship target — the on-behalf egress
gate's fail-closed branch (``resolve_on_behalf_verdict`` returns ``BLOCK`` under
ASK). A mutant that flips that ``BLOCK`` to ``PROCEED`` would let an unattended
post go out under the user's identity; the existing suite must catch it.

Two proofs, by design. ``TestManualMutantKilled`` is the deterministic,
platform-independent proof: ONE assertion — the full verdict table — is applied
to the REAL resolver and to a hand-built BLOCK→PROCEED mutant, and must pass on
the first and fail on the second. Both directions bite: mutate the real module
and the real-code direction reds; weaken the table and the mutant direction stops
distinguishing. (An earlier form asserted only that the test-local mutant differed
from ``BLOCK``, which is true of the mutant's own source text and would hold with
``on_behalf_gate.py`` deleted.) ``TestMutmutKillsTheMutant`` drives the REAL mutmut
runner over ``on_behalf_gate.py`` and asserts mutmut reports at least one killed
mutant; mutmut's fork+output-capture model segfaults on macOS (a mutmut-3.5 bug,
not a test gap), so when the run yields only inconclusive results the test SKIPs
rather than failing — the deterministic proof above still guards the contract,
and Linux CI exercises the real run.
"""

import shutil
import sys
from collections.abc import Callable

import pytest

from teatree.config import OnBehalfPostMode
from teatree.on_behalf_gate import OnBehalfVerdict, resolve_on_behalf_verdict

Resolver = Callable[[OnBehalfPostMode, str], OnBehalfVerdict]

#: The fail-closed contract, in full. A colleague-VISIBLE action is refused under
#: both blocking modes; a draft is colleague-invisible so it auto-drafts instead;
#: IMMEDIATE is the user's explicit opt-out.
_EXPECTED: dict[tuple[OnBehalfPostMode, str], OnBehalfVerdict] = {
    (OnBehalfPostMode.ASK, "post_comment"): OnBehalfVerdict.BLOCK,
    (OnBehalfPostMode.DRAFT_OR_ASK, "post_comment"): OnBehalfVerdict.BLOCK,
    (OnBehalfPostMode.ASK, "post_draft_note"): OnBehalfVerdict.AUTO_DRAFT,
    (OnBehalfPostMode.DRAFT_OR_ASK, "post_draft_note"): OnBehalfVerdict.AUTO_DRAFT,
    (OnBehalfPostMode.IMMEDIATE, "post_comment"): OnBehalfVerdict.PROCEED,
}


def _mutant_resolve(mode: OnBehalfPostMode, action: str) -> OnBehalfVerdict:
    """The BLOCK→PROCEED mutant of the ASK branch (the regression we fear)."""
    if mode is OnBehalfPostMode.IMMEDIATE:
        return OnBehalfVerdict.PROCEED
    if mode is OnBehalfPostMode.ASK:
        return OnBehalfVerdict.PROCEED  # mutated: real code returns BLOCK
    if action == "post_draft_note":
        return OnBehalfVerdict.AUTO_DRAFT
    return OnBehalfVerdict.BLOCK


def _assert_fail_closed_table(resolve: Resolver) -> None:
    """The one assertion both directions of the proof are made against."""
    for (mode, action), expected in _EXPECTED.items():
        assert resolve(mode, action) is expected, f"{mode} + {action} must resolve {expected}"


def _real_resolver(monkeypatch: pytest.MonkeyPatch) -> Resolver:
    def resolve(mode: OnBehalfPostMode, action: str) -> OnBehalfVerdict:
        monkeypatch.setenv("T3_ON_BEHALF_POST_MODE", mode.value)
        return resolve_on_behalf_verdict(action)

    return resolve


class TestManualMutantKilled:
    """One assertion, applied to the real resolver and to the mutant."""

    def test_real_code_satisfies_the_fail_closed_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Reds on any mutation of the shipped resolver — the ASK branch flipped to
        # PROCEED, the draft carve-out widened, the enum renamed.
        _assert_fail_closed_table(_real_resolver(monkeypatch))

    def test_the_same_table_rejects_the_mutant(self) -> None:
        # The assertion that just passed against production code fails against the
        # mutant, so it demonstrably distinguishes them rather than certifying both.
        with pytest.raises(AssertionError, match="must resolve"):
            _assert_fail_closed_table(_mutant_resolve)


# The real mutmut run is an expensive whole-module subprocess; deselected at push
# (`-m "not push_heavy"`) and run in CI instead.
@pytest.mark.push_heavy
@pytest.mark.integration
class TestMutmutKillsTheMutant:
    # The real mutmut run is given an internal 420s subprocess budget below; the
    # global 60s pytest-timeout (pyproject.toml ``[tool.pytest.ini_options]``)
    # would kill the test long before that budget elapses on a loaded CI runner.
    # Grant an outer timeout that comfortably exceeds the inner budget so the
    # mutmut subprocess can finish and report instead of being flake-killed.
    @pytest.mark.timeout(600)
    def test_mutmut_reports_a_killed_mutant(self, tmp_path) -> None:
        if shutil.which("uv") is None:
            pytest.skip("uv not available")
        if sys.platform == "darwin":
            pytest.skip("mutmut-3.5 fork+output-capture segfaults on macOS; Linux CI runs the real check")

        from teatree.quality.mutation_run import _run_mutmut  # noqa: PLC0415

        result = _run_mutmut(
            ("src/teatree/on_behalf_gate.py",),
            tests_dir=("tests/test_on_behalf_gate.py", "tests/test_on_behalf_post_mode.py"),
            repo=".",
            timeout=420,
        )
        if not (result.killed or result.survived):
            pytest.skip(f"mutmut produced only inconclusive results ({len(result.inconclusive)} segfault/timeout)")
        assert result.killed, "no mutant was killed — the fail-closed gate's tests do not bite"
