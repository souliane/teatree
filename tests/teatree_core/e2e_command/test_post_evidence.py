"""Tests for ``t3 <overlay> e2e post-evidence`` (souliane/teatree#1409).

Mirrors ``tests/teatree_core/pr_command/test_post_evidence_and_sweep.py``:
``TestCase`` + ``call_command`` + a ``MagicMock`` host, with the
``disable_on_behalf_gate`` fixture so the transport-mechanics tests don't
trip the on-behalf gate.

The hard-fail half asserts the validators refuse bad evidence with no host
side effect; the idempotency half asserts the one-comment-per-(ticket, env)
hidden-marker create-or-update flow (a new commit on the same env edits in
place with an old -> new delta); the gate half asserts the on-behalf gate
stays in front of both branches; the pure-validator half exercises the regex
/ hash / enum helpers directly.
"""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.backend_protocols import UploadVerification
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
    """The (ticket, env) hidden-marker create-or-update flow (gate disabled)."""

    def _ticket(self) -> None:
        from teatree.core.models import Ticket  # noqa: PLC0415

        Ticket.objects.create(overlay="test", issue_url=_ISSUE_URL)

    def _run(self, host: MagicMock, **kwargs: str) -> dict[str, object]:
        self._clean_repo()
        self._patch_host(host)
        host.upload_file.return_value = {"full_path": "/-/project/9/uploads/deadbeef/x.png"}
        # The self-verification gate passes: each upload resolves + renders, and
        # the embed is the verified ABSOLUTE url (never the relative /uploads path).
        host.verify_upload.return_value = UploadVerification(
            ok=True, embed_url="https://gitlab.com/-/project/9/uploads/deadbeef/x.png"
        )
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            return cast("dict[str, object]", call_command("e2e", "post-evidence", **kwargs))

    def _existing_comment(self, *, env: str, commit: str, comment_id: int) -> dict[str, object]:
        marker = f"<!-- t3-e2e-evidence env={env} -->"
        return {"id": comment_id, "body": f"{marker}\n## old\nCommit tested: `{commit}`\n"}

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
        assert "t3-e2e-evidence env=dev -->" in body
        assert "Commit tested: `" + "a" * 40 + "`" in body
        assert "The thing works" in body
        # The body embeds the verified ABSOLUTE upload URL, never the relative
        # /uploads path that fails to render in the work-items UI (#2156).
        assert "https://gitlab.com/-/project/9/uploads/deadbeef/x.png" in body
        assert "](/uploads/" not in body
        host.verify_upload.assert_called()

    def test_updates_when_marker_env_matches(self) -> None:
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

    def test_updates_in_place_when_commit_differs(self) -> None:
        self._ticket()
        before, after = self._before_after()
        host = MagicMock()
        host.list_issue_comments.return_value = [
            self._existing_comment(env="dev", commit="b" * 40, comment_id=33),
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

    def test_update_body_renders_commit_delta(self) -> None:
        self._ticket()
        before, after = self._before_after()
        host = MagicMock()
        host.list_issue_comments.return_value = [
            self._existing_comment(env="dev", commit="b" * 40, comment_id=33),
        ]
        host.update_issue_comment.return_value = {"id": 33, "web_url": "u"}

        self._run(
            host,
            ticket=_ISSUE_URL,
            env="dev",
            before=before,
            after=after,
            assertion="works",
        )

        body = host.update_issue_comment.call_args.kwargs["body"]
        assert "Re-verified: `" + "b" * 8 + "` -> `" + "a" * 8 + "`" in body
        match = _evidence._E2E_MARKER_RE.search(body)
        assert match is not None
        assert match.group("env") == "dev"

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


class TestMediaRenderGate(_EvidenceTestBase):
    """The post-upload self-verification gate refuses to post broken media (#2156).

    "Posted" must mean "the media is verified renderable", not "the upload
    POST returned 201". If any embedded artifact does not resolve + render,
    the command aborts non-zero, posts no comment, and burns no approval.
    """

    def _ticket(self) -> None:
        from teatree.core.models import Ticket  # noqa: PLC0415

        Ticket.objects.create(overlay="test", issue_url=_ISSUE_URL)

    def _run_expecting_exit(self, host: MagicMock, **kwargs: str) -> None:
        self._clean_repo()
        self._patch_host(host)
        host.upload_file.return_value = {"full_path": "/-/project/9/uploads/deadbeef/x.png"}
        self._ticket()
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            pytest.raises(SystemExit),
        ):
            call_command("e2e", "post-evidence", **kwargs)
        # The defining assertion (anti-vacuity): a non-rendering artifact means
        # NO comment is ever posted. Remove the gate in post_evidence_comment
        # and these go RED — the broken-media comment would be created.
        host.post_issue_comment.assert_not_called()
        host.update_issue_comment.assert_not_called()

    def test_refuses_to_post_when_upload_does_not_render(self) -> None:
        host = MagicMock()
        host.list_issue_comments.return_value = []
        # The upload returned 201 but the artifact does not resolve / render.
        host.verify_upload.return_value = UploadVerification(
            ok=False,
            embed_url="https://gitlab.com/-/project/9/uploads/deadbeef/x.png",
            detail="upload fetch returned HTTP 404",
        )
        before, after = self._before_after()
        self._run_expecting_exit(
            host,
            ticket=_ISSUE_URL,
            env="dev",
            before=before,
            after=after,
            assertion="works",
        )

    def test_refuses_to_post_when_only_the_video_does_not_render(self) -> None:
        host = MagicMock()
        host.list_issue_comments.return_value = []
        before, after = self._before_after()
        video = self._tmp / "clip.webm"
        video.write_bytes(b"\x1a\x45\xdf\xa3fakewebm")

        # The stills verify fine; only the video fails the render check.
        ok_then_video_fails = [
            UploadVerification(ok=True, embed_url="https://gitlab.com/-/project/9/uploads/s/before.png"),
            UploadVerification(ok=True, embed_url="https://gitlab.com/-/project/9/uploads/s/after.png"),
            UploadVerification(ok=False, embed_url="", detail="fetched bytes are not a renderable video"),
        ]
        host.verify_upload.side_effect = ok_then_video_fails
        self._run_expecting_exit(
            host,
            ticket=_ISSUE_URL,
            env="dev",
            before=before,
            after=after,
            video=str(video),
            assertion="works",
        )

    def test_non_video_passed_as_video_is_rejected(self) -> None:
        host = MagicMock()
        host.list_issue_comments.return_value = []
        host.verify_upload.return_value = UploadVerification(ok=True, embed_url="https://x/img.png")
        before, after = self._before_after()
        # A .txt handed to --video must never be embedded as a <video>.
        not_a_video = self._tmp / "notes.txt"
        not_a_video.write_text("not a video", encoding="utf-8")
        self._run_expecting_exit(
            host,
            ticket=_ISSUE_URL,
            env="dev",
            before=before,
            after=after,
            video=str(not_a_video),
            assertion="works",
        )


