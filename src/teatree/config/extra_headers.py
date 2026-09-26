"""The only request headers ``openai_compatible_extra_headers`` may carry.

Config is stored, rendered and exported in cleartext, so a header outside this list is refused on
every write and withheld on every read; the endpoint's key travels only through the secret store.
"""

import logging

EXTRA_HEADERS_SETTING = "openai_compatible_extra_headers"

#: OrcaRouter's request headers: the float cost in ``usage`` the usage tee records, and per-run session affinity.
ALLOWED_EXTRA_HEADERS = ("X-OrcaRouter-Include-Cost", "X-OrcaRouter-Session-Id")

UNLISTED_HEADER_REFUSAL = (
    f"{EXTRA_HEADERS_SETTING} carries only {', '.join(ALLOWED_EXTRA_HEADERS)}; "
    "a credential belongs in the secret store, named by openai_compatible_credential_entry"
)

_ALLOWED_NAMES = frozenset(name.lower() for name in ALLOWED_EXTRA_HEADERS)

logger = logging.getLogger(__name__)


def carries_unlisted_header(key: str, value: object) -> bool:
    """Whether *value*, stored under *key*, is an extra-headers map naming a header outside the allowlist."""
    return (
        key == EXTRA_HEADERS_SETTING
        and isinstance(value, dict)
        and any(not isinstance(name, str) or name.lower() not in _ALLOWED_NAMES for name in value)
    )


def listed_headers(headers: dict[str, str]) -> dict[str, str]:
    """*headers* narrowed to the allowlist; what is withheld is logged by count, never by name or value."""
    listed = {name: value for name, value in headers.items() if name.lower() in _ALLOWED_NAMES}
    if withheld := len(headers) - len(listed):
        logger.warning("withheld %d stored header(s): %s", withheld, UNLISTED_HEADER_REFUSAL)
    return listed
