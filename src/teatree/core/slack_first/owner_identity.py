"""The owner's Slack user id as a messaging backend carries it."""


def configured_owner_id(backend: object) -> str:
    return str(getattr(backend, "user_id", "") or "")
