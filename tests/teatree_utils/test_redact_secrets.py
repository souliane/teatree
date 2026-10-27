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

CHAIN_LENGTH = 10_000
LONG_CHAIN_LENGTH = 100_000
TIME_BUDGET_SECONDS = 1.0


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


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("refresh_token=notarealvalue", "refresh_token=<redacted>"),
        ("client_secret=notarealvalue", "client_secret=<redacted>"),
        ("private_token=notarealvalue", "private_token=<redacted>"),
        ("PRIVATE-TOKEN: notarealvalue", "PRIVATE-TOKEN: <redacted>"),
    ],
)
def test_credential_suffixes_and_private_token_header_are_redacted(arg: str, expected: str) -> None:
    assert redact_secrets(arg) == expected


class TestRedactionCostIsLinearInMessageLength:
    @pytest.mark.parametrize("unit", ["a-", "x-", "-Hx-", "%20x-"])
    def test_a_long_hyphen_chain_is_redacted_within_the_time_budget(self, unit: str) -> None:
        message = unit * CHAIN_LENGTH

        started = time.perf_counter()
        redact_secrets(message)

        assert time.perf_counter() - started < TIME_BUDGET_SECONDS

    @pytest.mark.parametrize(
        ("unit", "tail"),
        [
            ("a-", "token=notarealvalue"),
            ("x-", "X-Api-Key: notarealvalue"),
            ("-Hx-", "-HX-Api-Key: notarealvalue"),
            ("%20x-", "%20X-Api-Key: notarealvalue"),
        ],
    )
    def test_a_secret_after_a_long_chain_is_still_redacted_within_the_time_budget(self, unit: str, tail: str) -> None:
        message = f"{unit * CHAIN_LENGTH} {tail}"

        started = time.perf_counter()
        redacted = redact_secrets(message)

        assert time.perf_counter() - started < TIME_BUDGET_SECONDS
        assert "notarealvalue" not in redacted


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("token=notarealvalue", "token=<redacted>"),
        ("access_token=notarealvalue", "access_token=<redacted>"),
        ("key=notarealvalue", "key=<redacted>"),
        ("secret=notarealvalue", "secret=<redacted>"),
        ("api_key=notarealvalue", "api_key=<redacted>"),
        ("--token=notarealvalue", "--token=<redacted>"),
        ("--api-key=notarealvalue", "--api-key=<redacted>"),
        ("--password=notarealvalue", "--password=<redacted>"),
        ("--client_secret=notarealvalue", "--client_secret=<redacted>"),
        ("https://example.test/api?page=2&api-key=notarealvalue", "https://example.test/api?page=2&api-key=<redacted>"),
    ],
)
def test_credential_name_shapes_keep_their_name_and_lose_their_value(arg: str, expected: str) -> None:
    assert redact_secrets(arg) == expected


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("Proxy-Authorization: Bearer notarealvalue", "Proxy-Authorization: <redacted>"),
        ("X-Gitlab-Token: notarealvalue", "X-Gitlab-Token: <redacted>"),
        ("X-Api-Key: notarealvalue", "X-Api-Key: <redacted>"),
        ("--header=X-Api-Key: notarealvalue", "--header=X-Api-Key: <redacted>"),
        ("-HX-Api-Key: notarealvalue", "-HX-Api-Key: <redacted>"),
        ("curl -sS -HX-Api-Key: notarealvalue", "curl -sS -HX-Api-Key: <redacted>"),
        ("curl -HX-Gitlab-Token:notarealvalue", "curl -HX-Gitlab-Token: <redacted>"),
        ("curl -H X-Api-Key:notarealvalue", "curl -H X-Api-Key: <redacted>"),
        (
            "https://example.test/api?q=a%20X-Api-Key: notarealvalue",
            "https://example.test/api?q=a%20X-Api-Key: <redacted>",
        ),
        ("curl -sSHX-Api-Key:notarealvalue", "curl -sSHX-Api-Key: <redacted>"),
        ("curl -fsSLHX-Gitlab-Token:notarealvalue", "curl -fsSLHX-Gitlab-Token: <redacted>"),
        ("curl -vHX-Api-Key: notarealvalue", "curl -vHX-Api-Key: <redacted>"),
        ("Index-Key: notarealvalue", "Index-Key: <redacted>"),
    ],
)
def test_credential_header_shapes_keep_their_name_and_lose_their_value(arg: str, expected: str) -> None:
    assert redact_secrets(arg) == expected


@pytest.mark.parametrize("arg", ["token_count=5", "monkey=1", "max_tokens=100"])
def test_a_name_that_only_contains_a_credential_word_is_untouched(arg: str) -> None:
    assert redact_secrets(arg) == arg


@pytest.mark.parametrize("unit", ["x-", "-Hx-", "%20x-"])
def test_a_very_long_chain_followed_by_a_secret_is_redacted_within_the_time_budget(unit: str) -> None:
    message = f"{unit * LONG_CHAIN_LENGTH} X-Api-Key: notarealvalue"

    started = time.perf_counter()
    redacted = redact_secrets(message)

    assert time.perf_counter() - started < TIME_BUDGET_SECONDS
    assert "notarealvalue" not in redacted


def test_a_header_name_of_one_hundred_characters_is_not_required_to_redact() -> None:
    message = f"x-{'a' * 98}-key: notarealvalue"

    started = time.perf_counter()
    redact_secrets(message)

    assert time.perf_counter() - started < TIME_BUDGET_SECONDS
