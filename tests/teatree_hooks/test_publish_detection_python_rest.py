"""Tests for python-script REST-publish detection (the gap found via PR #2943).

``extract_publish_payload`` (banned-terms #1415, quote-scanner #1213) gates
ALL scanning on ``_command_parser.is_publish_command`` -- a ``python3``/
``python``-led segment POSTing/PATCHing to a forge REST API (the
"Post or Update Note with Images" recipe in ``skills/platforms/references/
gitlab.md``) was never recognised as a publish action at all, so the
leak-prevention scan never ran against it, on ANY repo, public or private.

``segment_is_python_rest_publish`` / ``command_has_python_rest_publish_surface``
generalise the SAME write-method + forge-target two-part test
``segment_is_api_write`` already applies to a ``gh``/``glab api`` call to an
interpreted script the CLI-specific argument walkers cannot parse a
``--repo``/URL out of: a python REST client hitting a forge's REST API is
structurally the same shape as a raw ``curl`` POST, just authored in Python
(``requests``/``httpx``/``urllib``/a raw ``Authorization``-headed call)
instead of CLI flags.
"""

import pytest

from teatree.hooks import _command_parser
from teatree.hooks._python_rest_detection import (
    command_has_python_rest_publish_surface,
    find_python_forge_rest_urls,
    is_python_leader,
    segment_is_python_rest_publish,
)

_GITLAB_NOTE_URL = "https://gitlab.com/api/v4/projects/42/merge_requests/5/notes"
_GITHUB_COMMENT_URL = "https://api.github.com/repos/owner/repo/issues/5/comments"


class TestIsPythonLeader:
    @pytest.mark.parametrize("word", ["python", "python3", "python3.11", "python3.12", "/usr/bin/python3"])
    def test_recognised_interpreter_spellings(self, word: str) -> None:
        assert is_python_leader(word)

    @pytest.mark.parametrize("word", ["gh", "glab", "curl", "git", "pythonic", "python-is-fun"])
    def test_non_interpreter_words_are_not_leaders(self, word: str) -> None:
        assert not is_python_leader(word)


class TestSegmentIsPythonRestPublish:
    """The write-method + forge-target two-part test, mirroring `segment_is_api_write`."""

    def test_requests_post_to_gitlab_is_detected(self) -> None:
        command = (
            f"python3 -c \"import requests; requests.post('{_GITLAB_NOTE_URL}', "
            "json={'body': 'note'}, headers={'PRIVATE-TOKEN': token})\""
        )
        words = ["python3", "-c", command.split(" -c ", 1)[1].strip('"')]
        assert segment_is_python_rest_publish(words, command)

    def test_requests_patch_to_github_is_detected(self) -> None:
        command = (
            f"python3 -c \"import requests; requests.patch('{_GITHUB_COMMENT_URL}', "
            "json={'body': 'note'}, headers={'Authorization': f'Bearer {token}'})\""
        )
        words = ["python3", "-c", command.split(" -c ", 1)[1].strip('"')]
        assert segment_is_python_rest_publish(words, command)

    def test_httpx_post_is_detected(self) -> None:
        command = f"python3 -c \"import httpx; httpx.post('{_GITLAB_NOTE_URL}', json={{}})\""
        words = ["python3", "-c", command.split(" -c ", 1)[1].strip('"')]
        assert segment_is_python_rest_publish(words, command)

    def test_urllib_request_method_post_is_detected(self) -> None:
        command = (
            'python3 -c "import urllib.request; '
            f"req = urllib.request.Request('{_GITLAB_NOTE_URL}', data=b'{{}}', method='POST')\""
        )
        words = ["python3", "-c", command.split(" -c ", 1)[1].strip('"')]
        assert segment_is_python_rest_publish(words, command)

    def test_raw_authorization_header_to_forge_is_detected(self) -> None:
        # No recognised client-library call name (http.client / raw socket) --
        # the Authorization header + forge target alone is the detection surface.
        command = (
            "python3 -c \"import http.client; conn.request('POST', "
            f"'{_GITHUB_COMMENT_URL}', headers={{'Authorization': 'Bearer ' + token}})\""
        )
        words = ["python3", "-c", command.split(" -c ", 1)[1].strip('"')]
        assert segment_is_python_rest_publish(words, command)

    def test_heredoc_fed_script_is_detected(self) -> None:
        command = (
            "python3 << 'PYEOF'\n"
            "import json, urllib.request\n"
            f"url = '{_GITLAB_NOTE_URL}'\n"
            "req = urllib.request.Request(url, data=b'{}', method='POST', "
            "headers={'PRIVATE-TOKEN': token})\n"
            "urllib.request.urlopen(req)\n"
            "PYEOF"
        )
        assert command_has_python_rest_publish_surface(command)

    def test_read_only_python_call_is_not_a_publish(self) -> None:
        command = f"python3 -c \"import requests; requests.get('{_GITLAB_NOTE_URL}')\""
        words = ["python3", "-c", command.split(" -c ", 1)[1].strip('"')]
        assert not segment_is_python_rest_publish(words, command)

    def test_write_call_to_non_forge_host_is_not_a_publish(self) -> None:
        command = "python3 -c \"import requests; requests.post('https://example.com/api/notes', json={})\""
        words = ["python3", "-c", command.split(" -c ", 1)[1].strip('"')]
        assert not segment_is_python_rest_publish(words, command)

    def test_non_python_leader_is_not_a_publish(self) -> None:
        words = ["curl", "-X", "POST", _GITLAB_NOTE_URL]
        assert not segment_is_python_rest_publish(words, " ".join(words))

    def test_empty_words_is_not_a_publish(self) -> None:
        assert not segment_is_python_rest_publish([], "")


