from subprocess import CompletedProcess
from unittest.mock import patch

from teatree.utils.git_branch import RefPresence, list_remote_refs, ref_presence


def _probe(code: int, stdout: str = "") -> CompletedProcess[str]:
    return CompletedProcess(args=["git"], returncode=code, stdout=stdout, stderr="")


def test_ref_absent_in_all_three_places_is_proven_absent() -> None:
    with patch("teatree.utils.git_branch.run_with_status", side_effect=[_probe(1), _probe(1), _probe(2)]) as run:
        result = ref_presence("/repo", "missing")

    assert result is RefPresence.ABSENT
    assert run.call_count == 3
    assert run.call_args.args == ()
    assert run.call_args.kwargs["args"] == ["ls-remote", "--exit-code", "origin", "refs/heads/missing"]


def test_failed_remote_probe_cannot_prove_absence() -> None:
    with patch("teatree.utils.git_branch.run_with_status", side_effect=[_probe(1), _probe(1), _probe(128)]):
        assert ref_presence("/repo", "unknown") is RefPresence.UNKNOWN


def test_one_remote_listing_serves_many_absent_branches() -> None:
    with patch(
        "teatree.utils.git_branch.run_with_status",
        side_effect=[_probe(0, "abc\trefs/heads/other\n"), _probe(1), _probe(1), _probe(1), _probe(1)],
    ) as run:
        listing = list_remote_refs("/repo")
        first = ref_presence("/repo", "one", listing=listing)
        second = ref_presence("/repo", "two", listing=listing)

    assert (first, second) == (RefPresence.ABSENT, RefPresence.ABSENT)
    assert sum(call.kwargs["args"][:1] == ["ls-remote"] for call in run.call_args_list) == 1
    assert run.call_args_list[0].kwargs["timeout"] == 30


def test_failed_listing_keeps_absent_local_refs_unknown() -> None:
    with patch("teatree.utils.git_branch.run_with_status", side_effect=[_probe(128), _probe(1), _probe(1)]) as run:
        listing = list_remote_refs("/repo")
        result = ref_presence("/repo", "missing", listing=listing)

    assert result is RefPresence.UNKNOWN
    assert run.call_count == 3
