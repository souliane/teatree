"""Tests for ``t3 <overlay> e2e post-evidence`` (souliane/teatree#1409).

Mirrors ``tests/teatree_core/pr_command/test_post_evidence_and_sweep.py``:
``TestCase`` + ``call_command`` + a ``MagicMock`` host, with the
``disable_on_behalf_gate`` fixture so the transport-mechanics tests don't
trip the on-behalf gate.

The hard-fail half asserts the validators refuse bad evidence with no host
side effect; the idempotency half asserts the (env, commit) hidden-marker
create-or-update flow; the pure-validator half exercises the regex / hash /
enum helpers directly.
"""

from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import _e2e_evidence as _evidence
from teatree.core.management.commands import e2e as e2e_command
from teatree.core.management.commands._e2e_evidence import EvidenceEnv
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}
_ISSUE_URL = "https://gitlab.com/org/repo/-/issues/1409"


def _write_png(path, payload: bytes) -> str:
    """Write a tiny distinct PNG-ish blob and return its path string."""
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + payload)
    return str(path)


class _EvidenceTestBase(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        self._monkeypatch = monkeypatch
        self._tmp = tmp_path

    @pytest.fixture(autouse=True)
    def _no_on_behalf_gate(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from tests.teatree_core._on_behalf_gate_helpers import disable_on_behalf_gate  # noqa: PLC0415

        disable_on_behalf_gate(tmp_path_factory, monkeypatch)

    def _clean_repo(self) -> None:
        """Make the commit validators deterministic: known SHA + clean tree."""
        self._monkeypatch.setattr(_evidence.git, "head_sha", lambda repo=".": "a" * 40)
        self._monkeypatch.setattr(_evidence.git, "status_porcelain", lambda repo=".": "")
        self._monkeypatch.setattr(
            _evidence.git,
            "check",
            lambda *, repo=".", args: True,
        )

    def _patch_host(self, host: MagicMock) -> None:
        self._monkeypatch.setattr(e2e_command, "code_host_from_overlay", lambda: host)
        # No worktree resolvable from the test cwd → ticket comes from --ticket.
        self._monkeypatch.setattr(
            _evidence,
            "resolve_worktree",
            MagicMock(side_effect=_evidence.WorktreeNotFoundError("none")),
        )

    def _before_after(self) -> tuple[str, str]:
        before = _write_png(self._tmp / "before.png", b"BEFORE")
        after = _write_png(self._tmp / "after.png", b"AFTER")
        return before, after


class TestHardFail(_EvidenceTestBase):
    """Each invalid input exits non-zero and posts/uploads nothing."""

    def _run_expecting_exit(self, host: MagicMock, **kwargs: str) -> None:
        # A real ticket exists, so the ONLY failure path is the validator
        # under test — never an unresolvable-ticket false positive.
        from teatree.core.models import Ticket  # noqa: PLC0415

        Ticket.objects.create(overlay="test", issue_url=_ISSUE_URL)
        self._patch_host(host)
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            pytest.raises(SystemExit),
        ):
            call_command("e2e", "post-evidence", **kwargs)
        host.upload_file.assert_not_called()
        host.post_issue_comment.assert_not_called()
        host.update_issue_comment.assert_not_called()

    def test_bad_env(self) -> None:
        self._clean_repo()
        before, after = self._before_after()
        host = MagicMock()
        self._run_expecting_exit(
            host,
            ticket=_ISSUE_URL,
            env="staging",
            before=before,
            after=after,
            assertion="It works",
        )

    def test_missing_before(self) -> None:
        self._clean_repo()
        _, after = self._before_after()
        host = MagicMock()
        self._run_expecting_exit(
            host,
            ticket=_ISSUE_URL,
            env="dev",
            before="",
            after=after,
            assertion="It works",
        )

    def test_missing_after(self) -> None:
        self._clean_repo()
        before, _ = self._before_after()
        host = MagicMock()
        self._run_expecting_exit(
            host,
            ticket=_ISSUE_URL,
            env="dev",
            before=before,
            after="",
            assertion="It works",
        )

    def test_identical_before_after_same_path(self) -> None:
        self._clean_repo()
        before, _ = self._before_after()
        host = MagicMock()
        self._run_expecting_exit(
            host,
            ticket=_ISSUE_URL,
            env="dev",
            before=before,
            after=before,
            assertion="It works",
        )

    def test_byte_identical_distinct_paths(self) -> None:
        self._clean_repo()
        before = _write_png(self._tmp / "before.png", b"SAME")
        after = _write_png(self._tmp / "after.png", b"SAME")
        host = MagicMock()
        self._run_expecting_exit(
            host,
            ticket=_ISSUE_URL,
            env="local",
            before=before,
            after=after,
            assertion="It works",
        )

    def test_dirty_tree(self) -> None:
        self._monkeypatch.setattr(_evidence.git, "head_sha", lambda repo=".": "a" * 40)
        self._monkeypatch.setattr(_evidence.git, "status_porcelain", lambda repo=".": " M src/x.py")
        before, after = self._before_after()
        host = MagicMock()
        self._run_expecting_exit(
            host,
            ticket=_ISSUE_URL,
            env="dev",
            before=before,
            after=after,
            assertion="It works",
        )

    def test_unknown_commit(self) -> None:
        self._monkeypatch.setattr(_evidence.git, "status_porcelain", lambda repo=".": "")

        def _reject(*, repo: str = ".", args: list[str]) -> str:
            raise _evidence.CommandFailedError(["git", *args], 128, "", "bad object")

        self._monkeypatch.setattr(_evidence.git, "run_strict", _reject)
        before, after = self._before_after()
        host = MagicMock()
        self._run_expecting_exit(
            host,
            ticket=_ISSUE_URL,
            env="dev",
            commit="deadbeef",
            before=before,
            after=after,
            assertion="It works",
        )


class TestIdempotency(_EvidenceTestBase):
    """The (env, commit) hidden-marker create-or-update flow (gate disabled)."""

    def _ticket(self) -> None:
        from teatree.core.models import Ticket  # noqa: PLC0415

        Ticket.objects.create(overlay="test", issue_url=_ISSUE_URL)

    def _run(self, host: MagicMock, **kwargs: str) -> dict[str, object]:
        self._clean_repo()
        self._patch_host(host)
        host.upload_file.return_value = {"markdown": "![x](u)"}
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            return cast("dict[str, object]", call_command("e2e", "post-evidence", **kwargs))

    def _existing_comment(self, *, env: str, commit: str, comment_id: int) -> dict[str, object]:
        marker = f"<!-- t3-e2e-evidence env={env} commit={commit} -->"
        return {"id": comment_id, "body": f"{marker}\n## old"}

    def test_creates_new_when_list_empty(self) -> None:
        self._ticket()
        before, after = self._before_after()
        host = MagicMock()
        host.list_issue_comments.return_value = []
        host.post_issue_comment.return_value = {"id": 77, "web_url": "u"}

        result = self._run(
            host,
            ticket=_ISSUE_URL,
            env="dev",
            before=before,
            after=after,
            assertion="The thing works",
        )

        assert result["action"] == "created"
        assert result["comment_id"] == 77
        assert result["env"] == "dev"
        host.post_issue_comment.assert_called_once()
        host.update_issue_comment.assert_not_called()
        body = host.post_issue_comment.call_args.kwargs["body"]
        assert "t3-e2e-evidence env=dev commit=" + "a" * 40 in body
        assert "The thing works" in body

    def test_updates_when_marker_env_and_commit_match(self) -> None:
        self._ticket()
        before, after = self._before_after()
        host = MagicMock()
        host.list_issue_comments.return_value = [
            self._existing_comment(env="dev", commit="a" * 40, comment_id=33),
        ]
        host.update_issue_comment.return_value = {"id": 33, "web_url": "u"}

        result = self._run(
            host,
            ticket=_ISSUE_URL,
            env="dev",
            before=before,
            after=after,
            assertion="works",
        )

        assert result["action"] == "updated"
        assert result["comment_id"] == 33
        host.update_issue_comment.assert_called_once()
        host.post_issue_comment.assert_not_called()

    def test_short_commit_dedups_against_full_sha_marker(self) -> None:
        # #1652: a supplied short `--commit` is expanded to the canonical
        # full SHA, so it matches a prior comment whose marker carries the
        # full 40-char SHA (the no-`--commit` auto-detect form) instead of
        # posting a duplicate.
        self._ticket()
        before, after = self._before_after()
        full = "a" * 40
        host = MagicMock()
        host.list_issue_comments.return_value = [
            self._existing_comment(env="dev", commit=full, comment_id=55),
        ]
        host.update_issue_comment.return_value = {"id": 55, "web_url": "u"}

        self._patch_host(host)
        self._monkeypatch.setattr(_evidence.git, "status_porcelain", lambda repo=".": "")
        self._monkeypatch.setattr(_evidence.git, "run_strict", lambda *, repo=".", args: full)
        host.upload_file.return_value = {"markdown": "![x](u)"}
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast(
                "dict[str, object]",
                call_command(
                    "e2e",
                    "post-evidence",
                    ticket=_ISSUE_URL,
                    env="dev",
                    commit="aaaaaaa",
                    before=before,
                    after=after,
                    assertion="works",
                ),
            )

        assert result["action"] == "updated"
        assert result["commit"] == full
        host.update_issue_comment.assert_called_once()
        host.post_issue_comment.assert_not_called()

    def test_new_comment_when_commit_differs(self) -> None:
        self._ticket()
        before, after = self._before_after()
        host = MagicMock()
        host.list_issue_comments.return_value = [
            self._existing_comment(env="dev", commit="b" * 40, comment_id=33),
        ]
        host.post_issue_comment.return_value = {"id": 88}

        result = self._run(
            host,
            ticket=_ISSUE_URL,
            env="dev",
            before=before,
            after=after,
            assertion="works",
        )

        assert result["action"] == "created"
        host.post_issue_comment.assert_called_once()
        host.update_issue_comment.assert_not_called()

    def test_new_comment_when_env_differs(self) -> None:
        self._ticket()
        before, after = self._before_after()
        host = MagicMock()
        host.list_issue_comments.return_value = [
            self._existing_comment(env="local", commit="a" * 40, comment_id=33),
        ]
        host.post_issue_comment.return_value = {"id": 99}

        result = self._run(
            host,
            ticket=_ISSUE_URL,
            env="dev",
            before=before,
            after=after,
            assertion="works",
        )

        assert result["action"] == "created"
        host.post_issue_comment.assert_called_once()
        host.update_issue_comment.assert_not_called()


class TestPureValidators:
    """The pure helpers in ``_e2e_evidence`` are independently testable."""

    def test_marker_regex_round_trip(self) -> None:
        marker = _evidence.evidence_marker(env=EvidenceEnv.DEV, commit="c" * 40)
        match = _evidence._E2E_MARKER_RE.search(f"prefix {marker} suffix")
        assert match is not None
        assert match.group("env") == "dev"
        assert match.group("commit") == "c" * 40

    def test_before_after_hash_compare(self, tmp_path) -> None:
        a = tmp_path / "a.png"
        b = tmp_path / "b.png"
        a.write_bytes(b"AAA")
        b.write_bytes(b"BBB")
        # Distinct bytes pass.
        _evidence.validate_before_differs_from_after(before=a, after=b)
        # Same bytes (distinct path) fail.
        b.write_bytes(b"AAA")
        with pytest.raises(_evidence.EvidenceValidationError):
            _evidence.validate_before_differs_from_after(before=a, after=b)

    def test_env_enum_coercion(self) -> None:
        assert _evidence.coerce_env("DEV") is EvidenceEnv.DEV
        assert _evidence.coerce_env(" local ") is EvidenceEnv.LOCAL
        with pytest.raises(_evidence.EvidenceValidationError):
            _evidence.coerce_env("")
        with pytest.raises(_evidence.EvidenceValidationError):
            _evidence.coerce_env("prod")

    def test_find_matching_comment_ignores_other_markers(self) -> None:
        comments = [
            {"id": 1, "body": "<!-- t3-e2e-evidence env=dev commit=zzz -->\nx"},
            {"id": 2, "body": "no marker here"},
            {"id": 3, "body": "<!-- t3-e2e-evidence env=local commit=yyy -->\nx"},
        ]
        assert _evidence.find_matching_comment(comments, env=EvidenceEnv.DEV, commit="zzz") == 1
        assert _evidence.find_matching_comment(comments, env=EvidenceEnv.DEV, commit="nope") is None

    def test_resolve_commit_expands_short_to_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # #1652: the supplied short SHA is captured from rev-parse output as
        # the canonical full SHA, not echoed verbatim.
        full = "b" * 40
        monkeypatch.setattr(_evidence.git, "run_strict", lambda *, repo=".", args: full)
        monkeypatch.setattr(_evidence.git, "status_porcelain", lambda repo=".": "")
        resolved = _evidence.resolve_and_validate_commit(commit="bbbbbbb", repo=".")
        assert resolved == full

    def test_build_body_includes_video_row_only_when_given(self) -> None:
        without = _evidence.build_evidence_body(
            _evidence.EvidenceComment(
                env=EvidenceEnv.LOCAL,
                commit="d" * 40,
                before_md="![b](b)",
                after_md="![a](a)",
                assertion="claim",
            ),
        )
        assert "Video" not in without
        assert "environment: **LOCAL**" in without
        with_video = _evidence.build_evidence_body(
            _evidence.EvidenceComment(
                env=EvidenceEnv.LOCAL,
                commit="d" * 40,
                before_md="![b](b)",
                after_md="![a](a)",
                assertion="claim",
                video_md="[vid](v)",
            ),
        )
        assert "| Video | [vid](v) |" in with_video
