"""The attachment-manifest engine — extraction, build/diff, gate, fetch (PR-15).

Extraction is pure regex over issue text; the build reconciles refs against the
on-disk cache (ground truth); the gate holds the planner hand-off while any
attachment is un-fetched and passes vacuously on a zero-attachment ticket; fetch
downloads through an injected seam. Each behaviour has a RED-without-the-code
assertion (a wrong kind, a missing refusal, a stale-flag pass would all fail).
"""

import tempfile
from pathlib import Path
from unittest import mock

import pytest
from django.test import TestCase

from teatree.core.attachment_manifest import (
    AttachmentFetchError,
    AttachmentKind,
    AttachmentRef,
    attachment_gate_refusal,
    attachments_dir_for,
    build_manifest,
    default_fetcher,
    extract_refs,
    fetch_manifest,
    local_path_for,
    ticket_text_sources,
    unfetched_entries,
)
from teatree.core.models import AttachmentManifest, Ticket

_GITLAB = "/uploads/" + "a" * 32 + "/spec.pdf"
_GITLAB_ABS = "https://gitlab.com/acme/app/uploads/" + "b" * 32 + "/design.png"
_NOTION = "https://www.notion.so/acme/Design-abc123"
_SLACK = "https://acme.slack.com/archives/C012/p1700000000000000"
_SLACK_FILE = "https://files.slack.com/files-pri/T01-F01/mock.png"


def _ticket(**extra: object) -> Ticket:
    return Ticket.objects.create(overlay="acme", role=Ticket.Role.AUTHOR, extra=dict(extra))


class TestExtractRefs:
    def test_classifies_each_source_kind(self) -> None:
        refs = extract_refs([f"see {_GITLAB} and {_NOTION} and {_SLACK} and {_SLACK_FILE}"])
        by_url = {ref.source_url: ref.kind for ref in refs}
        assert by_url[_GITLAB] is AttachmentKind.GITLAB_UPLOAD
        assert by_url[_NOTION] is AttachmentKind.NOTION
        assert by_url[_SLACK] is AttachmentKind.SLACK
        assert by_url[_SLACK_FILE] is AttachmentKind.SLACK

    def test_absolute_gitlab_upload_is_matched(self) -> None:
        refs = extract_refs([f"![design]({_GITLAB_ABS})"])
        assert [ref.source_url for ref in refs] == [_GITLAB_ABS]

    def test_dedups_repeated_url_across_texts(self) -> None:
        refs = extract_refs([f"body {_GITLAB}", f"comment {_GITLAB}"])
        assert [ref.source_url for ref in refs] == [_GITLAB]

    def test_no_attachment_text_yields_no_refs(self) -> None:
        assert extract_refs(["just prose, https://example.com/not-an-attachment"]) == []

    def test_bare_notion_domain_without_path_is_not_matched(self) -> None:
        # A plain domain mention with no page path is not an attachment ref.
        assert extract_refs(["notion.so"]) == []


class TestBuildManifest(TestCase):
    def test_unfetched_when_cache_absent(self) -> None:
        ticket = _ticket(branch="1-feat")
        att_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

        manifest = build_manifest(ticket, texts=[f"spec {_GITLAB}"], attachments_dir=att_dir)

        missing = unfetched_entries(manifest)
        assert [entry.source_url for entry in missing] == [_GITLAB]

    def test_fetched_when_cache_file_present(self) -> None:
        ticket = _ticket(branch="1-feat")
        att_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        ref = AttachmentRef(_GITLAB, AttachmentKind.GITLAB_UPLOAD)
        cached = local_path_for(att_dir, ref)
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(b"%PDF-1.4 fake")

        manifest = build_manifest(ticket, texts=[f"spec {_GITLAB}"], attachments_dir=att_dir)

        assert unfetched_entries(manifest) == []
        entry = next(iter(manifest.entries))
        assert entry["local_path"] == str(cached)
        assert entry["fetched_at"]

    def test_is_idempotent_when_unchanged(self) -> None:
        ticket = _ticket(branch="1-feat")
        att_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

        first = build_manifest(ticket, texts=[f"spec {_GITLAB}"], attachments_dir=att_dir)
        second = build_manifest(ticket, texts=[f"spec {_GITLAB}"], attachments_dir=att_dir)

        assert second.pk == first.pk
        assert AttachmentManifest.objects.filter(ticket=ticket).count() == 1

    def test_records_new_snapshot_when_set_changes(self) -> None:
        ticket = _ticket(branch="1-feat")
        att_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

        build_manifest(ticket, texts=[f"spec {_GITLAB}"], attachments_dir=att_dir)
        build_manifest(ticket, texts=[f"spec {_GITLAB} and {_NOTION}"], attachments_dir=att_dir)

        assert AttachmentManifest.objects.filter(ticket=ticket).count() == 2


class TestAttachmentGate(TestCase):
    def test_zero_attachment_ticket_passes_vacuously(self) -> None:
        ticket = _ticket(branch="1-feat")
        att_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

        refusal = attachment_gate_refusal(
            ticket,
            texts=["no attachments here"],
            attachments_dir=att_dir,
            fetch_command="t3 acme ticket attachments 1 --fetch",
        )

        assert refusal is None

    def test_unfetched_attachment_is_refused_with_urls_and_command(self) -> None:
        ticket = _ticket(branch="1-feat")
        att_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        cmd = "t3 acme ticket attachments 1 --fetch"

        refusal = attachment_gate_refusal(ticket, texts=[f"spec {_GITLAB}"], attachments_dir=att_dir, fetch_command=cmd)

        assert refusal is not None
        assert _GITLAB in refusal
        assert cmd in refusal

    def test_passes_once_the_attachment_is_fetched(self) -> None:
        ticket = _ticket(branch="1-feat")
        att_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        ref = AttachmentRef(_GITLAB, AttachmentKind.GITLAB_UPLOAD)
        cached = local_path_for(att_dir, ref)
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(b"fetched")

        refusal = attachment_gate_refusal(
            ticket, texts=[f"spec {_GITLAB}"], attachments_dir=att_dir, fetch_command="cmd"
        )

        assert refusal is None


