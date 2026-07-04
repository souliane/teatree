import dataclasses
import datetime
import enum
import io
import json

from teatree.core.machine_output import emit, to_jsonable


class _Color(enum.Enum):
    RED = "red"
    RANK = 3


@dataclasses.dataclass
class _Row:
    name: str
    color: _Color
    when: datetime.date


class TestToJsonable:
    def test_scalars_pass_through(self) -> None:
        yes = True
        assert to_jsonable(1) == 1
        assert to_jsonable("x") == "x"
        assert to_jsonable(yes) is True
        assert to_jsonable(None) is None

    def test_enum_uses_value(self) -> None:
        assert to_jsonable(_Color.RED) == "red"
        assert to_jsonable(_Color.RANK) == 3

    def test_datetime_isoformat(self) -> None:
        assert to_jsonable(datetime.date(2026, 7, 4)) == "2026-07-04"

    def test_dataclass_recurses_into_enum_and_date(self) -> None:
        row = _Row(name="a", color=_Color.RED, when=datetime.date(2026, 1, 2))
        assert to_jsonable(row) == {"name": "a", "color": "red", "when": "2026-01-02"}

    def test_nested_list_of_dataclasses_is_json_dumpable(self) -> None:
        rows = [_Row("a", _Color.RED, datetime.date(2026, 1, 2))]
        # Round-trips through json.dumps without raising.
        assert json.loads(json.dumps(to_jsonable(rows))) == [{"name": "a", "color": "red", "when": "2026-01-02"}]

    def test_unknown_leaf_degrades_to_str(self) -> None:
        assert to_jsonable(object()) is not None  # str(object) — no raise


class TestEmit:
    def test_json_writes_pure_json_to_stdout_nothing_to_stderr(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        emit({"a": 1, "b": True}, json_output=True, out=out, err=err, human="human noise")
        assert json.loads(out.getvalue()) == {"a": 1, "b": True}
        assert err.getvalue() == ""

    def test_non_json_writes_human_to_stderr_nothing_to_stdout(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        emit({"a": 1}, json_output=False, out=out, err=err, human="the human view\n")
        assert out.getvalue() == ""
        assert err.getvalue() == "the human view\n"

    def test_non_json_callable_human_renders_to_stderr(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        emit(
            [1, 2],
            json_output=False,
            out=out,
            err=err,
            human=lambda s: s.write("rendered table"),
        )
        assert out.getvalue() == ""
        assert err.getvalue() == "rendered table"

    def test_non_json_without_human_emits_nothing(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        emit({"a": 1}, json_output=False, out=out, err=err)
        assert out.getvalue() == ""
        assert err.getvalue() == ""
