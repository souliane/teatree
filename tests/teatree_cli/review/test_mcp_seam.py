"""The MCP review-post seam: bound to the target repo, and carrying the finding intact.

Two properties, both invisible when broken.

``register_review_post_seam`` takes a factory of the TARGET REPO, so the seam is
built per repo rather than once per process, and that repo is the overlay-resolution
context for both the credential and the base URL. Binding it per repo is what keeps
the MCP write tools working on a multi-overlay install, where an ambient resolution
empties the credential and refuses the base-URL read (souliane/teatree#1814,
refined upstream as #3793/#3794).

And :class:`GatedReviewPoster` is the ONLY code turning a :class:`SeamNote` into the
service's ``file=`` / ``line=`` / ``evidence=`` arguments. A translation that drops
what it was handed raises nothing and posts something: the anchor becomes an MR-wide
note, and ``inline = bool(file and line)`` flips, so the general-note and inline-shape
gates take their other branch. Nothing downstream can tell that apart from a caller
who genuinely asked for a general note — which is why each of the three translations
is pinned here rather than at the tool surface, where the seam is patched out.
"""

from typing import ClassVar

import pytest
from asgiref.sync import async_to_sync

from teatree.cli.review.batch_post import InlineNote
from teatree.cli.review.evidence_gate import FindingEvidence, check_finding_evidence
from teatree.cli.review.mcp_seam import _finding, register
from teatree.cli.review.service import ReviewService
from teatree.mcp import review_seam
from teatree.mcp.review_seam import SeamNote
from teatree.mcp.review_write_tools import _review_post_comment, _seam_note

_EVIDENCE_JSON = '{"master_check_paths": ["src/a.py:42"], "confidence": "verified"}'


@pytest.fixture(autouse=True)
def registered_seam() -> None:
    """Install the real seam factory (``teatree.cli`` does this at import time)."""
    register()


@pytest.mark.usefixtures("two_overlays", "no_glab_login")
class TestSeamBindsTheServiceToTheTargetRepo:
    def test_post_comment_builds_a_service_scoped_to_the_posted_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def _record(self: ReviewService, repo: str, mr: int, note: str, **kwargs: object) -> tuple[str, int]:
            captured.update(token=self.token, bound_repo=self.repo, posted_repo=repo, mr=mr, note=note, **kwargs)
            return "OK note_id=1", 0

        monkeypatch.setattr(ReviewService, "post_comment", _record)
        seam = review_seam.review_post_seam("acme/alpha")
        message, code = seam.post_comment("acme/alpha", 7, SeamNote(note="blocker: bug"), live=True)

        assert (message, code) == ("OK note_id=1", 0)
        assert captured == {
            "token": "glpat-ALPHA",
            "bound_repo": "acme/alpha",
            "posted_repo": "acme/alpha",
            "mr": 7,
            "note": "blocker: bug",
            "file": "",
            "line": 0,
            "live": True,
            "evidence": None,
            "force_general": False,
            "allow_bloat": False,
        }

    def test_post_draft_note_builds_a_service_scoped_to_the_posted_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def _record(self: ReviewService, repo: str, mr: int, note: str, **kwargs: object) -> tuple[str, int]:
            del kwargs
            captured.update(token=self.token, bound_repo=self.repo, posted_repo=repo, mr=mr, note=note)
            return "OK draft_note_id=9", 0

        monkeypatch.setattr(ReviewService, "post_draft_note", _record)
        message, code = review_seam.review_post_seam("acme/alpha").post_draft_note(
            "acme/alpha", 7, SeamNote(note="nit: naming")
        )

        assert (message, code) == ("OK draft_note_id=9", 0)
        assert captured["token"] == "glpat-ALPHA"
        assert captured["bound_repo"] == "acme/alpha"

    def test_two_posts_to_different_repos_each_get_their_own_overlay_credential(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, str]] = []

        def _record(self: ReviewService, repo: str, mr: int, note: str, **kwargs: object) -> tuple[str, int]:
            del mr, note, kwargs
            seen.append((repo, self.token))
            return "OK draft_note_id=9", 0

        monkeypatch.setattr(ReviewService, "post_draft_note", _record)
        review_seam.review_post_seam("acme/alpha").post_draft_note("acme/alpha", 1, SeamNote(note="nit: naming"))
        review_seam.review_post_seam("acme/bravo").post_draft_note("acme/bravo", 2, SeamNote(note="nit: naming"))

        # ``bravo`` holds no credential, so its posts carry none — the point is that
        # each call reads from ITS repo's overlay, not from one process-wide guess.
        assert seen == [("acme/alpha", "glpat-ALPHA"), ("acme/bravo", "")]


