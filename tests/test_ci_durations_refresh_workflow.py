"""The scheduled durations-refresh job must open ONE mergeable refresh PR (#3160).

Three residual reliability bugs are pinned here against the workflow YAML.

CI-3: the refresh branch name must be STABLE (reused + force-updated), so an unmerged
refresh is updated in place instead of stacking a new dated PR every day.

CI-2: the refresh PR must be created/pushed with a token that TRIGGERS the required
``test (3.13)`` check (a PAT, ``TEATREE_GH_TOKEN``) — the default ``GITHUB_TOKEN`` never
fires it, so such a PR could never merge unaided. The step also fails LOUD when that
token is unset rather than silently opening an un-mergeable PR.

CI-4 (#4477): ``dev/.test_durations`` is a HIDDEN file and ``actions/upload-artifact``
excludes hidden files by default, so all twelve shard uploads found nothing, warned, and
succeeded — leaving ``refresh-durations`` to fail two jobs later on an absent artifact.
The upload must opt in to hidden files, must fail at the SOURCE when it finds none, and
the shard must refuse to publish a file its own run did not rewrite.
"""

from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

_SHARD_GROUPS = range(1, 13)


def _jobs() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"])


def _refresh_job() -> dict[str, Any]:
    return cast("dict[str, Any]", _jobs()["refresh-durations"])


def _steps() -> list[dict[str, Any]]:
    return [s for s in _refresh_job().get("steps", []) if isinstance(s, dict)]


def _shard_steps() -> list[dict[str, Any]]:
    return [s for s in _jobs()["test-shard"].get("steps", []) if isinstance(s, dict)]


def _durations_upload_step() -> dict[str, Any]:
    matches = [s for s in _shard_steps() if "durations-shard-" in str(s.get("with", {}).get("name", ""))]
    assert len(matches) == 1, f"expected exactly one durations-upload step in test-shard, found {len(matches)}"
    return matches[0]


def _recording_guard_step() -> dict[str, Any]:
    matches = [s for s in _shard_steps() if "recorded no durations" in str(s.get("run", ""))]
    assert len(matches) == 1, (
        "test-shard must carry exactly one step that refuses to publish an unrewritten durations file — "
        "without it a silent recording failure uploads the STALE committed file and the refresh reports "
        f"a clean green no-op. Found {len(matches)}."
    )
    return matches[0]


def _download_step() -> dict[str, Any]:
    matches = [s for s in _steps() if "download-artifact" in str(s.get("uses", ""))]
    assert matches, "refresh-durations must download the shard durations artifacts."
    return matches[0]


def _merge_step() -> dict[str, Any]:
    matches = [s for s in _steps() if "durations_refresh.py" in str(s.get("run", ""))]
    assert matches, "refresh-durations must run scripts/ci/durations_refresh.py."
    return matches[0]


def _pr_step() -> dict[str, Any]:
    matches = [s for s in _steps() if "Open or update" in str(s.get("name", ""))]
    assert matches, "refresh-durations must have an 'Open or update ... refresh PR' step."
    return matches[0]


def _checkout_step() -> dict[str, Any]:
    matches = [s for s in _steps() if "actions/checkout" in str(s.get("uses", ""))]
    assert matches, "refresh-durations must have an actions/checkout step."
    return matches[0]


class TestRefreshBranchIsStable:
    """CI-3: a STABLE branch name means at most one open refresh PR, updated in place."""

    def test_branch_name_is_not_dated(self) -> None:
        run = str(_pr_step().get("run", ""))
        assert 'BRANCH="ci/test-durations-refresh"' in run, (
            "The refresh branch must be the STABLE name ci/test-durations-refresh so an "
            "unmerged refresh is force-updated in place, not stacked as a new PR each day."
        )

    def test_branch_does_not_embed_the_date(self) -> None:
        run = str(_pr_step().get("run", ""))
        branch_lines = [line for line in run.splitlines() if line.strip().startswith("BRANCH=")]
        assert branch_lines, "The PR step must assign a BRANCH variable."
        for line in branch_lines:
            assert "$(date" not in line, (
                "The refresh branch name must NOT embed the date — a dated branch opens a "
                "NEW PR every unmerged day, stacking conflicting PRs (CI-3)."
            )


