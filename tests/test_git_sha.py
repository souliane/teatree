import pytest

from teatree.git_sha import is_full_git_sha

SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (SHA, True),
        (SHA.upper(), False),
        (SHA[:39], False),
        (f"{SHA}0", False),
        ("main", False),
        ("", False),
    ],
)
def test_only_a_full_lowercase_commit_sha_is_valid(value: str, *, expected: bool) -> None:
    assert is_full_git_sha(value) is expected
