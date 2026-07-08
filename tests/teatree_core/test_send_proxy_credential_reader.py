"""Grep-gate: the send-proxy is the ONLY SEND-PATH reader of a posting credential (#117).

Every point-of-use secret-store read for a Slack bot/app/user token or a
GitHub/GitLab forge token *on the outbound send path* goes through
:func:`teatree.core.send_proxy.read_posting_credential`. Concretely, the two
messaging/forge backend CONSTRUCTORS the send chokepoints use —
``core/backend_factory.py`` and ``backends/loader.py`` — read posting tokens only
via the proxy, so the send-path credential surface is one auditable function.
This gate turns red if a posting-credential ``read_pass`` read is (re)introduced
in any send-path module.

Two categories are deliberately OUT of scope of a *send* proxy, and named in
:data:`_CREDENTIAL_MANAGEMENT_ALLOWLIST`:

* the ``t3 slack …`` credential-management CLI (setup / rotation / provisioning /
    socket-listener bootstrap) — it reads the bot/app/user token to VALIDATE,
    DERIVE, ROTATE, or CONNECT a listener, never to send. Folding that lifecycle
    into a send chokepoint would be the wrong component boundary.

Non-posting secrets (the Postgres password, the Figma token, a Slack user *id*,
the Slack app *config* token) are out of scope everywhere — they authorise no
outbound post — and legitimately read ``read_pass`` at their own point of use.
"""

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "teatree"
PROXY = SRC / "core" / "send_proxy.py"

#: The credential-MANAGEMENT surface (``t3 slack …``): setup, rotation,
#: provisioning, user-token setup, and the socket-listener bootstrap. These read
#: the bot/app/user posting token to configure/validate/connect, not to send, so
#: they are not the send-proxy's concern. Any read outside this set AND outside
#: the proxy is a send-path leak the gate refuses.
_CREDENTIAL_MANAGEMENT_ALLOWLIST = frozenset(
    {
        "cli/slack_listen.py",
        "cli/slack_setup.py",
        "cli/slack_app_resolve.py",
        "cli/slack_dm_provisioning.py",
        "cli/slack_provision.py",
        "cli/slack_user_token_setup.py",
    },
)

#: The suffix / config-key markers (matched case-insensitively) that name a Slack
#: or forge POSTING token — the ``xoxb``/``xoxp`` bot/app/user tokens and the
#: forge access tokens. Deliberately NOT the Slack *config* token
#: (``_CONFIG_TOKEN_REF``), the user *id* (``SLACK_USER_ID_PASS_KEY``), the
#: Postgres password, or the Figma token — none of those authorise an outbound
#: post, so they are not the send-proxy's concern.
_POSTING_MARKERS = (
    "-bot",
    "-app",
    "github_token",
    "gitlab_token",
    "slack_token",
    "user_token",
)

_READ_PASS_CALL = re.compile(r"read_pass\s*\(([^)]*)\)")


def test_read_posting_credential_is_defined_in_the_proxy() -> None:
    source = PROXY.read_text()
    assert "def read_posting_credential(" in source


def test_no_posting_credential_read_pass_outside_the_proxy() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC).as_posix()
        if path == PROXY or relative in _CREDENTIAL_MANAGEMENT_ALLOWLIST:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            for call in _READ_PASS_CALL.finditer(line):
                arg = call.group(1).lower()
                if any(marker in arg for marker in _POSTING_MARKERS):
                    offenders.append(f"{path.relative_to(SRC)}:{lineno}: {line.strip()}")
    detail = "\n".join(offenders)
    assert not offenders, f"posting-credential read_pass reads must route through the send-proxy:\n{detail}"


def test_backend_factory_reads_posting_tokens_only_via_the_proxy() -> None:
    # backend_factory was the sole posting-credential reader before #117; it must
    # now delegate entirely to the proxy and never call read_pass itself.
    source = (SRC / "core" / "backend_factory.py").read_text()
    assert "read_pass(" not in source
    assert "read_posting_credential(" in source
