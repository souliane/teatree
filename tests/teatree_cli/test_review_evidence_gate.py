"""Structured-evidence pre-publish gate for review findings (souliane/teatree#1280).

When a reviewer posts a finding that asserts something "is missing", "is wrong",
"is broken", "does not exist", etc. via ``t3 review post-comment`` or
``post-draft-note``, the gate refuses the post unless a structured
:class:`teatree.cli.review.evidence_gate.FindingEvidence` record accompanies the
call. The evidence record carries the typed receipts a reviewer used to derive
the claim — file:line on master, ticket dependency refs, helper indirections
consulted, recent-merge sweep query, and a ``verified|speculative`` confidence
tag.

The gate refuses when:

* The body matches the missing/wrong/broken pattern, AND
* No evidence record is supplied, OR
* ``confidence == "speculative"``, OR
* Neither ``master_check_paths`` nor ``ticket_dep_refs`` carries at least one
    entry (a verified claim must have looked at master or have a documented
    upstream ticket — both empty = no evidence at all).

The gate is independent of the sibling gates (on-behalf, shape, TODO-anchor) —
all four run on every publishing call that takes a body.
"""

from pathlib import Path
from typing import Any, cast

import pytest

from teatree.cli.review import ReviewService
from teatree.cli.review.evidence_gate import FindingEvidence, check_finding_evidence, looks_like_evidence_claim
from teatree.config import OnBehalfPostMode

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _gate_immediate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin on-behalf gate to IMMEDIATE so it does not also block the call."""
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(
        f'[teatree]\non_behalf_post_mode = "{OnBehalfPostMode.IMMEDIATE.value}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)


def _disable_other_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the colleague-MR shape and TODO-anchor gates so refusals come from the evidence gate only.

    The gate chain lives in :mod:`teatree.cli.review.pre_publish_gates`, so
    the sibling gates are patched where the chain looks them up.
    """
    from teatree.cli.review import pre_publish_gates as gates_mod  # noqa: PLC0415

    monkeypatch.setattr(gates_mod, "check_review_shape", lambda **_kw: "")
    monkeypatch.setattr(gates_mod, "check_todo_anchor", lambda **_kw: "")


def _build_diff_with_added_line(target_line: int) -> str:
    """Build a unified diff with every line from ``target_line - 5`` to ``+5`` added.

    Mirrors the helper in :mod:`tests.teatree_cli.test_review_todo_gate` so the
    anchor line under test is reliably an added (``+``) line in the synthetic diff.
    """
    start = max(1, target_line - 5)
    end = target_line + 5
    lines = [f"@@ -{start},{end - start + 1} +{start},{end - start + 1} @@"]
    lines.extend(f"+    code_at_{ln}()" for ln in range(start, end + 1))
    return "\n".join(lines) + "\n"


class _StubAPI:
    """Stand-in for GitLabAPI — records calls and returns a passing-stub response."""

    def __init__(self, mr_author: str = "carol", target_line: int = 42) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self._mr_author = mr_author
        self._diff = _build_diff_with_added_line(target_line)

    def post_json(self, endpoint: str, payload: object) -> dict[str, object]:
        self.calls.append(("post_json", endpoint, payload))
        return {"id": 1, "notes": [{"type": "DiffNote"}], "web_url": "", "line_code": "abc_42_42"}

    def post_status(self, endpoint: str) -> int:
        self.calls.append(("post_status", endpoint, None))
        return 200

    def delete(self, endpoint: str) -> int:
        self.calls.append(("delete", endpoint, None))
        return 204

    def get_json(self, endpoint: str) -> object:
        self.calls.append(("get_json", endpoint, None))
        if "/changes" in endpoint:
            return {
                "changes": [
                    {"new_path": "x.py", "old_path": "x.py", "diff": self._diff},
                ],
            }
        if "/merge_requests/" in endpoint:
            return {
                "author": {"username": self._mr_author},
                "diff_refs": {"base_sha": "b", "head_sha": "h", "start_sha": "s"},
            }
        return {}

    def current_username(self) -> str:
        return "alice"


