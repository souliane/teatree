"""Branches CI doesn't exercise without explicit help.

These tests force code paths that locally take a different branch than they
do in a fresh CI checkout (macOS symlinks under ``/private``, worktree
``.git`` pointer files, having multiple registered overlays). Without these
the project's coverage floor passes locally but slips in CI — the kind of
drift the coverage guardrail (``tests/test_coverage_floor_guard.py``) is
designed to prevent, but only if the targeted branches are reachable from
the test suite at all.
"""

import subprocess
from operator import itemgetter
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest


class TestRegisterOverlayCommandsAllowlistFilter:
    """``register_overlay_commands`` skips entries outside the allowlist.

    CI installs only ``t3-teatree``, so without an injected second entry the
    allowlist-filter branch (``continue``) is never reached.
    """

    def test_skips_entries_not_in_allowlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.cli import register_overlay_commands  # noqa: PLC0415
        from teatree.config import OverlayEntry  # noqa: PLC0415

        keep = OverlayEntry(name="t3-teatree", overlay_class="")
        drop = OverlayEntry(name="t3-other-fake", overlay_class="")

        with (
            patch("teatree.config.discover_overlays", return_value=[keep, drop]),
            patch("teatree.config.discover_active_overlay", return_value=None),
            patch("teatree.cli.OverlayAppBuilder") as mock_builder,
            patch("teatree.cli.app.add_typer") as mock_add,
        ):
            register_overlay_commands(allowlist={"t3-teatree"})
            registered_names = [call.kwargs.get("name") or call.args[1] for call in mock_add.call_args_list]
            assert "teatree" in registered_names
            assert "other-fake" not in registered_names
            assert mock_builder.call_count == 1


class TestInferOverlayFromIssueUrl:
    """``Ticket._infer_overlay`` maps URL → overlay via ``get_workspace_repos()``.

    The match-and-return branch needs an overlay whose
    ``get_workspace_repos()`` result contains a substring of ``issue_url``.
    """

    def test_returns_overlay_name_when_repo_slug_matches_url(self) -> None:
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415

        class _Overlay:
            config = None

            def get_workspace_repos(self) -> list[str]:
                return ["example/widgets"]

        ticket = Ticket(issue_url="https://github.com/example/widgets/issues/3")
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"widgets": _Overlay()}):
            assert ticket._infer_overlay() == "widgets"

    def test_returns_empty_when_no_repo_slug_matches(self) -> None:
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415

        class _Overlay:
            config = None

            def get_workspace_repos(self) -> list[str]:
                return ["acme/other"]

        ticket = Ticket(issue_url="https://github.com/example/widgets/issues/3")
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"x": _Overlay()}):
            assert ticket._infer_overlay() == ""


class TestOverlayLoaderTomlSkipNoClassPath:
    """``_discover_toml_overlays`` skips TOML entries without a Python class path.

    These project-only overlays exist (CLI bridge via OverlayAppBuilder) but
    can't be instantiated as ``OverlayBase`` — line 112 ``continue``.
    """

    def test_skips_overlay_entry_with_empty_class_path(self) -> None:
        from teatree.core import overlay_loader  # noqa: PLC0415
        from teatree.core.overlay import OverlayBase  # noqa: PLC0415

        fake_config = type(
            "Cfg",
            (),
            {"raw": {"overlays": {"project-only": {"path": "/tmp/whatever"}}}},
        )()

        with patch("teatree.config.load_config", return_value=fake_config):
            result = overlay_loader._discover_toml_overlays(OverlayBase, set())

        assert "project-only" not in result

    def test_skips_overlay_entry_with_class_missing_colon(self) -> None:
        from teatree.core import overlay_loader  # noqa: PLC0415
        from teatree.core.overlay import OverlayBase  # noqa: PLC0415

        fake_config = type(
            "Cfg",
            (),
            {"raw": {"overlays": {"bad": {"class": "no_colon_here"}}}},
        )()

        with patch("teatree.config.load_config", return_value=fake_config):
            result = overlay_loader._discover_toml_overlays(OverlayBase, set())

        assert "bad" not in result


class TestProbeHostCliEmptyResults:
    """``probe_host_cli`` short-circuits on empty / ``[]`` stdout — line 166."""

    def test_returns_empty_string_when_stdout_is_empty(self) -> None:
        from teatree.core import branch_classification  # noqa: PLC0415

        with patch.object(
            branch_classification,
            "run_allowed_to_fail",
            return_value=CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ):
            assert branch_classification.probe_host_cli(["gh", "pr"], "/tmp", itemgetter("sha")) == ""

    def test_returns_empty_string_when_stdout_is_bracket_pair(self) -> None:
        from teatree.core import branch_classification  # noqa: PLC0415

        with patch.object(
            branch_classification,
            "run_allowed_to_fail",
            return_value=CompletedProcess(args=[], returncode=0, stdout="[]", stderr=""),
        ):
            assert branch_classification.probe_host_cli(["gh", "pr"], "/tmp", itemgetter("sha")) == ""


