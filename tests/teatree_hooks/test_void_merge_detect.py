import pytest

from teatree.hooks.void_merge_detect import DENY_REASON, void_merge_deny_reason

_URL = "https://github.com/example-org/example-repo/pull/41"


@pytest.mark.parametrize(
    "command",
    [
        f"t3 teatree pr void-merge {_URL} --reason 'history rewrite' --by alice",
        f"deploy/t3 teatree pr void-merge {_URL} --reason x --by alice --no-dry-run",
        f"cd /tmp && t3 teatree pr void-merge {_URL} --reason x --by alice",
        f"python manage.py pr void-merge {_URL} --reason x --by alice",
        f"echo ok; $(t3 teatree pr void-merge {_URL} --reason x --by alice)",
    ],
)
def test_a_writing_void_is_refused(command: str) -> None:
    assert void_merge_deny_reason(command) == DENY_REASON


@pytest.mark.parametrize(
    "command",
    [
        f"t3 teatree pr void-merge {_URL} --dry-run",
        "t3 teatree pr void-merge --help",
        f"grep -rn 'pr void-merge {_URL}' docs/",
        f"echo 't3 teatree pr void-merge {_URL} --reason x --by alice'",
        "t3 teatree pr create 1844",
        "",
    ],
)
def test_reads_and_mentions_pass(command: str) -> None:
    assert void_merge_deny_reason(command) is None
