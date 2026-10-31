"""``t3 <overlay> publish``: the one sanctioned way to write a PR body, issue body or comment."""

from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

from tests._send_gate import allow_github_repos
from tests.teatree_core.publishing._recording_host import RecordingHost

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_PR = "https://github.com/souliane/teatree/pull/5870"
_ISSUE = "https://github.com/souliane/teatree/issues/12"


@pytest.fixture(autouse=True)
def _private_allowlisted_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.core.gates.privacy_gate._target_is_public", lambda *_, **__: False)
    allow_github_repos("souliane/teatree")


def _publish(host: RecordingHost, *args: str) -> str:
    out = StringIO()
    with patch("teatree.core.management.commands.publish.build_publish_host", return_value=host):
        call_command("publish", *args, stdout=out, stderr=StringIO())
    return out.getvalue()


def _body(tmp_path: Path, text: str) -> str:
    path = tmp_path / "body.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


class TestPublishCommand:
    def test_a_clean_body_file_is_written_and_reported(self, tmp_path: Path) -> None:
        host = RecordingHost()
        out = _publish(host, "pr-body", _PR, "--body-file", _body(tmp_path, "Tidies it.\n\nRelates to #12"))
        assert len(host.writes) == 1
        assert "published" in out

    def test_a_closing_keyword_exits_one_with_zero_writes(self, tmp_path: Path) -> None:
        host = RecordingHost()
        with pytest.raises(SystemExit) as exited:
            _publish(host, "pr-body", _PR, "--body-file", _body(tmp_path, "Closes #12"))
        assert exited.value.code == 1
        assert host.writes == []

    def test_closes_lets_the_named_ref_through(self, tmp_path: Path) -> None:
        host = RecordingHost()
        args = ("issue-body", _ISSUE, "--body-file", _body(tmp_path, "Closes #12"), "--closes", "souliane/teatree#12")
        _publish(host, *args)
        assert len(host.writes) == 1

    def test_a_forge_that_appends_a_trailer_exits_one(self, tmp_path: Path) -> None:
        trailer = "\n\nCo-Authored-By: Claude <noreply@anthropic.com>"  # privacy-scan:allow fake trailer fixture
        host = RecordingHost(appended=trailer)
        with pytest.raises(SystemExit) as exited:
            _publish(host, "comment", _ISSUE, "--body-file", _body(tmp_path, "A note."))
        assert exited.value.code == 1

    def test_a_missing_body_file_exits_one(self, tmp_path: Path) -> None:
        host = RecordingHost()
        with pytest.raises(SystemExit) as exited:
            _publish(host, "comment", _ISSUE, "--body-file", str(tmp_path / "absent.md"))
        assert exited.value.code == 1
        assert host.writes == []