class TestOnBehalfGateConsulted(TestCase):
    """The on-behalf gate stays in front of the upsert — no bypass.

    The (ticket, env) idempotency change must NOT weaken the gate: with the
    default blocking mode and no recorded approval, neither the create nor
    the update branch may post; the command exits non-zero with no host write.
    """

    @pytest.fixture(autouse=True)
    def _inject(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        self._monkeypatch = monkeypatch
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text('[teatree]\non_behalf_post_mode = "ask"\n', encoding="utf-8")
        monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)

    def _post(self, *, comments: list[dict[str, object]]) -> MagicMock:
        host = MagicMock()
        host.list_issue_comments.return_value = comments
        post = _evidence.EvidencePost(
            issue_url=_ISSUE_URL,
            repo="org/repo",
            env=EvidenceEnv.DEV,
            commit="a" * 40,
            before_path=Path("/dev/null"),
            after_path=Path("/dev/null"),
            assertion="works",
        )
        with pytest.raises(_evidence.OnBehalfPostBlockedError):
            _evidence.post_evidence_comment(host, post)
        host.upload_file.assert_not_called()
        host.post_issue_comment.assert_not_called()
        host.update_issue_comment.assert_not_called()
        return host

    def test_create_branch_blocked_without_approval(self) -> None:
        self._post(comments=[])

    def test_update_branch_blocked_without_approval(self) -> None:
        self._post(comments=[{"id": 1, "body": "<!-- t3-e2e-evidence env=dev -->\nx"}])


