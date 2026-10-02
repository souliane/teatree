"""``redact_secrets`` must cover every credential-carrying word shape, not just headers and queries.

A failing command's argv is echoed verbatim into the transcript by
:class:`CommandFailedError`, so whatever this function misses is published. It
covered an ``Authorization:`` header and a ``?token=`` query parameter and
nothing else — an ``NAME=value`` assignment (a ``docker run -e`` forward, an env
prefix on a command) passed straight through, which is how ``T3_SECRET_KEY`` and
an ``ANTHROPIC_API_KEY`` reached a transcript.

The name test is by SUFFIX CLASS — ``KEY`` / ``SECRET`` / ``PASSWORD`` / ``TOKEN``
— because a per-variable list is a list someone forgets to extend.
"""

import pytest

from teatree.utils.run import redact_secrets


class TestEnvAssignmentsAreRedacted:
    @pytest.mark.parametrize(
        "arg",
        [
            "ANTHROPIC_API_KEY=sk-ant-notarealvalue",
            "T3_SECRET_KEY=notarealvalue",
            "GITLAB_TOKEN=glpat-notarealvalue",
            "POSTGRES_PASSWORD=notarealvalue",
            "MY_APP_SECRET=notarealvalue",
            "--env=GITLAB_TOKEN=glpat-notarealvalue",
        ],
    )
    def test_credential_assignment_loses_its_value(self, arg: str) -> None:
        redacted = redact_secrets(arg)
        assert "notarealvalue" not in redacted, f"{arg!r} published its value as {redacted!r}"

    @pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "T3_SECRET_KEY", "GITLAB_TOKEN", "POSTGRES_PASSWORD"])
    def test_the_variable_name_is_kept(self, name: str) -> None:
        # The name is what makes a failure diagnosable; only the value is a secret.
        assert name in redact_secrets(f"{name}=notarealvalue")


class TestNonCredentialArgsAreUntouched:
    @pytest.mark.parametrize(
        "arg",
        [
            "DATABASE_NAME=orders",
            "PYTHONPATH=/app/src",
            "COVERAGE_FILE=/tmp/.coverage",
            "T3_E2E_TARGET=dev",
            "--keyfile=/etc/ssl/app.pem",
        ],
    )
    def test_ordinary_assignment_is_unchanged(self, arg: str) -> None:
        assert redact_secrets(arg) == arg, "redacting a non-credential argument makes a failure unreadable"


class TestExistingCoverageUnchanged:
    def test_authorization_header_is_still_redacted(self) -> None:
        assert "notarealvalue" not in redact_secrets("Authorization: Bearer notarealvalue")

    def test_query_parameter_is_still_redacted(self) -> None:
        assert "notarealvalue" not in redact_secrets("https://example.test/api?token=notarealvalue&page=2")

    def test_a_plain_word_is_untouched(self) -> None:
        assert redact_secrets("migrate") == "migrate"
