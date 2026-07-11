"""The shared Django-free scalar/list coercers (config §3d #5).

The single home both the hot ``setting_parsers`` and the cold ``cold_reader``
import, so the ``bool``/``int`` subclass trap (#258) and the one intentional
hot-vs-cold divergence — coercing a JSON string ``"5"`` — are pinned in ONE place
as the explicit ``accept_numeric_str`` argument, not two copies aligned by comment.

Pure-logic coercers: unit-tested directly per the Test-Writing Doctrine.
"""

import pytest

from teatree.config import value_coercion


class TestStrictBool:
    def test_real_bool_passes(self) -> None:
        assert value_coercion.strict_bool(raw=True) is True
        assert value_coercion.strict_bool(raw=False) is False

    @pytest.mark.parametrize("bad", ["false", "true", 1, 0, [], 1.0])
    def test_non_bool_raises(self, bad: object) -> None:
        # ``bool("false") == True`` would silently ENABLE an opt-in safety setting.
        with pytest.raises(ValueError, match="Invalid bool value"):
            value_coercion.strict_bool(bad)


class TestStrictIntNumericStrDivergence:
    """The cold-vs-hot ``"5"`` divergence is the ``accept_numeric_str`` argument."""

    def test_hot_path_coerces_numeric_string(self) -> None:
        assert value_coercion.strict_int("5", accept_numeric_str=True) == 5
        assert value_coercion.strict_int(" 7 ", accept_numeric_str=True) == 7

    def test_cold_path_rejects_numeric_string(self) -> None:
        # Defense-in-depth: the cold reader's only writer stores canonical JSON ints.
        with pytest.raises(TypeError, match="Invalid int value"):
            value_coercion.strict_int("5", accept_numeric_str=False)

    def test_real_int_passes_on_both_policies(self) -> None:
        assert value_coercion.strict_int(5, accept_numeric_str=True) == 5
        assert value_coercion.strict_int(5, accept_numeric_str=False) == 5

    def test_bool_rejected_when_numeric_str_accepted(self) -> None:
        # ``int(True) == 1`` — a JSON ``true`` for an int setting must never coerce.
        with pytest.raises(TypeError, match="a boolean is not an integer"):
            value_coercion.strict_int(raw=True, accept_numeric_str=True)

    def test_bool_rejected_when_numeric_str_rejected(self) -> None:
        with pytest.raises(TypeError, match="a boolean is not an integer"):
            value_coercion.strict_int(raw=True, accept_numeric_str=False)

    def test_float_rejected(self) -> None:
        with pytest.raises(TypeError, match="Invalid int value"):
            value_coercion.strict_int(5.0, accept_numeric_str=True)


class TestStrictFloat:
    def test_accepts_int_and_float_and_numeric_str(self) -> None:
        assert value_coercion.strict_float(25) == pytest.approx(25.0)
        assert value_coercion.strict_float(1.5) == pytest.approx(1.5)
        assert value_coercion.strict_float("2.5") == pytest.approx(2.5)

    def test_bool_rejected(self) -> None:
        with pytest.raises(TypeError, match="a boolean is not a float"):
            value_coercion.strict_float(raw=True)

    def test_numeric_str_rejected_when_opted_out(self) -> None:
        with pytest.raises(TypeError, match="Invalid float value"):
            value_coercion.strict_float("2.5", accept_numeric_str=False)


class TestStrictStr:
    def test_real_str_passes(self) -> None:
        assert value_coercion.strict_str("hi") == "hi"

    @pytest.mark.parametrize("bad", [True, 1, 1.0, ["a"]])
    def test_non_str_raises(self, bad: object) -> None:
        with pytest.raises(TypeError, match="Invalid str value"):
            value_coercion.strict_str(bad)


class TestStrictStrList:
    def test_list_coerces_each_element(self) -> None:
        assert value_coercion.strict_str_list(["a", "b"]) == ["a", "b"]
        assert value_coercion.strict_str_list([1, 2]) == ["1", "2"]

    @pytest.mark.parametrize("bad", [True, 1, "a", {"k": "v"}])
    def test_non_list_scalar_raises(self, bad: object) -> None:
        # A scalar for a list-typed setting must be LOUD, never a masked ``[]``.
        with pytest.raises(TypeError, match="expected a JSON/TOML array"):
            value_coercion.strict_str_list(bad)
