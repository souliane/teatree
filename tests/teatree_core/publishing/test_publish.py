"""publish_text: scan, write, read back, rescan; a text that is not provably clean live exits non-zero."""

import pytest

from teatree.core.publishing.publish import PublishKind, PublishRefusedError, PublishRequest, publish_text
from tests._send_gate import allow_github_repos
from tests.teatree_core.publishing._recording_host import RecordingHost

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_PR = "https://github.com/souliane/teatree/pull/5870"
_ISSUE = "https://github.com/souliane/teatree/issues/12"
_FOOTER = "\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)"


@pytest.fixture(autouse=True)
def _private_allowlisted_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.core.gates.privacy_gate._target_is_public", lambda *_, **__: False)
    allow_github_repos("souliane/teatree")


def _request(text: str, kind: PublishKind = PublishKind.PR_BODY, *, closes: tuple[str, ...] = ()) -> PublishRequest:
    return PublishRequest(kind=kind, target=_PR if kind is PublishKind.PR_BODY else _ISSUE, text=text, closes=closes)


class TestProposedTextIsScannedBeforeAnyWrite:
    def test_a_closing_keyword_is_refused_with_zero_writes(self) -> None:
        host = RecordingHost()
        with pytest.raises(PublishRefusedError, match="closing keyword"):
            publish_text(_request("Tidies it.\n\nCloses #12"), host)
        assert host.writes == []

    def test_the_same_body_without_the_keyword_is_written_once(self) -> None:
        host = RecordingHost()
        result = publish_text(_request("Tidies it.\n\nRelates to #12"), host)
        assert [(kind, target) for kind, target, _ in host.writes] == [(PublishKind.PR_BODY, _PR)]
        assert result.text == "Tidies it.\n\nRelates to #12"

    def test_closes_permits_only_the_named_ref(self) -> None:
        host = RecordingHost()
        publish_text(_request("Closes #12", closes=("souliane/teatree#12",)), host)
        assert len(host.writes) == 1
        with pytest.raises(PublishRefusedError, match="closing keyword"):
            publish_text(_request("Closes #13", closes=("souliane/teatree#12",)), host)
        assert len(host.writes) == 1

    def test_an_ai_signature_is_refused_with_zero_writes(self) -> None:
        host = RecordingHost()
        with pytest.raises(PublishRefusedError, match="AI-signature"):
            publish_text(_request(f"Tidies it.\n\n{_FOOTER}"), host)
        assert host.writes == []

    def test_an_empty_text_is_refused_with_zero_writes(self) -> None:
        host = RecordingHost()
        with pytest.raises(PublishRefusedError, match="empty"):
            publish_text(_request("  \n"), host)
        assert host.writes == []


class TestLiveTextIsReadBackAndRescanned:
    def test_a_forge_that_appends_a_banned_trailer_is_refused(self) -> None:
        host = RecordingHost(appended=f"\n\n{_FOOTER}")
        with pytest.raises(PublishRefusedError, match="AI-signature"):
            publish_text(_request("Tidies it."), host)
        assert len(host.writes) == 1

    def test_a_forge_that_returns_different_text_is_refused(self) -> None:
        host = RecordingHost(replaced_with="something else entirely")
        with pytest.raises(PublishRefusedError, match="differs"):
            publish_text(_request("Tidies it."), host)

    def test_an_unreadable_forge_is_refused(self) -> None:
        host = RecordingHost(unreadable=True)
        with pytest.raises(PublishRefusedError, match="could not be read back"):
            publish_text(_request("Tidies it."), host)

    def test_a_forge_that_appends_a_closing_keyword_is_refused(self) -> None:
        host = RecordingHost(appended="\n\nCloses #99")
        with pytest.raises(PublishRefusedError, match="closing keyword"):
            publish_text(_request("Tidies it."), host)

    def test_a_comment_and_an_issue_body_publish_through_their_own_kinds(self) -> None:
        host = RecordingHost()
        publish_text(_request("A note.", PublishKind.COMMENT), host)
        publish_text(_request("A body.", PublishKind.ISSUE_BODY), host)
        assert [kind for kind, _, _ in host.writes] == [PublishKind.COMMENT, PublishKind.ISSUE_BODY]


class TestAnUnlistedDestinationIsRefused:
    def test_a_repo_off_the_send_proxy_allowlist_gets_zero_writes(self) -> None:
        allow_github_repos("someone/else")
        host = RecordingHost()
        with pytest.raises(PublishRefusedError, match="allowlist"):
            publish_text(_request("Tidies it."), host)
        assert host.writes == []