@pytest.mark.usefixtures("two_overlays", "no_glab_login")
class TestTheSeamCarriesTheAnchorToTheService:
    """Each of the three translations, pinned against silently posting a general note."""

    def test_post_comment_lands_the_anchor_as_file_and_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def _record(self: ReviewService, repo: str, mr: int, note: str, **kwargs: object) -> tuple[str, int]:
            del self, repo, mr, note
            captured.update(kwargs)
            return "OK note_id=1", 0

        monkeypatch.setattr(ReviewService, "post_comment", _record)
        review_seam.review_post_seam("acme/alpha").post_comment(
            "acme/alpha", 7, SeamNote(note="blocker: bug", anchor=("src/a.py", 42)), live=True
        )

        assert (captured["file"], captured["line"]) == ("src/a.py", 42)

    def test_post_draft_note_lands_the_anchor_as_file_and_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def _record(self: ReviewService, repo: str, mr: int, note: str, **kwargs: object) -> tuple[str, int]:
            del self, repo, mr, note
            captured.update(kwargs)
            return "OK draft_note_id=9", 0

        monkeypatch.setattr(ReviewService, "post_draft_note", _record)
        review_seam.review_post_seam("acme/alpha").post_draft_note(
            "acme/alpha", 7, SeamNote(note="nit: naming", anchor=("src/b.py", 3))
        )

        assert (captured["file"], captured["line"]) == ("src/b.py", 3)

    def test_the_batch_lands_every_anchor_as_file_and_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[InlineNote] = []

        def _record(
            self: ReviewService, repo: str, mr: int, notes: list[InlineNote], **kwargs: object
        ) -> tuple[str, int]:
            del self, repo, mr, kwargs
            captured.extend(notes)
            return "OK posted 2 comment(s)", 0

        monkeypatch.setattr(ReviewService, "post_comments", _record)
        review_seam.review_post_seam("acme/alpha").post_comments(
            "acme/alpha",
            7,
            [
                SeamNote(note="blocker: the retry is gone", anchor=("src/a.py", 42)),
                SeamNote(note="verdict: one blocker"),
            ],
            live=True,
        )

        assert [(item.file, item.line) for item in captured] == [("src/a.py", 42), ("", 0)]


@pytest.mark.usefixtures("two_overlays", "no_glab_login")
class TestTheSeamCarriesTheEvidenceToTheService:
    """The #1280 record has to reach the gate, or the batch cannot post a "broken" finding at all."""

    def test_post_comment_parses_the_evidence_json_into_the_record(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def _record(self: ReviewService, repo: str, mr: int, note: str, **kwargs: object) -> tuple[str, int]:
            del self, repo, mr, note
            captured.update(kwargs)
            return "OK note_id=1", 0

        monkeypatch.setattr(ReviewService, "post_comment", _record)
        review_seam.review_post_seam("acme/alpha").post_comment(
            "acme/alpha",
            7,
            SeamNote(note="the retry is missing here", anchor=("src/a.py", 42), evidence_json=_EVIDENCE_JSON),
            live=True,
        )

        assert captured["evidence"] == FindingEvidence(master_check_paths=["src/a.py:42"], confidence="verified")

    def test_the_batch_carries_each_findings_own_evidence_and_escapes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[InlineNote] = []

        def _record(
            self: ReviewService, repo: str, mr: int, notes: list[InlineNote], **kwargs: object
        ) -> tuple[str, int]:
            del self, repo, mr, kwargs
            captured.extend(notes)
            return "OK posted 2 comment(s)", 0

        monkeypatch.setattr(ReviewService, "post_comments", _record)
        review_seam.review_post_seam("acme/alpha").post_comments(
            "acme/alpha",
            7,
            [
                SeamNote(note="the retry is missing here", anchor=("src/a.py", 42), evidence_json=_EVIDENCE_JSON),
                SeamNote(note="Nit: rename this helper.", anchor=("src/b.py", 3), allow_bloat=True),
            ],
            live=True,
        )

        assert captured[0].evidence == FindingEvidence(master_check_paths=["src/a.py:42"], confidence="verified")
        assert captured[1].evidence is None
        assert [item.allow_bloat for item in captured] == [False, True]

    def test_malformed_evidence_json_is_refused_before_the_service(self, monkeypatch: pytest.MonkeyPatch) -> None:
        posted: list[str] = []
        monkeypatch.setattr(ReviewService, "post_comment", lambda *_args, **_kwargs: (posted.append("x"), ("OK", 0))[1])

        message, code = review_seam.review_post_seam("acme/alpha").post_comment(
            "acme/alpha", 7, SeamNote(note="the retry is missing here", evidence_json="{not json")
        )

        assert code == 2
        assert "invalid json" in message
        assert posted == []


@pytest.mark.usefixtures("two_overlays", "no_glab_login")
class TestAMappingEvidencePostsWhereItPreviouslyRefused:
    """The commonest finding a review has to post, in the shape a model naturally sends it.

    `evidence` sits beside `note` and `anchor` in a mapping, so a model passes a mapping.
    Stringified with `str()` it became a Python repr the single parser rejects, and the
    whole finding refused with code 2 -- before any network call, so nothing was posted
    and nothing said why the shape was wrong.
    """

    _BODY = "HIGH (correctness): the retry is missing from the new path."
    _MAPPING: ClassVar[dict[str, object]] = {
        "master_check_paths": ["src/teatree/cli/review/service.py:42"],
        "confidence": "verified",
    }

    def test_a_mapping_evidence_reaches_the_service_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def _record(self: ReviewService, repo: str, mr: int, note: str, **kwargs: object) -> tuple[str, int]:
            del self, repo, mr, note
            captured.update(kwargs)
            return "OK note_id=1", 0

        monkeypatch.setattr(ReviewService, "post_comment", _record)
        result = async_to_sync(_review_post_comment)(
            "acme/alpha",
            7,
            {"note": self._BODY, "anchor": "src/a.py:42", "evidence": self._MAPPING},
        )

        assert result == {"message": "OK note_id=1", "code": 0}
        assert captured["evidence"] == FindingEvidence(
            master_check_paths=["src/teatree/cli/review/service.py:42"], confidence="verified"
        )

    def test_the_mapping_satisfies_the_evidence_gate_the_bare_body_still_fails(self) -> None:
        built = _seam_note({"note": self._BODY, "evidence": self._MAPPING})
        assert isinstance(built, SeamNote)
        finding, refusal = _finding(built)

        assert refusal == ""
        assert finding is not None
        assert check_finding_evidence(body=self._BODY, evidence=finding.evidence) == ""
        assert check_finding_evidence(body=self._BODY, evidence=None) != ""
