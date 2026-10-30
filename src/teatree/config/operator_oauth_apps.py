from typing import cast
from urllib.parse import SplitResult, urlsplit


class OperatorOAuthConfigError(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid operator OAuth app configuration")


def parse_operator_oauth_apps(raw: object) -> dict[str, dict[str, str]]:
    if not isinstance(raw, dict):
        message = "operator OAuth apps must be a table"
        raise TypeError(message)
    apps: dict[str, dict[str, str]] = {}
    for forge, item in raw.items():
        if forge not in {"github", "gitlab"} or not isinstance(item, dict):
            raise OperatorOAuthConfigError
        required = {"client_id", "client_secret_pass_key", "redirect_uri"}
        allowed = required | ({"base_url"} if forge == "gitlab" else set())
        if not required <= item.keys() or item.keys() - allowed:
            raise OperatorOAuthConfigError
        if any(not isinstance(value, str) or not value or "\0" in value for value in item.values()):
            raise OperatorOAuthConfigError
        if any(char.isspace() for char in item["client_secret_pass_key"]):
            raise OperatorOAuthConfigError
        callback = urlsplit(item["redirect_uri"])
        if (
            callback.path != f"/login/oauth/{forge}/callback/"
            or callback.query
            or callback.fragment
            or not _operator_oauth_url_is_safe(callback)
        ):
            raise OperatorOAuthConfigError
        if "base_url" in item:
            base = urlsplit(item["base_url"])
            if base.path not in {"", "/"} or base.query or base.fragment or not _operator_oauth_url_is_safe(base):
                raise OperatorOAuthConfigError
        apps[forge] = cast("dict[str, str]", item)
    return apps


def _operator_oauth_url_is_safe(url: SplitResult) -> bool:
    return bool(
        url.hostname
        and not url.username
        and not url.password
        and (url.scheme == "https" or (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"}))
    )