def _service_with_stub(stub: _StubAPI) -> ReviewService:
    service = ReviewService(token="t")
    service._get_api = lambda: stub  # type: ignore[method-assign]
    return service


# ---------------------------------------------------------------------------
# Unit tests on the schema and gate function (no Django, no API round-trips).
# ---------------------------------------------------------------------------


class TestLooksLikeEvidenceClaim:
    """The pattern detector flags 'X is missing/wrong/broken' shapes."""

    @pytest.mark.parametrize(
        "body",
        [
            "This helper is missing from the module.",
            "The enum value is missing.",
            "The function name is wrong here.",
            "API signature is wrong.",
            "Documentation is broken — references a removed symbol.",
            "This is broken: the import does not exist.",
            "The constant does not exist in the canonical list.",
            "Cannot find the helper anywhere in the codebase.",
            "There is no such function in master.",
            "This file should not exist.",
            "Stale reference to a removed module.",
        ],
    )
    def test_canonical_claim_phrases_are_flagged(self, body: str) -> None:
        assert looks_like_evidence_claim(body), f"must flag claim shape: {body!r}"

    @pytest.mark.parametrize(
        "body",
        [
            "Nit: prefer a shorter name.",
            "Tracked at #1234.",
            "LGTM.",
            "Consider renaming this for clarity.",
            "Why this choice over the sibling pattern?",
            "Nit: blank line above.",
        ],
    )
    def test_non_claim_phrases_are_not_flagged(self, body: str) -> None:
        assert not looks_like_evidence_claim(body), f"must NOT flag non-claim: {body!r}"


class TestCheckFindingEvidenceUnit:
    """Direct unit tests on ``check_finding_evidence`` — no ReviewService round-trip."""

    def test_non_claim_body_proceeds_without_evidence(self) -> None:
        """A body that does not match the claim pattern bypasses the gate entirely."""
        assert check_finding_evidence(body="Nit: rename for clarity.", evidence=None) == ""

    def test_claim_body_without_evidence_is_refused(self) -> None:
        """A 'missing/wrong/broken' claim without an evidence record is refused."""
        msg = check_finding_evidence(
            body="This helper is missing from the module.",
            evidence=None,
        )
        assert msg, "expected a refusal string"
        assert "evidence" in msg.lower()

    def test_claim_body_with_speculative_evidence_is_refused(self) -> None:
        """A claim tagged ``speculative`` is refused — speculative findings must drop entirely."""
        ev = FindingEvidence(
            master_check_paths=["src/foo.py"],
            ticket_dep_refs=["souliane/teatree#100"],
            helper_indirection_paths=[],
            recent_merge_sweep_query="",
            confidence="speculative",
        )
        msg = check_finding_evidence(
            body="This helper is missing from the module.",
            evidence=ev,
        )
        assert msg, "expected a refusal string"
        assert "speculative" in msg.lower()

    def test_claim_body_with_verified_but_empty_signals_is_refused(self) -> None:
        """Verified but both master_check_paths and ticket_dep_refs empty = no evidence at all."""
        ev = FindingEvidence(
            master_check_paths=[],
            ticket_dep_refs=[],
            helper_indirection_paths=["src/helper.py"],
            recent_merge_sweep_query="git log --grep=foo",
            confidence="verified",
        )
        msg = check_finding_evidence(
            body="The enum value is missing from the canonical list.",
            evidence=ev,
        )
        assert msg, "expected a refusal string"
        assert "master_check_paths" in msg or "ticket_dep_refs" in msg

    def test_claim_body_with_verified_master_check_is_accepted(self) -> None:
        """Verified + at least one master check path → proceed."""
        ev = FindingEvidence(
            master_check_paths=["src/foo.py:42"],
            ticket_dep_refs=[],
            helper_indirection_paths=[],
            recent_merge_sweep_query="",
            confidence="verified",
        )
        assert (
            check_finding_evidence(
                body="The helper is missing from src/foo.py.",
                evidence=ev,
            )
            == ""
        )

    def test_claim_body_with_verified_ticket_dep_is_accepted(self) -> None:
        """Verified + at least one ticket dependency reference → proceed."""
        ev = FindingEvidence(
            master_check_paths=[],
            ticket_dep_refs=["souliane/teatree#1234"],
            helper_indirection_paths=[],
            recent_merge_sweep_query="",
            confidence="verified",
        )
        assert (
            check_finding_evidence(
                body="The API signature is wrong here.",
                evidence=ev,
            )
            == ""
        )

    def test_empty_body_proceeds(self) -> None:
        """An empty body never trips the gate (other gates own the empty-body refusal)."""
        assert check_finding_evidence(body="", evidence=None) == ""


