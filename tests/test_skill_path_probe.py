# test-path: cross-cutting — a hooks/scripts leaf module; no src/teatree/ mirror.
"""A pathological skill name degrades to "absent", never to an ``OSError``.

The names probed here come from ``<session>.pending``, which no gate authored,
and the skill-loading gate treats an unresolvable name as fail-open. A raising
probe would instead propagate out of a hook whose contract is to be crash-proof.
"""

from pathlib import Path

import pytest

from hooks.scripts.skill_path_probe import is_file_safe


class TestIsFileSafe:
    def test_an_existing_file_is_reported(self, tmp_path: Path) -> None:
        target = tmp_path / "SKILL.md"
        target.write_text("---\nname: code\n---\n", encoding="utf-8")
        assert is_file_safe(target) is True

    def test_an_absent_path_is_reported_absent(self, tmp_path: Path) -> None:
        assert is_file_safe(tmp_path / "nope" / "SKILL.md") is False

    def test_a_directory_is_not_a_file(self, tmp_path: Path) -> None:
        assert is_file_safe(tmp_path) is False

    def test_an_overlong_segment_degrades_instead_of_raising(self, tmp_path: Path) -> None:
        # 255+ bytes in one segment makes the underlying stat raise ENAMETOOLONG.
        overlong = tmp_path / ("x" * 300) / "SKILL.md"
        with pytest.raises(OSError):  # noqa: PT011 — the control: the RAW probe must really raise here
            overlong.is_file()
        assert is_file_safe(overlong) is False
