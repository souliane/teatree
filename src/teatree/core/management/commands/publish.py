"""``t3 <overlay> publish`` — the one sanctioned write of a PR body, an issue body or a comment.

The text is scanned, written through the send-proxy, read back from the forge and scanned again; a text that
is not provably clean live exits 1. A closing keyword is refused unless ``--closes REF`` names that ref.
A PR body whose live text carries one ``Carries:`` line declares its merge round: the named member tickets freeze.
"""

from pathlib import Path
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.core.merge.carrier import parse_carries, record_published_round
from teatree.core.publishing.host import build_publish_host
from teatree.core.publishing.publish import PublishKind, PublishRefusedError, PublishRequest, publish_text

_TARGET = Annotated[str, typer.Argument(help="The PR or issue URL to write.")]
_BODY_FILE = Annotated[Path, typer.Option("--body-file", help="File holding the text to publish.")]
_CLOSES = Annotated[
    list[str] | None,
    typer.Option("--closes", help="A ref the text may close (#N, slug#N or an issue URL); repeatable."),
]


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """``t3 <overlay> publish`` group root."""

    @command(name="pr-body")
    def pr_body(self, target: _TARGET, body_file: _BODY_FILE, closes: _CLOSES = None) -> str:
        """Replace a PR body with the scanned, read-back text of BODY_FILE."""
        return self._publish(PublishKind.PR_BODY, target, body_file, closes)

    @command(name="issue-body")
    def issue_body(self, target: _TARGET, body_file: _BODY_FILE, closes: _CLOSES = None) -> str:
        """Replace an issue body with the scanned, read-back text of BODY_FILE."""
        return self._publish(PublishKind.ISSUE_BODY, target, body_file, closes)

    @command()
    def comment(self, target: _TARGET, body_file: _BODY_FILE, closes: _CLOSES = None) -> str:
        """Post BODY_FILE as a comment on a PR or issue, scanned before and after the write."""
        return self._publish(PublishKind.COMMENT, target, body_file, closes)

    def _publish(self, kind: PublishKind, target: str, body_file: Path, closes: list[str] | None) -> str:
        try:
            text = body_file.read_text(encoding="utf-8")
            request = PublishRequest(kind=kind, target=target, text=text, closes=tuple(closes or ()))
            result = publish_text(request, build_publish_host(target))
        except (OSError, PublishRefusedError) as exc:
            self.stderr.write(f"  refused: {exc}")
            raise SystemExit(1) from exc
        message = f"published {kind.value} to {target} ({len(result.text)} chars, read back and rescanned clean)"
        return message + (self._declare_round(target, result.text) if kind is PublishKind.PR_BODY else "")

    @staticmethod
    def _declare_round(carrier_url: str, live_text: str) -> str:
        member_urls = parse_carries(live_text)
        if record_published_round(carrier_url=carrier_url, member_urls=member_urls):
            return f"; froze {len(member_urls)} member PR(s)" if member_urls else ""
        return "; the Carries line names members but no ticket tracks this PR: nothing frozen" if member_urls else ""