# ---------------------------------------------------------------------------
# Schema fields cover the common finding shapes documented in the issue.
# ---------------------------------------------------------------------------


class TestFindingEvidenceSchema:
    """The schema's fields cover the four common review-finding shapes."""

    def test_missing_file_evidence_shape(self) -> None:
        """Missing-file finding: master_check_paths names the master path that was looked at."""
        ev = FindingEvidence(
            master_check_paths=["src/teatree/cli/foo.py"],
            ticket_dep_refs=[],
            helper_indirection_paths=[],
            recent_merge_sweep_query="",
            confidence="verified",
        )
        assert ev.confidence == "verified"
        assert ev.master_check_paths == ["src/teatree/cli/foo.py"]

    def test_missing_function_evidence_shape(self) -> None:
        """Missing-function finding: master_check_paths names file:line of the search."""
        ev = FindingEvidence(
            master_check_paths=["src/teatree/cli/review/service.py:42-100"],
            ticket_dep_refs=[],
            helper_indirection_paths=[],
            recent_merge_sweep_query="",
            confidence="verified",
        )
        assert "src/teatree/cli/review/service.py:42-100" in ev.master_check_paths

    def test_wrong_api_signature_evidence_shape(self) -> None:
        """Wrong-API-signature finding: helper_indirection_paths shows where the canonical signature lives."""
        ev = FindingEvidence(
            master_check_paths=["src/teatree/backends/gitlab/api.py:200"],
            ticket_dep_refs=[],
            helper_indirection_paths=["src/teatree/cli/review/diff.py"],
            recent_merge_sweep_query="",
            confidence="verified",
        )
        assert ev.helper_indirection_paths == ["src/teatree/cli/review/diff.py"]

    def test_stale_doc_evidence_shape(self) -> None:
        """Stale-doc finding: ticket_dep_refs cites the ticket that landed the change."""
        ev = FindingEvidence(
            master_check_paths=["BLUEPRINT.md:1200"],
            ticket_dep_refs=["souliane/teatree#999"],
            helper_indirection_paths=[],
            recent_merge_sweep_query="git log --grep='(#999)' origin/main",
            confidence="verified",
        )
        assert ev.recent_merge_sweep_query.startswith("git log")
        assert ev.ticket_dep_refs == ["souliane/teatree#999"]


# ---------------------------------------------------------------------------
# Integration: ReviewService.post_draft_note / post_comment route through the gate.
# ---------------------------------------------------------------------------