class TestPureValidators:
    """The pure helpers in ``_e2e_evidence`` are independently testable."""

    def test_marker_regex_round_trip(self) -> None:
        marker = _evidence.evidence_marker(env=EvidenceEnv.DEV)
        match = _evidence._E2E_MARKER_RE.search(f"prefix {marker} suffix")
        assert match is not None
        assert match.group("env") == "dev"

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

    def test_find_matching_comment_keys_on_env_only(self) -> None:
        comments = [
            {"id": 1, "body": "<!-- t3-e2e-evidence env=dev -->\nCommit tested: `" + "f" * 40 + "`"},
            {"id": 2, "body": "no marker here"},
            {"id": 3, "body": "<!-- t3-e2e-evidence env=local -->\nx"},
        ]
        dev = _evidence.find_matching_comment(comments, env=EvidenceEnv.DEV)
        assert dev is not None
        assert dev.comment_id == 1
        assert dev.prior_commit == "f" * 40
        local = _evidence.find_matching_comment(comments, env=EvidenceEnv.LOCAL)
        assert local is not None
        assert local.comment_id == 3
        assert local.prior_commit == ""
        assert _evidence.find_matching_comment([], env=EvidenceEnv.DEV) is None
        # A marker comment with no usable id is skipped, not returned.
        assert (
            _evidence.find_matching_comment(
                [{"id": 0, "body": "<!-- t3-e2e-evidence env=dev -->"}], env=EvidenceEnv.DEV
            )
            is None
        )

    def test_prior_commit_from_body_parses_marker(self) -> None:
        body = "<!-- t3-e2e-evidence env=dev -->\nCommit tested: `" + "b" * 40 + "`\nclaim"
        assert _evidence._prior_commit_from_body(body) == "b" * 40
        assert _evidence._prior_commit_from_body("no commit line") == ""

    def test_build_body_renders_delta_when_prior_commit_given(self) -> None:
        body = _evidence.build_evidence_body(
            _evidence.EvidenceComment(
                env=EvidenceEnv.DEV,
                commit="a" * 40,
                before_md="![b](b)",
                after_md="![a](a)",
                assertion="claim",
                prior_commit="b" * 40,
            ),
        )
        assert ("b" * 8) in body
        assert ("a" * 8) in body
        assert "Re-verified:" in body
        # No prior commit -> no delta line.
        first = _evidence.build_evidence_body(
            _evidence.EvidenceComment(
                env=EvidenceEnv.DEV,
                commit="a" * 40,
                before_md="![b](b)",
                after_md="![a](a)",
                assertion="claim",
            ),
        )
        assert "Re-verified:" not in first
        # Same prior == current commit -> no delta line either.
        same = _evidence.build_evidence_body(
            _evidence.EvidenceComment(
                env=EvidenceEnv.DEV,
                commit="a" * 40,
                before_md="![b](b)",
                after_md="![a](a)",
                assertion="claim",
                prior_commit="a" * 40,
            ),
        )
        assert "Re-verified:" not in same

    def test_resolve_commit_expands_short_to_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # #1652: the supplied short SHA is captured from rev-parse output as
        # the canonical full SHA, not echoed verbatim.
        full = "b" * 40
        monkeypatch.setattr(_evidence.git, "run_strict", lambda *, repo=".", args: full)
        monkeypatch.setattr(_evidence.git, "status_porcelain", lambda repo=".": "")
        resolved = _evidence.resolve_and_validate_commit(commit="bbbbbbb", repo=".")
        assert resolved == full

    def test_build_body_includes_video_only_when_given(self) -> None:
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
                video_md="![video](v)",
            ),
        )
        # The video is embedded inline (full-width <video>), never in a one-cell row.
        assert "### Video" in with_video
        assert "![video](v)" in with_video
        assert "| Video |" not in with_video

    def test_build_body_places_video_above_before_after_table(self) -> None:
        """The user's standard: the clip sits ABOVE the Before/After stills."""
        body = _evidence.build_evidence_body(
            _evidence.EvidenceComment(
                env=EvidenceEnv.LOCAL,
                commit="d" * 40,
                before_md="![b](b)",
                after_md="![a](a)",
                assertion="claim",
                video_md="![video](v)",
            ),
        )
        assert body.index("### Video") < body.index("| Before | After |")
