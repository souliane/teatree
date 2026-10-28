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

import time

import pytest

from teatree.utils.run import redact_secrets

CHAIN_UNITS = 2_000
CHAIN_GROWTH = 4
# Linear cost grows about 4x for a 4x longer input, quadratic about 16x.
MAX_COST_GROWTH = 10
TIMING_REPEATS = 5


class TestEnvAssignmentsAreRedacted:
    @pytest.mark.parametrize(
        "arg",
        [
            "ANTHROPIC_API_KEY=sk-ant-notarealvalue",  # privacy-scan:allow fake test fixture
            "T3_SECRET_KEY=notarealvalue",
            "GITLAB_TOKEN=glpat-notarealvalue",  # privacy-scan:allow fake test fixture
            "POSTGRES_PASSWORD=notarealvalue",
            "MY_APP_SECRET=notarealvalue",
            "--env=GITLAB_TOKEN=glpat-notarealvalue",  # privacy-scan:allow fake test fixture
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
            "monkey=1",
            "the bearer of this note",
            "https://example.test:8443?next=a@b",
            '{"name": "abc123XYZ"}',
            "ssh://git@host.test",
            "ssh://abcdefghijk@host.test",
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


class TestCredentialNamesAreRedactedWhateverTheirPrefix:
    @pytest.mark.parametrize(
        ("arg", "expected"),
        [
            ("refresh_token=notarealvalue", "refresh_token=<redacted>"),
            ("client_secret=notarealvalue", "client_secret=<redacted>"),
            ("db_password=notarealvalue", "db_password=<redacted>"),
            ("key=notarealvalue", "key=<redacted>"),
            ("--api-key=notarealvalue", "--api-key=<redacted>"),
        ],
    )
    def test_the_name_survives_and_the_value_is_lost(self, arg: str, expected: str) -> None:
        assert redact_secrets(arg) == expected


class TestCredentialHeadersAreRedacted:
    @pytest.mark.parametrize(
        ("arg", "expected"),
        [
            ("PRIVATE-TOKEN: notarealvalue", "PRIVATE-TOKEN: <redacted>"),
            ("X-Gitlab-Token: notarealvalue", "X-Gitlab-Token: <redacted>"),
            ("X-Api-Key: notarealvalue", "X-Api-Key: <redacted>"),
            ("curl -sSHX-Api-Key:notarealvalue", "curl -sSHX-Api-Key: <redacted>"),
            (f"X-{'a' * 54}-Key: notarealvalue", f"X-{'a' * 54}-Key: <redacted>"),
        ],
    )
    def test_the_name_survives_and_the_value_is_lost(self, arg: str, expected: str) -> None:
        assert redact_secrets(arg) == expected


class TestBearerUserinfoAndQuotedCredentialsAreRedacted:
    @pytest.mark.parametrize(
        ("arg", "expected"),
        [
            ("retry with bearer abc123XYZ now", "retry with bearer <redacted> now"),
            ("https://user:abc123XYZ@host.test:8443/path", "https://user:<redacted>@host.test:8443/path"),
            ("scheme://abc123XYZ@host.test", "scheme://<redacted>@host.test"),
            ("scheme://abc_xyz@host.test", "scheme://<redacted>@host.test"),
            ("scheme://abc-xyz@host.test", "scheme://<redacted>@host.test"),
            ("scheme://abcdefghijkl@host.test", "scheme://<redacted>@host.test"),
            ('{"access_token": "abc123XYZ"}', '{"access_token": "<redacted>"}'),
            ('{"client_secret":"abc123XYZ"}', '{"client_secret":"<redacted>"}'),
            ("{'password': 'abc123XYZ'}", "{'password': '<redacted>'}"),
            ('{"api-key" : "abc123XYZ"}', '{"api-key" : "<redacted>"}'),
        ],
    )
    def test_the_surrounding_text_survives_and_the_value_is_lost(self, arg: str, expected: str) -> None:
        assert redact_secrets(arg) == expected


def _best_cpu_time(message: str) -> float:
    # CPU time, not wall time: a process descheduled on a loaded box must not skew the ratio.
    best = float("inf")
    for _ in range(TIMING_REPEATS):
        started = time.process_time()
        redact_secrets(message)
        best = min(best, time.process_time() - started)
    return best


class TestRedactionCostIsLinearInMessageLength:
    @pytest.mark.parametrize(
        ("unit", "tail"),
        [
            ("a-", "token=notarealvalue"),
            ("x-", "X-Api-Key: notarealvalue"),
            ("bearer ", "Bearer notarealvalue"),
            ("://a:", "://user:notarealvalue@host"),
            ('"a-', '"client_secret": "notarealvalue"'),
        ],
    )
    def test_a_longer_repetitive_message_costs_proportionally_more_and_is_still_redacted(
        self, unit: str, tail: str
    ) -> None:
        short = f"{unit * CHAIN_UNITS} {tail}"
        long = f"{unit * CHAIN_UNITS * CHAIN_GROWTH} {tail}"

        assert "notarealvalue" not in redact_secrets(long)
        assert _best_cpu_time(long) / _best_cpu_time(short) < MAX_COST_GROWTH
