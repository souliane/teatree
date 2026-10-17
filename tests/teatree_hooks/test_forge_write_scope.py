import pytest

from teatree.hooks.forge_write_scope import is_forge_write


@pytest.mark.parametrize(
    "command",
    [
        "gh issue comment 5 --repo owner/repo --body 'note'",
        "glab mr note 5 -R group/repo -m 'note'",
        "gh api repos/owner/repo/issues/5/comments -f body=note",
        'sh -c "gh pr create --repo owner/repo --title t --body note"',
        'curl -X POST -d \'{"body": "note"}\' https://api.github.com/repos/owner/repo/issues/5/comments',
        "python3 -c \"import requests; requests.post('https://api.github.com/repos/owner/repo/issues', json={})\"",
        "t3 acme review post-comment group/repo 5 'note'",
    ],
)
def test_forge_publish_is_a_forge_write(command: str) -> None:
    assert is_forge_write(command, None)


@pytest.mark.parametrize(
    "command",
    [
        "curl -X POST -d 'text=note' https://slack.com/api/chat.postMessage",
        "t3 acme notify send 'note'",
        "t3 slack react --channel C1 --ts 1.2 --emoji eyes",
    ],
)
def test_slack_publish_is_not_a_forge_write(command: str) -> None:
    assert not is_forge_write(command, None)
