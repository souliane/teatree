"""Every forge writer reaches the one normalized send-proxy destination seam."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from teatree.core import send_proxy
from teatree.core.reply_transport import _route_reply

_SRC = Path(__file__).parents[2] / "src" / "teatree"
_EXPECTED_WRITERS = {
    "cli/review/send_routing.py",
    "core/answering/work_item_filing.py",
    "core/issue_hygiene.py",
    "core/management/commands/_test_plan/mr_post.py",
    "core/management/commands/ticket.py",
    "core/review/verdict_findings_publish.py",
    "loops/dream/umbrella_ledger.py",
}
_DIRECT_SEND_REQUEST_CALLERS = {
    "core/notify.py",
    "core/on_behalf_egress.py",
    "core/reply_transport.py",
}


def test_every_route_forge_write_caller_is_accounted_for() -> None:
    callers = {
        path.relative_to(_SRC).as_posix()
        for path in _SRC.rglob("*.py")
        if path.name != "send_proxy.py"
        and any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "route_forge_write"
            for node in ast.walk(ast.parse(path.read_text()))
        )
    }
    assert callers == _EXPECTED_WRITERS


def test_every_direct_send_request_caller_is_accounted_for() -> None:
    callers = {
        path.relative_to(_SRC).as_posix()
        for path in _SRC.rglob("*.py")
        if path.name != "send_proxy.py"
        and any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "SendRequest"
            for node in ast.walk(ast.parse(path.read_text()))
        )
    }
    assert callers == _DIRECT_SEND_REQUEST_CALLERS


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        ("owner/repo", "github:owner/repo"),
        ("github.com/owner/repo", "github:owner/repo"),
        ("https://github.com/owner/repo/issues/4", "github:owner/repo"),
    ],
)
def test_route_forge_write_audits_channel_qualified_repo(
    monkeypatch: pytest.MonkeyPatch, repo: str, expected: str
) -> None:
    seen = []
    monkeypatch.setattr(send_proxy, "scan_outbound_text", lambda **_kwargs: SimpleNamespace(refused=False))
    monkeypatch.setattr(
        send_proxy,
        "route_send",
        lambda request: (
            seen.append(request) or send_proxy.SendVerdict(allowed=True, payload=request.payload, allowlist_ok=True)
        ),
    )
    assert send_proxy.route_forge_write(forge="github", repo=repo, text="safe", action="test", target="x") == "safe"
    assert seen[0].destination == expected


@pytest.mark.parametrize("repo", ["", "https://github.com/owner", "owner"])
def test_route_forge_write_refuses_empty_or_non_repo_destination(repo: str) -> None:
    with pytest.raises(send_proxy.OutboundBlockedError, match="owner/repo"):
        send_proxy.route_forge_write(forge="github", repo=repo, text="safe", action="test", target="x")


@pytest.mark.parametrize(
    ("source", "ref", "expected"),
    [
        ("github", "https://github.com/owner/repo/issues/4", "github:owner/repo"),
        ("gitlab", "group/repo", "gitlab:group/repo"),
    ],
)
def test_reply_transport_audits_qualified_repo(
    monkeypatch: pytest.MonkeyPatch, source: str, ref: str, expected: str
) -> None:
    seen = []
    monkeypatch.setattr(
        "teatree.core.reply_transport.route_send",
        lambda request: (
            seen.append(request) or send_proxy.SendVerdict(allowed=True, payload=request.payload, allowlist_ok=True)
        ),
    )
    spec = SimpleNamespace(
        event=SimpleNamespace(source=source, channel_ref=ref), body="safe", action_name="reply", target_ref="x"
    )
    assert _route_reply(spec) == "safe"
    assert seen[0].destination == expected