class TestPostDraftNoteRefusesClaimWithoutEvidence:
    """ReviewService.post_draft_note refuses a 'missing/wrong/broken' body when no evidence is supplied."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_immediate(tmp_path, monkeypatch)
        _disable_other_gates(monkeypatch)

    def test_claim_body_without_evidence_refused(self) -> None:
        stub = _StubAPI()
        service = _service_with_stub(stub)

        msg, code = service.post_draft_note(
            "org/repo",
            7,
            "This helper is missing from src/foo.py.",
            file="x.py",
            line=42,
        )

        assert code == 1, f"claim-without-evidence must refuse: code={code} msg={msg!r}"
        assert "evidence" in msg.lower()
        assert not any(c[0] == "post_json" and "/draft_notes" in c[1] for c in stub.calls), (
            "no GitLab POST must fire when the gate refuses"
        )

    def test_claim_body_with_verified_evidence_publishes(self) -> None:
        stub = _StubAPI()
        service = _service_with_stub(stub)
        ev = FindingEvidence(
            master_check_paths=["src/foo.py:1-100"],
            ticket_dep_refs=[],
            helper_indirection_paths=[],
            recent_merge_sweep_query="",
            confidence="verified",
        )

        msg, code = service.post_draft_note(
            "org/repo",
            7,
            "This helper is missing from src/foo.py.",
            file="x.py",
            line=42,
            evidence=ev,
        )

        assert code == 0, f"claim with verified evidence must pass: code={code} msg={msg!r}"
        assert any(c[0] == "post_json" and "/draft_notes" in c[1] for c in stub.calls)

    def test_claim_body_with_speculative_evidence_refused(self) -> None:
        stub = _StubAPI()
        service = _service_with_stub(stub)
        ev = FindingEvidence(
            master_check_paths=["src/foo.py"],
            ticket_dep_refs=["souliane/teatree#100"],
            helper_indirection_paths=[],
            recent_merge_sweep_query="",
            confidence="speculative",
        )

        msg, code = service.post_draft_note(
            "org/repo",
            7,
            "This helper is missing from src/foo.py.",
            file="x.py",
            line=42,
            evidence=ev,
        )

        assert code == 1, f"speculative finding must drop: code={code} msg={msg!r}"
        assert "speculative" in msg.lower()
        assert not any(c[0] == "post_json" and "/draft_notes" in c[1] for c in stub.calls)


class TestPostDraftNoteAllowsNonClaimsWithoutEvidence:
    """A non-claim body (no missing/wrong/broken assertion) bypasses the evidence gate."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_immediate(tmp_path, monkeypatch)
        _disable_other_gates(monkeypatch)

    def test_nit_body_publishes_without_evidence(self) -> None:
        stub = _StubAPI()
        service = _service_with_stub(stub)

        msg, code = service.post_draft_note(
            "org/repo",
            7,
            "Nit: rename for clarity.",
            file="x.py",
            line=42,
        )

        assert code == 0, f"nit body must publish without evidence: code={code} msg={msg!r}"
        assert any(c[0] == "post_json" and "/draft_notes" in c[1] for c in stub.calls)