class TestFindPythonForgeRestUrls:
    """The resolver both the classifier's existence check and ``publish_destination`` reuse."""

    def test_yields_every_forge_url_in_source(self) -> None:
        # Every consumer only pulls the FIRST yielded value (``next(...)``/a
        # ``for ... return`` loop) -- a source carrying more than one forge URL
        # is the only way to observe the generator resume past its first yield.
        source = f"first at {_GITHUB_COMMENT_URL} then {_GITLAB_NOTE_URL}"
        assert list(find_python_forge_rest_urls(source)) == [("github", "owner/repo"), ("gitlab", "42")]

    def test_yields_nothing_for_a_non_forge_url(self) -> None:
        assert list(find_python_forge_rest_urls("https://example.com/api/notes")) == []


class TestIsPublishCommandRecognizesPythonRestPublish:
    """RED-before-fix: ``is_publish_command`` never classified a python REST publish."""

    def test_inline_c_script_is_a_publish_command(self) -> None:
        command = (
            f"python3 -c \"import requests; requests.post('{_GITLAB_NOTE_URL}', "
            "json={'body': 'note'}, headers={'PRIVATE-TOKEN': token})\""
        )
        assert _command_parser.is_publish_command(command)

    def test_heredoc_script_is_a_publish_command(self) -> None:
        command = (
            "python3 << 'PYEOF'\n"
            "import json, urllib.request\n"
            f"url = '{_GITLAB_NOTE_URL}'\n"
            "req = urllib.request.Request(url, data=b'{}', method='POST', "
            "headers={'PRIVATE-TOKEN': token})\n"
            "urllib.request.urlopen(req)\n"
            "PYEOF"
        )
        assert _command_parser.is_publish_command(command)

    def test_read_only_script_is_not_a_publish_command(self) -> None:
        command = f"python3 -c \"import requests; requests.get('{_GITLAB_NOTE_URL}')\""
        assert not _command_parser.is_publish_command(command)

    def test_unrelated_python_invocation_is_not_a_publish_command(self) -> None:
        assert not _command_parser.is_publish_command("python3 manage.py migrate")
