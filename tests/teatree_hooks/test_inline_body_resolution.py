"""Unit tests for inline ``--body``/``--description`` value resolution (F7.4).

Direct coverage of the ``$VAR`` liveness rule the leak-gate body extractor
relies on: double-quoted / unquoted forms env-resolve, a single-quoted ``'$VAR'``
is inert literal text scanned verbatim, and an absent env var yields the
unavailable-body-source sentinel. Synthetic term ``acmecorp`` only.
"""

import pytest

from teatree.hooks._command_parser import UNAVAILABLE_BODY_SOURCE_SENTINEL, is_unavailable_body_source_sentinel
from teatree.hooks._inline_body_resolution import _var_ref_is_live, resolve_inline_body_value


class TestVarRefIsLive:
    """F7.4: which ``$VAR`` raw spans bash would expand (env-resolvable)."""

    @pytest.mark.parametrize("raw", ["$VAR", "${VAR}", '"$VAR"', '"${VAR}"', ""])
    def test_live_forms(self, raw: str) -> None:
        assert _var_ref_is_live(raw) is True

    @pytest.mark.parametrize("raw", ["'$VAR'", "'${VAR}'"])
    def test_single_quoted_is_inert(self, raw: str) -> None:
        assert _var_ref_is_live(raw) is False


class TestResolveInlineBodyValue:
    """F7.4: resolution of a whole-value ``$VAR`` body from the hook env."""

    def test_unquoted_present_var_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BODYV", "ship to acmecorp")
        assert resolve_inline_body_value("$BODYV", None, raw="$BODYV") == "ship to acmecorp"

    def test_double_quoted_present_var_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BODYV", "ship to acmecorp")
        assert resolve_inline_body_value("$BODYV", None, raw='"$BODYV"') == "ship to acmecorp"

    def test_unquoted_absent_var_is_unavailable_sentinel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BODYV", raising=False)
        out = resolve_inline_body_value("$BODYV", None, raw="$BODYV")
        assert out == UNAVAILABLE_BODY_SOURCE_SENTINEL
        assert is_unavailable_body_source_sentinel(out)

    def test_single_quoted_var_returned_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A single-quoted '$BODYV' is the literal published body, NOT an env
        # reference -- even with the env set, it is scanned verbatim.
        monkeypatch.setenv("BODYV", "ship to acmecorp")
        assert resolve_inline_body_value("$BODYV", None, raw="'$BODYV'") == "$BODYV"
