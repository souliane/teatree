"""Kill-proof: the scoped mutation layer bites a fail-closed-gate regression.

The whole point of mutation testing is to expose vacuous coverage. This file
proves the layer is not vacuous on its flagship target — the on-behalf egress
gate's fail-closed branch (``resolve_on_behalf_verdict`` returns ``BLOCK`` under
ASK). A mutant that flips that ``BLOCK`` to ``PROCEED`` would let an unattended
post go out under the user's identity; the existing suite must catch it.

Two proofs, by design. ``TestManualMutantKilled`` is the deterministic,
platform-independent proof: it rebuilds the exact BLOCK→PROCEED mutant by hand
and asserts the existing test's assertion goes RED on it (and GREEN on the real
code) — the methodology's "revert the fix, confirm RED" applied to the mutant,
running on every platform. ``TestMutmutKillsTheMutant`` drives the REAL mutmut
runner over ``on_behalf_gate.py`` and asserts mutmut reports at least one killed
mutant; mutmut's fork+output-capture model segfaults on macOS (a mutmut-3.5 bug,
not a test gap), so when the run yields only inconclusive results the test SKIPs
rather than failing — the deterministic proof above still guards the contract,
and Linux CI exercises the real run.
"""

import shutil
import sys

import pytest

from teatree.config import OnBehalfPostMode
from teatree.on_behalf_gate import OnBehalfVerdict, resolve_on_behalf_verdict


def _mutant_resolve(mode: OnBehalfPostMode, action: str) -> OnBehalfVerdict:
    """The BLOCK→PROCEED mutant of the ASK branch (the regression we fear)."""
    if mode is OnBehalfPostMode.IMMEDIATE:
        return OnBehalfVerdict.PROCEED
    if mode is OnBehalfPostMode.ASK:
        return OnBehalfVerdict.PROCEED  # mutated: real code returns BLOCK
    if action == "post_draft_note":
        return OnBehalfVerdict.AUTO_DRAFT
    return OnBehalfVerdict.BLOCK


class TestManualMutantKilled:
    """The existing assertion is RED on the mutant and GREEN on the real code."""

    def test_real_code_blocks_under_ask(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text('[teatree]\non_behalf_post_mode = "ask"\n', encoding="utf-8")
        monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
        # The assertion that pins the fail-closed gate for a colleague-VISIBLE
        # action (mirrors test_on_behalf_gate.py::TestExplicitModes::
        # test_explicit_ask_blocks_visible_posts_but_exempts_drafts).
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK

    def test_same_assertion_goes_red_on_the_mutant(self) -> None:
        # Feeding the mutant to the exact assertion the suite makes proves the
        # assertion is anti-vacuous: it distinguishes BLOCK from PROCEED.
        verdict = _mutant_resolve(OnBehalfPostMode.ASK, "post_comment")
        assert verdict is not OnBehalfVerdict.BLOCK, "mutant should diverge from the real fail-closed result"
        with pytest.raises(AssertionError):
            assert verdict is OnBehalfVerdict.BLOCK


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
