"""Slack ``ok:false`` error-code classification for ``SlackBotBackend`` (#1287).

Slack returns ``{"ok": false, "error": "<code>"}`` for any API failure.
Two classes matter to the scanner dispatcher:

*   Global token failures (``invalid_auth``, ``missing_scope``,
    ``ratelimited``, …) suppress every Slack scan and must raise
    :class:`ScannerError` so the dispatcher records the error and DMs
    the user — silent fall-through to ``[]`` masks the entire
    workspace integration.
*   Channel-scoped failures (``channel_not_found``, ``not_in_channel``,
    ``is_archived``) legitimately degrade to "one channel unreachable"
    and keep the rest of the scan running, so they are NOT in this set.
"""

from teatree.types import ScannerErrorClass

GLOBAL_TOKEN_FAILURES: dict[str, ScannerErrorClass] = {
    "invalid_auth": ScannerErrorClass.AUTH,
    "not_authed": ScannerErrorClass.AUTH,
    "token_expired": ScannerErrorClass.AUTH,
    "token_revoked": ScannerErrorClass.AUTH,
    "account_inactive": ScannerErrorClass.AUTH,
    "missing_scope": ScannerErrorClass.MISSING_SCOPE,
    "no_permission": ScannerErrorClass.MISSING_SCOPE,
    "ratelimited": ScannerErrorClass.RATE_LIMIT,
    "rate_limited": ScannerErrorClass.RATE_LIMIT,
}


class SlackReadRefusedError(RuntimeError):
    """Slack answered a read with ``ok:false`` — never "there is nothing there"."""

    def __init__(self, method: str, subject: str, error_code: str) -> None:
        super().__init__(f"Slack refused {method} on {subject}: {error_code}. This is NOT an empty result.")
        self.method = method
        self.subject = subject
        self.error_code = error_code
