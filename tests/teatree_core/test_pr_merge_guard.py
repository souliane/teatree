"""#762 merge-path author guard (defense-in-depth for the server-side squash).

`gh pr merge --squash` takes the merging GitHub *account* email when
`--author-email` is omitted — the server-side squash commit never passes
through the #730 pre-push author guard. The helper here always passes an
explicit noreply `--author-email` on public souliane/* and FAILS CLOSED
if the resulting squash commit author is non-noreply. Only the `gh`
subprocess boundary is mocked; the helper logic + noreply regex are real.
"""

from unittest.mock import patch

import pytest

from teatree.core.pr_merge import squash_merge_public
from teatree.core.public_identity import MergeAuthorMismatchError


def _gh(argv: list[str], returncode: int = 0, stdout: str = "") -> tuple[int, str, str]:
    return (returncode, stdout, "")


class TestSquashMergePublic:
    def test_passes_noreply_author_email_on_public_souliane(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **_kw: object) -> tuple[int, str, str]:
            calls.append(argv)
            if "view" in argv:  # mergeCommit SHA lookup
                return (0, "abc1234deadbeef\n", "")
            if "api" in argv:  # the post-merge author verification
                return (0, "21343492+souliane@users.noreply.github.com\n", "")
            return (0, "", "")

        with patch("teatree.core.pr_merge._run_gh", side_effect=fake_run):
            squash_merge_public(pr=748, slug="souliane/teatree")

        merge_argv = next(c for c in calls if "merge" in c)
        assert "--squash" in merge_argv
        assert "--author-email" in merge_argv
        idx = merge_argv.index("--author-email")
        from teatree.core.public_identity import is_noreply_email  # noqa: PLC0415

        assert is_noreply_email(merge_argv[idx + 1]), merge_argv[idx + 1]

    def test_fails_closed_when_resulting_author_is_non_noreply(self) -> None:
        def fake_run(argv: list[str], **_kw: object) -> tuple[int, str, str]:
            if "view" in argv:
                return (0, "abc1234deadbeef\n", "")
            if "api" in argv:
                # The squash commit landed with a non-noreply author
                # despite our request — must HALT, never silently proceed.
                return (0, "real.dev@internal.example\n", "")
            return (0, "", "")

        with (
            patch("teatree.core.pr_merge._run_gh", side_effect=fake_run),
            pytest.raises(MergeAuthorMismatchError),
        ):
            squash_merge_public(pr=999, slug="souliane/teatree")

    def test_private_repo_is_exempt_no_author_email_no_guard(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **_kw: object) -> tuple[int, str, str]:
            calls.append(argv)
            return (0, "", "")

        with patch("teatree.core.pr_merge._run_gh", side_effect=fake_run):
            squash_merge_public(pr=1, slug="acme-private/internal-svc")

        merge_argv = next(c for c in calls if "merge" in c)
        assert "--author-email" not in merge_argv  # exempt: real internal email is fine
        assert not any("api" in c for c in calls)  # no fail-closed guard on private

    def test_merge_failure_propagates_without_claiming_success(self) -> None:
        def fake_run(argv: list[str], **_kw: object) -> tuple[int, str, str]:
            if "merge" in argv:
                return (1, "", "merge conflict")
            return (0, "", "")

        with (
            patch("teatree.core.pr_merge._run_gh", side_effect=fake_run),
            pytest.raises(RuntimeError, match=r"(?i)merge"),
        ):
            squash_merge_public(pr=2, slug="souliane/teatree")


class TestPrMergeCommandWiring:
    def test_command_delegates_to_squash_merge_public(self) -> None:
        from django.core.management import call_command  # noqa: PLC0415

        with patch("teatree.core.pr_merge.squash_merge_public") as helper:
            result = call_command("pr", "merge", "748", "souliane/teatree")

        helper.assert_called_once_with(pr=748, slug="souliane/teatree", auto=False)
        assert result == {"merged": True, "pr": 748, "slug": "souliane/teatree", "auto": False}
