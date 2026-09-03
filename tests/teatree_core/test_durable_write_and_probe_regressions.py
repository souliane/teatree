"""Interrupted writes, misread paths, and a failed push that is not a lost claim.

Four independent regressions with one shape — a write or a read that was only
correct when nothing went wrong:

*   ``record_fingerprint`` truncated the recovery checkpoint in place, so an
    interruption left an empty file that loads as "first run" and permanently
    skips the connector reprobe;
*   the handover ``latest`` pointer was unlinked before it was re-created, so a
    concurrent reader could observe no pointer at all;
*   ``_clear_redundant_hooks_path`` resolved a RELATIVE ``core.hooksPath``
    against the calling process's cwd instead of the repo, so provisioning from
    elsewhere compared two unrelated dirs and left ``prek install`` refusing;
*   ``fleet.claim.heartbeat`` read every failed CAS push as a steal, so a
    pre-receive rejection marked a still-owned marker ABANDONED.
"""

import json
import os
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from teatree.core import handover as handover_mod
from teatree.core import prek_hook
from teatree.core.account_fingerprint import load_recorded_fingerprint, record_fingerprint
from teatree.core.fleet import claim as claim_mod
from teatree.core.fleet.claim import Claim, ClaimLost, FleetClaimUnavailableError, heartbeat
from teatree.core.provision.provision_report import StepResult


class TestFingerprintCheckpointSurvivesAnInterruptedWrite:
    def test_a_crash_during_the_write_leaves_the_previous_value_readable(self, tmp_path: Path) -> None:
        record_fingerprint("account-a", home=tmp_path)

        original = Path.write_text

        def _die_mid_write(self: Path, *args: object, **kwargs: object) -> int:
            original(self, *args, **kwargs)
            msg = "interrupted"
            raise RuntimeError(msg)

        with patch.object(Path, "write_text", _die_mid_write), pytest.raises(RuntimeError):
            record_fingerprint("account-b", home=tmp_path)

        assert load_recorded_fingerprint(home=tmp_path) == "account-a"

    def test_a_completed_write_replaces_the_value(self, tmp_path: Path) -> None:
        record_fingerprint("account-a", home=tmp_path)
        record_fingerprint("account-b", home=tmp_path)
        assert load_recorded_fingerprint(home=tmp_path) == "account-b"

    def test_no_staging_file_is_left_behind(self, tmp_path: Path) -> None:
        path = record_fingerprint("account-a", home=tmp_path)
        assert list(path.parent.glob("*.tmp")) == []


class TestHandoverPointerIsPublishedAtomically:
    def test_the_pointer_never_disappears_while_it_is_repointed(self, tmp_path: Path) -> None:
        older = tmp_path / "handover-a-20260101T000000_000000.md"
        newer = tmp_path / "handover-b-20260102T000000_000000.md"
        older.write_text("a", encoding="utf-8")
        newer.write_text("b", encoding="utf-8")
        pointer = tmp_path / "latest.md"
        pointer.symlink_to(older.name)

        observed: list[bool] = []
        real_replace = os.replace

        def _watch(src: object, dst: object) -> None:
            observed.append(Path(str(dst)).is_symlink())
            real_replace(src, dst)

        with patch.object(handover_mod.os, "replace", _watch):
            handover_mod._update_latest_pointer(pointer, newer)

        assert observed == [True], "the pointer existed for the whole publish"
        assert pointer.readlink().name == newer.name

    def test_an_already_current_pointer_is_left_alone(self, tmp_path: Path) -> None:
        mirror = tmp_path / "handover-a-20260101T000000_000000.md"
        mirror.write_text("a", encoding="utf-8")
        pointer = tmp_path / "latest.md"
        pointer.symlink_to(mirror.name)
        handover_mod._update_latest_pointer(pointer, mirror)
        assert pointer.readlink().name == mirror.name


