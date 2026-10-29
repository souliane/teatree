import pytest

from teatree.config import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.operator_oauth_apps import parse_operator_oauth_apps


def test_oauth_app_config_uses_only_bootstrap_secret_references() -> None:
    apps = {
        "github": {
            "client_id": "example-client",
            "client_secret_pass_key": "github/operator-oauth-client-secret",
            "redirect_uri": "https://factory.example/login/oauth/github/callback/",
        }
    }

    assert parse_operator_oauth_apps(apps) == apps
    assert ALL_KNOWN_CONFIG_SETTINGS["operator_oauth_apps"] is parse_operator_oauth_apps


@pytest.mark.parametrize(
    "bad",
    [
        {"github": {"client_id": "id", "client_secret": "literal-secret", "redirect_uri": "https://example/"}},
        {"slack": {"client_id": "id", "client_secret_pass_key": "secret", "redirect_uri": "https://example/"}},
        {
            "github": {
                "client_id": "id",
                "client_secret_pass_key": "secret",
                "redirect_uri": "http://example.com/login/oauth/github/callback/",
            }
        },
    ],
)
def test_oauth_app_config_rejects_secret_values_unknown_forges_and_insecure_callbacks(bad: object) -> None:
    with pytest.raises(ValueError, match="operator OAuth"):
        parse_operator_oauth_apps(bad)