class TestRefreshPrTriggersCi:
    """CI-2: the refresh PR must be opened with a token that fires the required check."""

    def test_pr_step_uses_the_pat_not_github_token(self) -> None:
        gh_token = str(_pr_step().get("env", {}).get("GH_TOKEN", ""))
        assert "TEATREE_GH_TOKEN" in gh_token, (
            "The refresh-PR step must use TEATREE_GH_TOKEN so the PR triggers the required "
            "test (3.13) check; the default GITHUB_TOKEN never fires it (un-mergeable PR)."
        )
        assert "github.token" not in gh_token, (
            "GH_TOKEN must NOT be the default github.token — a PR/push it authenticates never "
            "triggers downstream workflows, so the required check never runs (CI-2)."
        )

    def test_checkout_persists_the_pat_for_push(self) -> None:
        token = str(_checkout_step().get("with", {}).get("token", ""))
        assert "TEATREE_GH_TOKEN" in token, (
            "The checkout must persist TEATREE_GH_TOKEN so the `git push` to the refresh "
            "branch is attributed to a real identity and re-triggers CI on updates (CI-2)."
        )

    def test_pr_step_fails_loud_when_token_unset(self) -> None:
        run = str(_pr_step().get("run", ""))
        guard_msg = (
            "The refresh-PR step must fail LOUD when the CI-triggering token is unset, rather "
            "than silently opening a PR whose required check never fires (CI-2)."
        )
        assert 'if [ -z "${GH_TOKEN:-}" ]' in run, guard_msg
        assert "exit 1" in run, guard_msg


class TestDurationsUploadCanSeeTheHiddenFile:
    """CI-4 (#4477): the upload must opt in to hidden files, and fail where the fault is."""

    def test_upload_includes_hidden_files(self) -> None:
        with_block = _durations_upload_step().get("with", {})
        assert with_block.get("include-hidden-files") is True, (
            "`dev/.test_durations` is a HIDDEN file and actions/upload-artifact defaults "
            "include-hidden-files to false, so its search matched nothing and every shard uploaded "
            "an empty set while reporting success. This is the #4477 root cause: set "
            "include-hidden-files: true or refresh-durations can never receive a shard artifact."
        )

    def test_upload_fails_when_it_finds_nothing(self) -> None:
        with_block = _durations_upload_step().get("with", {})
        assert with_block.get("if-no-files-found") == "error", (
            "A refresh-SOURCE upload that finds no file is a failure, not a warning. The default "
            "`warn` is what deferred #4477 two jobs downstream, where it surfaced as an absent "
            "artifact in refresh-durations and read as a download problem."
        )

    def test_upload_still_publishes_the_committed_path(self) -> None:
        assert str(_durations_upload_step().get("with", {}).get("path", "")).strip() == "dev/.test_durations"

    def test_the_shard_refuses_to_publish_an_unrewritten_file(self) -> None:
        """include-hidden-files alone would upload the STALE committed file on a silent record failure."""
        run = str(_recording_guard_step().get("run", ""))
        assert "git diff --quiet -- dev/.test_durations" in run, (
            "The guard must compare the shard's durations file against the committed one — a shard "
            "whose recording silently failed would otherwise republish the stale file as freshly measured."
        )
        assert "exit 1" in run, "The guard must FAIL the shard, not warn."

    def test_the_recording_guard_runs_only_on_refresh_runs(self) -> None:
        upload_gate = str(_durations_upload_step().get("if", "")).strip()
        assert str(_recording_guard_step().get("if", "")).strip() == upload_gate, (
            "The guard must carry the same gate as the upload it protects; a guard on a different "
            "condition either reds ordinary PR runs or leaves refresh runs unguarded."
        )


class TestTheDownloadedArtifactPathsStillResolve:
    """The upload/download/merge contract is three files apart — a rename in one silently breaks it."""

    def test_download_collects_every_shard_under_the_expected_root(self) -> None:
        with_block = _download_step().get("with", {})
        assert str(with_block.get("pattern", "")).strip() == "durations-shard-*"
        assert str(with_block.get("path", "")).strip() == "/tmp/durations-shards"

    def test_merge_reads_the_path_the_download_writes(self) -> None:
        """A single-file artifact unpacks to <path>/<artifact-name>/.test_durations."""
        run = str(_merge_step().get("run", ""))
        for group in _SHARD_GROUPS:
            expected = f"/tmp/durations-shards/durations-shard-{group}/.test_durations"
            assert expected in run, (
                f"the merge step does not read {expected} — the twelve shard slices partition the "
                "suite, so a path that no longer resolves silently drops its slice."
            )
