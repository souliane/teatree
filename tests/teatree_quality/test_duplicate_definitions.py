"""Changed Python files are scanned for shadowed definitions."""

from pathlib import Path

from teatree.quality.duplicate_definitions import duplicate_definitions


def test_duplicate_methods_are_reported_with_both_lines(tmp_path: Path) -> None:
    path = tmp_path / "src" / "sample.py"
    path.parent.mkdir(parents=True)
    path.write_text("class A:\n    def run(self):\n        return 1\n    def run(self):\n        return 2\n")
    findings = duplicate_definitions(tmp_path, (Path("src/sample.py"),))
    assert [(item.path, item.name, item.first_line, item.second_line) for item in findings] == [
        ("src/sample.py", "A.run", 2, 4)
    ]


def test_property_setter_is_not_a_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text(
        "class A:\n    @property\n    def value(self):\n        return 1\n"
        "    @value.setter\n    def value(self, value):\n        pass\n"
    )
    assert duplicate_definitions(tmp_path, (Path("sample.py"),)) == ()