class TestRelativeHooksPathResolvesAgainstTheRepo:
    def _stub_git(self, wt_path: Path, configured: str) -> AbstractContextManager[MagicMock]:
        def _run(name: str, argv: list[str], *, cwd: str) -> StepResult:
            if name == "git-hookspath-get":
                return StepResult(name=name, success=True, stdout=configured)
            if name == "git-common-dir":
                return StepResult(name=name, success=True, stdout=".git")
            return StepResult(name=name, success=True)

        return patch.object(prek_hook, "run_step", side_effect=_run)

    def test_a_relative_redundant_value_is_unset_from_any_cwd(self, tmp_path: Path) -> None:
        (tmp_path / ".git" / "hooks").mkdir(parents=True)
        with self._stub_git(tmp_path, ".git/hooks"):
            assert prek_hook._clear_redundant_hooks_path(str(tmp_path)) is True

    def test_a_genuine_custom_override_is_preserved(self, tmp_path: Path) -> None:
        (tmp_path / ".git" / "hooks").mkdir(parents=True)
        custom = tmp_path / "my-hooks"
        custom.mkdir()
        with self._stub_git(tmp_path, str(custom)):
            assert prek_hook._clear_redundant_hooks_path(str(tmp_path)) is False


class TestHeartbeatDistinguishesAStealFromAFailedPush:
    def _claim(self) -> Claim:
        return Claim(
            work_key="issue-1",
            ref="refs/teatree/claims/issue-1-abc",
            sha="a" * 40,
            instance_id="inst",
            claimed_at=0.0,
            ttl_seconds=10.0,
        )

    def test_a_rejected_push_with_our_sha_still_on_the_ref_is_not_a_loss(self) -> None:
        held = self._claim()
        with (
            patch.object(claim_mod, "_ephemeral_odb"),
            patch.object(claim_mod, "_write_claim_commit", return_value="b" * 40),
            patch.object(claim_mod, "_cas", return_value=False),
            patch.object(claim_mod, "_ls_remote_sha", return_value=held.sha),
            pytest.raises(FleetClaimUnavailableError),
        ):
            heartbeat(held)

    def test_a_genuine_steal_is_still_reported_lost(self) -> None:
        held = self._claim()
        with (
            patch.object(claim_mod, "_ephemeral_odb"),
            patch.object(claim_mod, "_write_claim_commit", return_value="b" * 40),
            patch.object(claim_mod, "_cas", return_value=False),
            patch.object(claim_mod, "_ls_remote_sha", return_value="c" * 40),
        ):
            outcome = heartbeat(held)
        assert isinstance(outcome, ClaimLost)
        assert outcome.observed_sha == "c" * 40

    def test_a_deleted_ref_is_still_reported_lost(self) -> None:
        held = self._claim()
        with (
            patch.object(claim_mod, "_ephemeral_odb"),
            patch.object(claim_mod, "_write_claim_commit", return_value="b" * 40),
            patch.object(claim_mod, "_cas", return_value=False),
            patch.object(claim_mod, "_ls_remote_sha", return_value=""),
        ):
            outcome = heartbeat(held)
        assert isinstance(outcome, ClaimLost)
        assert outcome.observed_sha == ""

    def test_a_landed_heartbeat_still_refreshes(self) -> None:
        held = self._claim()
        with (
            patch.object(claim_mod, "_ephemeral_odb"),
            patch.object(claim_mod, "_write_claim_commit", return_value="b" * 40),
            patch.object(claim_mod, "_cas", return_value=True),
        ):
            outcome = heartbeat(held)
        assert isinstance(outcome, Claim)
        assert outcome.sha == "b" * 40


class TestFingerprintFileShape:
    def test_the_recorded_payload_is_still_the_documented_json(self, tmp_path: Path) -> None:
        path = record_fingerprint("account-a", home=tmp_path)
        assert json.loads(path.read_text(encoding="utf-8")) == {"accountUuid": "account-a"}