class TestAttachmentsDirFor(TestCase):
    def test_keyed_on_branch(self) -> None:
        ticket = _ticket(branch="42-fix")
        assert attachments_dir_for(ticket, workspace=Path("/w")) == Path("/w/42-fix/.attachments")

    def test_falls_back_to_ticket_pk_without_branch(self) -> None:
        ticket = _ticket()
        assert attachments_dir_for(ticket, workspace=Path("/w")) == Path(f"/w/ticket-{ticket.pk}/.attachments")


class _FakeHost:
    def __init__(
        self,
        *,
        issue: dict[str, object] | None = None,
        comments: list[dict[str, object]] | None = None,
        raises: bool = False,
    ) -> None:
        self._issue = issue or {}
        self._comments = comments or []
        self._raises = raises

    def get_issue(self, issue_url: str) -> dict[str, object]:
        if self._raises:
            msg = "forge down"
            raise RuntimeError(msg)
        return self._issue

    def list_issue_comments(self, *, issue_url: str) -> list[dict[str, object]]:
        return self._comments


class TestTicketTextSources(TestCase):
    def test_reads_body_description_and_comments(self) -> None:
        ticket = _ticket()
        ticket.issue_url = "https://github.com/acme/app/issues/1"
        host = _FakeHost(
            issue={"body": "gh body", "description": "gl body"},
            comments=[{"body": "c1"}, {"note": "ignored"}, {"body": "c2"}],
        )

        texts = ticket_text_sources(ticket, code_host=host)

        assert texts == ["gh body", "gl body", "c1", "c2"]

    def test_no_backend_is_empty(self) -> None:
        ticket = _ticket()
        ticket.issue_url = "https://github.com/acme/app/issues/1"
        assert ticket_text_sources(ticket, code_host=None) == []

    def test_transport_error_fails_open_to_empty(self) -> None:
        ticket = _ticket()
        ticket.issue_url = "https://github.com/acme/app/issues/1"
        assert ticket_text_sources(ticket, code_host=_FakeHost(raises=True)) == []


class TestFetchManifest(TestCase):
    def test_successful_fetch_clears_the_gate(self) -> None:
        ticket = _ticket(branch="1-feat")
        att_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

        def _writer(ref: AttachmentRef, dest: Path) -> Path:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"downloaded")
            return dest

        refreshed, outcomes = fetch_manifest(
            ticket, texts=[f"spec {_GITLAB}"], attachments_dir=att_dir, fetcher=_writer
        )

        assert [o.ok for o in outcomes] == [True]
        assert unfetched_entries(refreshed) == []

    def test_failed_fetch_leaves_entry_unfetched(self) -> None:
        ticket = _ticket(branch="1-feat")
        att_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

        def _boom(ref: AttachmentRef, dest: Path) -> Path:
            msg = "no transport"
            raise AttachmentFetchError(msg)

        refreshed, outcomes = fetch_manifest(ticket, texts=[f"spec {_GITLAB}"], attachments_dir=att_dir, fetcher=_boom)

        assert [o.ok for o in outcomes] == [False]
        assert [e.source_url for e in unfetched_entries(refreshed)] == [_GITLAB]


class TestDefaultFetcher(TestCase):
    def test_dispatches_to_the_registered_fetcher(self) -> None:
        captured: list[str] = []

        def _fake(ref: AttachmentRef, dest: Path) -> Path:
            captured.append(ref.source_url)
            dest.write_bytes(b"downloaded")
            return dest

        self.enterContext(mock.patch("teatree.core.attachment_manifest.resolve_attachment_fetcher", return_value=_fake))
        att_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        ref = AttachmentRef(_NOTION, AttachmentKind.NOTION)

        result = default_fetcher(ref, att_dir / "out.png")

        assert captured == [_NOTION]
        assert result.exists()

    def test_unregistered_kind_raises_actionable_error(self) -> None:
        self.enterContext(mock.patch("teatree.core.attachment_manifest.resolve_attachment_fetcher", return_value=None))
        ref = AttachmentRef(_GITLAB, AttachmentKind.GITLAB_UPLOAD)
        with pytest.raises(AttachmentFetchError, match="no fetch transport registered"):
            default_fetcher(ref, Path("/tmp/x"))

    def test_unwired_hint_names_the_exact_gate_path_so_manual_placement_clears_it(self) -> None:
        # The hint must name the FULL deterministic cache path the gate checks
        # (local_path_for = <sha1>-<basename>), not its parent directory: a file
        # dropped under its natural basename would never clear the gate. Proving
        # both halves — the message carries the full dest, and a file placed at
        # that exact path releases the hold.
        self.enterContext(mock.patch("teatree.core.attachment_manifest.resolve_attachment_fetcher", return_value=None))
        ticket = _ticket(branch="1-feat")
        att_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        ref = AttachmentRef(_GITLAB, AttachmentKind.GITLAB_UPLOAD)
        dest = local_path_for(att_dir, ref)

        with pytest.raises(AttachmentFetchError) as exc_info:
            default_fetcher(ref, dest)
        assert str(dest) in str(exc_info.value)

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"manually placed")
        assert (
            attachment_gate_refusal(ticket, texts=[f"spec {_GITLAB}"], attachments_dir=att_dir, fetch_command="cmd")
            is None
        )