class TestPostCommentRoutesThroughEvidenceGate:
    """post_comment (default-draft path) also routes through the evidence gate."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_immediate(tmp_path, monkeypatch)
        _disable_other_gates(monkeypatch)

    def test_default_draft_path_refuses_claim_without_evidence(self) -> None:
        stub = _StubAPI()
        service = _service_with_stub(stub)

        msg, code = service.post_comment(
            "org/repo",
            7,
            "The API signature is wrong here.",
            file="x.py",
            line=42,
        )

        assert code == 1, f"default-draft path must refuse claim without evidence: code={code}"
        assert "evidence" in msg.lower()
        assert not any(c[0] == "post_json" and "/draft_notes" in c[1] for c in stub.calls)

    def test_default_draft_path_accepts_claim_with_verified_evidence(self) -> None:
        stub = _StubAPI()
        service = _service_with_stub(stub)
        ev = FindingEvidence(
            master_check_paths=["src/foo.py"],
            ticket_dep_refs=[],
            helper_indirection_paths=[],
            recent_merge_sweep_query="",
            confidence="verified",
        )

        msg, code = service.post_comment(
            "org/repo",
            7,
            "The API signature is wrong here.",
            file="x.py",
            line=42,
            evidence=ev,
        )

        assert code == 0, f"verified evidence must publish: code={code} msg={msg!r}"


# ---------------------------------------------------------------------------
# Vacuity guard: the RED test goes red on the PRE-fix code.
# ---------------------------------------------------------------------------


class TestGateIsAntiVacuous:
    """A claim-shaped body without evidence MUST be rejected.

    This is the canonical RED test for #1280 — it asserts the gate IS the
    enforcement, not a memory rule. Reverting :func:`check_finding_evidence`
    in :class:`ReviewService.post_draft_note` makes this test fail (claim
    publishes without evidence) — proving the gate guards the contract.
    """

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_immediate(tmp_path, monkeypatch)
        _disable_other_gates(monkeypatch)

    def test_unprotected_claim_post_is_blocked_with_actionable_message(self) -> None:
        stub = _StubAPI()
        service = _service_with_stub(stub)

        msg, code = service.post_draft_note(
            "org/repo",
            7,
            "The enum value is missing from the canonical list.",
            file="x.py",
            line=42,
        )

        assert code == 1
        assert "FindingEvidence" in msg, f"refusal must name the schema: {msg!r}"
        assert "confidence" in msg.lower() or "evidence" in msg.lower()
        assert "#1280" in msg or "evidence" in msg.lower()
        # Critical receipt: no POST hit the wire.
        post_calls = [c for c in stub.calls if c[0] == "post_json" and "/draft_notes" in c[1]]
        assert post_calls == [], f"the gate failed — POST leaked through: {post_calls!r}"


# ---------------------------------------------------------------------------
# Schema serialization — the agent passes evidence via JSON on the CLI surface.
# ---------------------------------------------------------------------------


class TestFindingEvidenceFromJSON:
    """The schema can be reconstructed from a JSON string (CLI plumbing path)."""

    def test_from_json_roundtrip(self) -> None:
        raw = (
            '{"master_check_paths": ["src/foo.py:42"],'
            ' "ticket_dep_refs": ["souliane/teatree#100"],'
            ' "helper_indirection_paths": [],'
            ' "recent_merge_sweep_query": "git log --grep=foo",'
            ' "confidence": "verified"}'
        )
        ev = FindingEvidence.from_json(raw)
        assert ev.master_check_paths == ["src/foo.py:42"]
        assert ev.ticket_dep_refs == ["souliane/teatree#100"]
        assert ev.helper_indirection_paths == []
        assert ev.recent_merge_sweep_query == "git log --grep=foo"
        assert ev.confidence == "verified"

    def test_from_json_rejects_unknown_confidence(self) -> None:
        raw = (
            '{"master_check_paths": [],'
            ' "ticket_dep_refs": [],'
            ' "helper_indirection_paths": [],'
            ' "recent_merge_sweep_query": "",'
            ' "confidence": "maybe"}'
        )
        with pytest.raises(ValueError, match="confidence"):
            FindingEvidence.from_json(raw)

    def test_from_json_rejects_malformed(self) -> None:
        with pytest.raises(ValueError, match="json"):
            FindingEvidence.from_json("not json at all")


# ---------------------------------------------------------------------------
# The gate runs independently of the on-behalf, shape, and TODO-anchor gates.
# ---------------------------------------------------------------------------


class TestEvidenceGateOrdering:
    """The evidence gate runs after on-behalf but before the API call — same shape as the sibling gates."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_immediate(tmp_path, monkeypatch)
        _disable_other_gates(monkeypatch)

    def test_no_api_get_when_evidence_gate_refuses(self) -> None:
        """The gate short-circuits — no GitLab GET fires when the body is a claim without evidence."""
        stub = _StubAPI()
        service = _service_with_stub(stub)

        _msg, code = service.post_draft_note(
            "org/repo",
            7,
            "The helper does not exist in master.",
            file="x.py",
            line=42,
        )

        assert code == 1
        # No POST fires.
        assert not any(c[0] == "post_json" for c in stub.calls)


class TestCheckFindingEvidenceFromCast:
    """The gate accepts ``Any``-typed evidence (CLI passes dict; from_json mints dataclass)."""

    def test_dict_shaped_evidence_via_from_json(self) -> None:
        ev = FindingEvidence.from_json(
            '{"master_check_paths": ["src/x.py"], "ticket_dep_refs": [],'
            ' "helper_indirection_paths": [], "recent_merge_sweep_query": "",'
            ' "confidence": "verified"}'
        )
        msg = check_finding_evidence(
            body="The helper is missing from src/x.py.",
            evidence=cast("Any", ev),
        )
        assert msg == ""
