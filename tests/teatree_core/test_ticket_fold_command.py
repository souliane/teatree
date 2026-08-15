"""``t3 <overlay> ticket fold`` / ``fold-check`` — the sweep's never-close-for-real seam (#4344).

``fold`` moves a member ticket's body into its host verbatim; ``fold-check`` proves a host
body (re-read off the forge) still carries it, and exits non-zero when it does not — so a
standalone row is never retired on a fold that summarised instead of moving.
"""

from pathlib import Path
from typing import cast

import pytest
from django.core.management import call_command

_MEMBER_BODY = """## The problem

`apply_lane_ceiling` drops force-keep items re-added after the cut.

## Acceptance

- A re-added force-keep item survives the ceiling.
"""

_HOST_BODY = "## The lane ceiling is applied twice\n\nThe ceiling runs before and after the force-keep pass.\n"


def _marker(ref: str) -> str:
    """The literal ``fold_preservation.fold_marker`` format, pinned in its own test file."""
    return f"## Folded in: {ref}"


@pytest.fixture
def bodies(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A host body, a member body, and the path the merged body is written to."""
    host, member = tmp_path / "host.md", tmp_path / "member.md"
    host.write_text(_HOST_BODY, encoding="utf-8")
    member.write_text(_MEMBER_BODY, encoding="utf-8")
    return host, member, tmp_path / "merged.md"


def _fold(host: Path, member: Path, out: Path, ref: str = "#4247") -> dict[str, object]:
    return cast(
        "dict[str, object]",
        call_command(
            "ticket",
            "fold",
            "--host-body",
            str(host),
            "--member-body",
            str(member),
            "--member-ref",
            ref,
            "--member-title",
            "force-keep items are dropped",
            "--out",
            str(out),
        ),
    )


class TestTicketFold:
    def test_the_merged_body_carries_both_tickets(self, bodies: tuple[Path, Path, Path]) -> None:
        host, member, out = bodies
        assert _fold(host, member, out)["folded"] is True

        merged = out.read_text(encoding="utf-8")
        assert _marker("#4247") in merged
        assert "The ceiling runs before and after the force-keep pass." in merged
        for line in (line.strip() for line in _MEMBER_BODY.splitlines() if line.strip()):
            assert line in merged

    def test_a_missing_output_path_is_refused(self, bodies: tuple[Path, Path, Path]) -> None:
        host, member, _ = bodies
        with pytest.raises(SystemExit) as exc:
            call_command(
                "ticket",
                "fold",
                "--host-body",
                str(host),
                "--member-body",
                str(member),
                "--member-ref",
                "#4247",
            )
        assert exc.value.code == 1

    def test_an_unreadable_body_is_refused(self, bodies: tuple[Path, Path, Path]) -> None:
        _, member, out = bodies
        with pytest.raises(SystemExit) as exc:
            _fold(out.parent / "absent.md", member, out)
        assert exc.value.code == 1


class TestTicketFoldCheck:
    def test_a_body_the_fold_produced_is_preserved(self, bodies: tuple[Path, Path, Path]) -> None:
        host, member, out = bodies
        _fold(host, member, out)
        result = cast(
            "dict[str, object]",
            call_command("ticket", "fold-check", "--host-body", str(out), "--member-body", str(member)),
        )
        assert result["preserved"] is True

    def test_a_host_that_summarised_the_member_exits_nonzero(self, bodies: tuple[Path, Path, Path]) -> None:
        host, member, _ = bodies
        lossy = host.parent / "lossy.md"
        lossy.write_text(f"{_HOST_BODY}\n{_marker('#4247')}\n\nSee #4247.\n", encoding="utf-8")

        with pytest.raises(SystemExit) as exc:
            call_command("ticket", "fold-check", "--host-body", str(lossy), "--member-body", str(member))
        assert exc.value.code == 1
        # The gate refuses; it never edits the bodies it was handed.
        assert host.read_text(encoding="utf-8") == _HOST_BODY

    def test_a_blank_argument_is_refused(self, bodies: tuple[Path, Path, Path]) -> None:
        _, member, _ = bodies
        with pytest.raises(SystemExit) as exc:
            call_command("ticket", "fold-check", "--host-body", "", "--member-body", str(member))
        assert exc.value.code == 1
