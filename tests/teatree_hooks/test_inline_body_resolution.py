"""Unit tests for inline ``--body``/``--description`` value resolution (F7.4).

Direct coverage of the ``$VAR`` liveness rule the leak-gate body extractor
relies on: double-quoted / unquoted forms env-resolve, a single-quoted ``'$VAR'``
is inert literal text scanned verbatim, and an absent env var yields the
unavailable-body-source sentinel. Synthetic term ``acmecorp`` only.
"""

from pathlib import Path

import pytest

from teatree.hooks._inline_body_resolution import _var_ref_is_live, resolve_inline_body_value, resolve_short_cluster
from teatree.hooks._parser_primitives import (
    NAMED_FAIL_CLOSED_PREFIX,
    UNAVAILABLE_BODY_SOURCE_SENTINEL,
    is_unavailable_body_source_sentinel,
)


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


class TestResolveShortCluster:
    def test_later_unquoted_attached_value_is_scanned(self) -> None:
        assert resolve_short_cluster(["-sbacmecorp"], ["-sbacmecorp"], frozenset({"-b"}), None) == ["acmecorp"]

    def test_unresolved_later_value_has_named_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_ABSENT_CLUSTER_VAR", raising=False)
        result = resolve_short_cluster(
            ["-sd", "$T3_ABSENT_CLUSTER_VAR"],
            ["-sd", '"$T3_ABSENT_CLUSTER_VAR"'],
            frozenset({"-d"}),
            None,
        )
        assert UNAVAILABLE_BODY_SOURCE_SENTINEL in result
        assert any(value.startswith(NAMED_FAIL_CLOSED_PREFIX) for value in result)

    def test_attached_live_cat_reads_the_file(self, tmp_path: Path) -> None:
        body = tmp_path / "body.md"
        body.write_text("acmecorp", encoding="utf-8")
        value = f"$(cat {body})"
        assert resolve_short_cluster([f"-sm{value}"], [f'-sm"{value}"'], frozenset({"-m"}), None) == ["acmecorp"]