class TestProbeHostCliTimeout:
    """``probe_host_cli`` bounds the host CLI and fails safe on expiry — #1580.

    A hung ``gh``/``glab`` must not block ``clean-all`` or the loop tick. The
    ``timeout`` is forwarded to ``run_allowed_to_fail`` (and thus
    ``subprocess.run``); when it expires the ``TimeoutExpired`` is swallowed and
    ``""`` (fail-safe "skip") is returned, so a timeout never yields a positive
    merged signal.
    """

    def test_returns_empty_string_when_subprocess_times_out(self) -> None:
        from teatree.core import branch_classification  # noqa: PLC0415

        with patch.object(
            branch_classification,
            "run_allowed_to_fail",
            side_effect=subprocess.TimeoutExpired(cmd=["gh", "pr"], timeout=30.0),
        ):
            assert branch_classification.probe_host_cli(["gh", "pr"], "/tmp", itemgetter("oid")) == ""

    def test_returns_empty_string_when_real_subprocess_run_times_out(self) -> None:
        from teatree.core import branch_classification  # noqa: PLC0415

        # Patch the actual subprocess.run reached through run_allowed_to_fail so
        # the fail-safe is exercised against the real probe + wrapper, not a
        # shallow stub of the wrapper itself.
        with patch(
            "teatree.utils.run.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["gh", "pr"], timeout=5.0),
        ):
            assert branch_classification.probe_host_cli(["gh", "pr"], "/tmp", itemgetter("oid"), timeout=5.0) == ""

    def test_forwards_timeout_to_run_allowed_to_fail(self) -> None:
        from teatree.core import branch_classification  # noqa: PLC0415

        with patch.object(
            branch_classification,
            "run_allowed_to_fail",
            return_value=CompletedProcess(args=[], returncode=0, stdout="[]", stderr=""),
        ) as run_mock:
            branch_classification.probe_host_cli(["gh", "pr"], "/tmp", itemgetter("oid"), timeout=12.5)

        assert run_mock.call_args.kwargs["timeout"] == pytest.approx(12.5)

    def test_defaults_timeout_to_thirty_seconds(self) -> None:
        from teatree.core import branch_classification  # noqa: PLC0415

        with patch.object(
            branch_classification,
            "run_allowed_to_fail",
            return_value=CompletedProcess(args=[], returncode=0, stdout="[]", stderr=""),
        ) as run_mock:
            branch_classification.probe_host_cli(["gh", "pr"], "/tmp", itemgetter("oid"))

        assert run_mock.call_args.kwargs["timeout"] == pytest.approx(30.0)


class TestProbeHostCliFailSafePaths:
    """The non-timeout failure/success paths still resolve correctly — #1580 regression guard."""

    def test_returns_empty_string_when_binary_is_missing(self) -> None:
        from teatree.core import branch_classification  # noqa: PLC0415

        with patch.object(branch_classification, "run_allowed_to_fail", side_effect=OSError("no gh")):
            assert branch_classification.probe_host_cli(["gh", "pr"], "/tmp", itemgetter("oid")) == ""

    def test_returns_empty_string_on_malformed_json(self) -> None:
        from teatree.core import branch_classification  # noqa: PLC0415

        with patch.object(
            branch_classification,
            "run_allowed_to_fail",
            return_value=CompletedProcess(args=[], returncode=0, stdout="not json", stderr=""),
        ):
            assert branch_classification.probe_host_cli(["gh", "pr"], "/tmp", itemgetter("oid")) == ""

    def test_extracts_value_on_happy_path(self) -> None:
        from teatree.core import branch_classification  # noqa: PLC0415

        with patch.object(
            branch_classification,
            "run_allowed_to_fail",
            return_value=CompletedProcess(args=[], returncode=0, stdout='[{"oid": "abc123"}]', stderr=""),
        ):
            assert branch_classification.probe_host_cli(["gh", "pr"], "/tmp", lambda data: data[0]["oid"]) == "abc123"


class TestResolveCandidatePathsMacosSymlinks:
    """``_candidate_paths`` builds path variants for macOS symlink mismatches.

    On Linux CI ``/var`` is a directory; on macOS it symlinks to ``/private/var``.
    Lines 73, 76, 80 only execute when those symlink relationships hold —
    tested here with ``Path.resolve`` / ``Path.exists`` mocks so the branches
    are reached on every platform.
    """

    def test_appends_resolved_path_when_different_from_input(self) -> None:
        from teatree.core import resolve  # noqa: PLC0415

        with patch("teatree.core.resolve.Path") as mock_path:
            mock_path.return_value.resolve.return_value = Path("/private/var/folders/x")
            mock_path.return_value.exists.return_value = False
            out = resolve._candidate_paths("/var/folders/x")

        assert "/var/folders/x" in out
        assert "/private/var/folders/x" in out

    def test_strips_private_prefix_when_path_starts_with_private(self) -> None:
        from teatree.core import resolve  # noqa: PLC0415

        with patch("teatree.core.resolve.Path") as mock_path:
            mock_path.return_value.resolve.return_value = Path("/private/var/folders/y")
            mock_path.return_value.exists.return_value = False
            out = resolve._candidate_paths("/private/var/folders/y")

        assert "/private/var/folders/y" in out
        assert "/var/folders/y" in out

    def test_appends_private_prefixed_path_when_it_exists_on_disk(self) -> None:
        from teatree.core import resolve  # noqa: PLC0415

        with patch("teatree.core.resolve.Path") as mock_path:
            instance = mock_path.return_value
            instance.resolve.return_value = Path("/var/folders/z")
            instance.exists.return_value = True
            out = resolve._candidate_paths("/var/folders/z")

        assert "/private/var/folders/z" in out
