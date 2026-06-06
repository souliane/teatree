"""Review CLI commands — GitLab draft note operations.

Every publishing method (``post_*`` / ``reply_*`` / ``resolve_*`` /
``publish_*`` / ``update_*`` / ``approve`` / ``unapprove`` /
``delete_discussion``) routes through the tri-state
``on_behalf_post_mode`` pre-gate (#960/#1013) the reply transport uses;
read-only methods (``list_draft_notes``, ``delete_draft_note``) bypass
it. Under IMMEDIATE the gate is off; under ASK every method is gated;
under DRAFT_OR_ASK (default) ``post_draft_note`` publishes autonomously
and the agent DMs the user with publish/delete commands, every other
method is gated identically to ASK.

``delete_discussion`` IS gated even though it is the deletion-shaped
sibling of ``delete_draft_note`` — it removes a *published* note that
colleagues can already see, so the removal itself is an on-behalf
colleague-visible mutation. Mirrors the ``update_note`` gating shape
exactly.

The gate is satisfiable without a TTY via a recorded
:class:`~teatree.core.models.on_behalf_approval.OnBehalfApproval`
scoped to ``(<repo>!<mr>, <method_name>)`` — the next matching
invocation publishes and consumes the row.
"""

from collections.abc import Callable
from http import HTTPStatus

import typer

from teatree.cli.review_approval import identity_has_reviewed
from teatree.cli.review_diff import find_added_line, resolve_inline_position
from teatree.cli.review_drafts import register as _register_drafts
from teatree.cli.review_evidence_gate import FindingEvidence, check_finding_evidence
from teatree.cli.review_on_behalf import check_on_behalf, on_behalf_gate_active, publish_on_behalf
from teatree.cli.review_on_behalf import register as _register_on_behalf
from teatree.cli.review_shape_gate import check_review_shape
from teatree.cli.review_todo_gate import InlineAnchor, check_todo_anchor
from teatree.utils.run import run_allowed_to_fail

# Re-exports — keep monkeypatch targets under the ``review`` namespace
# after extraction to :mod:`teatree.cli.review_diff` /
# :mod:`teatree.cli.review_on_behalf` for module-health LOC reasons.
# ``resolve_inline_position`` is re-exported here so the existing
# ``monkeypatch.setattr(review_mod, "resolve_inline_position", …)`` test
# pattern keeps working after the impl bodies moved to
# :mod:`teatree.cli.review_post_impl` (#1280).
_find_added_line = find_added_line
_on_behalf_gate_active = on_behalf_gate_active
_resolve_inline_position = resolve_inline_position

review_app = typer.Typer(no_args_is_help=True, help="Code review helpers.")
_TOKEN_PARTS_COUNT = 2


class ReviewService:
    """GitLab draft note operations for code review.

    Every method that publishes to an MR (post comment, post draft note,
    publish drafts, reply, resolve, update note, approve, unapprove,
    delete discussion) is wrapped by the recorded-approval on-behalf
    pre-gate. See module docstring for the full contract.
    """

    def __init__(self, token: str) -> None:
        self.token = token

    @staticmethod
    def get_gitlab_token() -> str:
        """Extract GitLab token from glab auth or GITLAB_TOKEN env var."""
        import os  # noqa: PLC0415

        token = os.environ.get("GITLAB_TOKEN", "")
        if token:
            return token
        result = run_allowed_to_fail(["glab", "auth", "status", "-t"], expected_codes=None)
        for line in result.stderr.splitlines():
            if "Token" in line and ":" in line:
                token_value = line.rsplit(":", 1)[-1].strip()
                if token_value:
                    return token_value
        return ""

    def _get_api(self):  # noqa: ANN202
        from teatree.backends.gitlab.api import GitLabAPI  # noqa: PLC0415

        return GitLabAPI(token=self.token, base_url=self._resolve_base_url())

    @staticmethod
    def _publish_or_blocked(
        repo: str,
        mr: int,
        action: str,
        body: Callable[[], tuple[str, int]],
    ) -> tuple[str, int]:
        """Run *body* (the GitLab post) atomically with the on-behalf consume + audit (#1879).

        ``check_on_behalf`` already peeked non-consuming; here the approval is
        consumed in the same ``transaction.atomic`` as the post, so a failed
        post rolls back the consume (no burn) and writes no lying audit. A
        BLOCK racing in after the peek is surfaced as ``(message, 1)``.
        """
        from teatree.core.on_behalf_gate_recorded import OnBehalfPostBlockedError  # noqa: PLC0415

        try:
            return publish_on_behalf(repo, mr, action, body)
        except OnBehalfPostBlockedError as blocked:
            return str(blocked), 1

    @staticmethod
    def _resolve_base_url() -> str:
        """Resolve GitLab API base URL from overlay config or env, defaulting to gitlab.com."""
        import os  # noqa: PLC0415

        try:
            from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

            return get_overlay().config.gitlab_url
        except Exception:  # noqa: BLE001
            return os.environ.get("GITLAB_URL", "https://gitlab.com/api/v4")

    def _post_draft_note_impl(self, repo: str, mr: int, note: str, *, file: str, line: int) -> tuple[str, int]:
        """The pre-gate-passed body of :meth:`post_draft_note` (extracted to :mod:`review_post_impl`)."""
        from teatree.cli.review_post_impl import post_draft_note_impl  # noqa: PLC0415

        return post_draft_note_impl(self, repo, mr, note, file=file, line=line)

    def post_draft_note(  # noqa: PLR0913 — public service method whose params map 1:1 to the ``t3 review post-draft-note`` CLI flags; ``evidence`` is the #1280 structured-evidence record and the ``allow_*`` overrides are the #126 documented escapes — all must stay kwargs on this surface.
        self,
        repo: str,
        mr: int,
        note: str,
        *,
        file: str = "",
        line: int = 0,
        evidence: FindingEvidence | None = None,
        allow_long_review: bool = False,
        allow_todo_blocker: bool = False,
    ) -> tuple[str, int]:
        """Post a draft note. Returns (message, exit_code).

        For inline notes (file+line), validates that the target line is an added
        (``+``) line in the MR diff, then verifies after posting that GitLab
        actually anchored the draft (``line_code`` non-null). Broken drafts
        (anchor refused, usually because the file diff is collapsed) are
        deleted and surfaced as an error so they cannot be published silently.

        Gated by the pre-publish chain in :meth:`_run_pre_publish_gates` —
        ``on_behalf_post_mode`` (#960), colleague-MR shape (#1114), TODO-anchor
        (#1186), and structured-evidence (#1280, requires ``evidence`` on
        ``missing/wrong/broken`` finding bodies). ``allow_long_review`` /
        ``allow_todo_blocker`` are the documented per-call escapes for the
        shape and TODO-anchor gates (#126).
        """
        refusal = self._run_pre_publish_gates(
            repo=repo,
            mr=mr,
            note=note,
            file=file,
            line=line,
            action="post_draft_note",
            evidence=evidence,
            allow_long_review=allow_long_review,
            allow_todo_blocker=allow_todo_blocker,
        )
        if refusal:
            return refusal, 1
        return self._publish_or_blocked(
            repo, mr, "post_draft_note", lambda: self._post_draft_note_impl(repo, mr, note, file=file, line=line)
        )

    def _run_pre_publish_gates(  # noqa: PLR0913
        self,
        *,
        repo: str,
        mr: int,
        note: str,
        file: str,
        line: int,
        action: str,
        evidence: FindingEvidence | None,
        allow_long_review: bool = False,
        allow_todo_blocker: bool = False,
    ) -> str:
        """Run on-behalf (#960) → shape (#1114) → TODO-anchor (#1186) → evidence (#1280); first refusal or ``""``.

        ``allow_long_review`` / ``allow_todo_blocker`` are the #126 documented
        escapes: each lets exactly one sibling gate proceed for a
        legitimately-authorized action. They never relax the on-behalf or
        evidence gates.
        """
        blocked = check_on_behalf(repo, mr, action)
        if blocked:
            return blocked
        encoded = repo.replace("/", "%2F")
        api = self._get_api()
        shape_error = check_review_shape(
            api=api,
            encoded_repo=encoded,
            mr=mr,
            body=note,
            inline=bool(file and line),
            allow_long_review=allow_long_review,
        )
        if shape_error:
            return shape_error
        todo_error = check_todo_anchor(
            api=api,
            encoded_repo=encoded,
            mr=mr,
            body=note,
            anchor=InlineAnchor(file=file, line=line),
            allow_todo_blocker=allow_todo_blocker,
        )
        if todo_error:
            return todo_error
        return check_finding_evidence(body=note, evidence=evidence)

    def _post_comment_impl(self, repo: str, mr: int, note: str, *, file: str, line: int) -> tuple[str, int]:
        """The pre-gate-passed body of :meth:`post_comment` (extracted to :mod:`review_post_impl`)."""
        from teatree.cli.review_post_impl import post_comment_impl  # noqa: PLC0415

        return post_comment_impl(self, repo, mr, note, file=file, line=line)

    def post_comment(  # noqa: PLR0913 — public service method whose params map 1:1 to the ``t3 review post-comment`` CLI flags; ``live`` (#1207 default-flip), ``evidence`` (#1280) and the ``allow_*`` escapes (#126) must stay kwargs on this surface.
        self,
        repo: str,
        mr: int,
        note: str,
        *,
        file: str = "",
        line: int = 0,
        live: bool = False,
        evidence: FindingEvidence | None = None,
        allow_long_review: bool = False,
        allow_todo_blocker: bool = False,
    ) -> tuple[str, int]:
        """Post an MR comment — DRAFT by default; ``--live`` needs a Slack-recorded LivePostApproval (#1207).

        Default path routes through :meth:`post_draft_note` (draft-form on-behalf carve-out).
        ``--live`` requires both a ``post_comment`` on-behalf approval and a LivePostApproval.

        Also gated by the structured-evidence pre-publish gate (#1280):
        when ``note`` matches an "X is missing/wrong/broken" pattern, the
        ``evidence`` kwarg must carry a verified
        :class:`~teatree.cli.review_evidence_gate.FindingEvidence` record.

        ``allow_long_review`` / ``allow_todo_blocker`` are the #126
        documented per-call escapes for the colleague-MR shape and the
        TODO-anchor gates respectively.
        """
        from teatree.cli.review_authorize import resolve_live_authorization  # noqa: PLC0415
        from teatree.cli.review_default_draft import check_live_post, notify_draft_created  # noqa: PLC0415

        if not live:
            msg, code = self.post_draft_note(
                repo,
                mr,
                note,
                file=file,
                line=line,
                evidence=evidence,
                allow_long_review=allow_long_review,
                allow_todo_blocker=allow_todo_blocker,
            )
            if code == 0:
                notify_draft_created(repo=repo, mr=mr, body=note, message=msg)
            return msg, code
        # One-step authorization gate (#126): a single ``t3 review authorize``
        # is the satisfier. Surface the unified refusal naming that one
        # command before the per-token chokepoints below would emit the old
        # two-command messages.
        live_refusal = resolve_live_authorization(scope=f"{repo}!{mr}", action="post_comment")
        if live_refusal:
            return live_refusal, 1
        refusal = self._run_pre_publish_gates(
            repo=repo,
            mr=mr,
            note=note,
            file=file,
            line=line,
            action="post_comment",
            evidence=evidence,
            allow_long_review=allow_long_review,
            allow_todo_blocker=allow_todo_blocker,
        )
        if refusal:
            return refusal, 1
        blocked_live = check_live_post(repo=repo, mr=mr)
        if blocked_live:
            return blocked_live, 1
        return self._publish_or_blocked(
            repo, mr, "post_comment", lambda: self._post_comment_impl(repo, mr, note, file=file, line=line)
        )

    def delete_draft_note(self, repo: str, mr: int, note_id: int) -> tuple[str, int]:
        """Delete a draft note. Returns (message, exit_code)."""
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        status = api.delete(f"projects/{encoded}/merge_requests/{mr}/draft_notes/{note_id}")
        if status == HTTPStatus.NO_CONTENT:
            return f"OK deleted draft_note_id={note_id}", 0
        return f"Failed: HTTP {status}", 1

    def publish_draft_notes(self, repo: str, mr: int) -> tuple[str, int]:
        """Bulk-publish every draft note on an MR.

        Gated by ``on_behalf_post_mode`` (#960, BLOCK under `ask` / `draft_or_ask`): the bulk publish is
        the moment drafts become visible to colleagues, so it routes
        through the same recorded-approval gate every other on-behalf
        post uses.
        """
        blocked = check_on_behalf(repo, mr, "publish_draft_notes")
        if blocked:
            return blocked, 1
        from teatree.cli.review_post_impl import publish_draft_notes_impl  # noqa: PLC0415

        encoded = repo.replace("/", "%2F")
        return self._publish_or_blocked(
            repo, mr, "publish_draft_notes", lambda: publish_draft_notes_impl(self, repo, mr, encoded=encoded)
        )

    def reply_to_discussion(self, repo: str, mr: int, discussion_id: str, body: str) -> tuple[str, int]:
        """Reply to an existing discussion thread on an MR. Returns (message, exit_code).

        Gated by ``on_behalf_post_mode`` (#960, BLOCK under `ask` / `draft_or_ask`): the reply is refused
        without any GitLab side effect when the gate is on and no recorded
        :class:`OnBehalfApproval` matches ``(<repo>!<mr>, "reply_to_discussion")``.
        """
        blocked = check_on_behalf(repo, mr, "reply_to_discussion")
        if blocked:
            return blocked, 1
        from teatree.cli.review_post_impl import reply_to_discussion_impl  # noqa: PLC0415

        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        # Reply bodies are always inline (anchored on the existing discussion's
        # diff position), so the inline cap applies.
        shape_error = check_review_shape(api=api, encoded_repo=encoded, mr=mr, body=body, inline=True)
        if shape_error:
            return shape_error, 1
        return self._publish_or_blocked(
            repo,
            mr,
            "reply_to_discussion",
            lambda: reply_to_discussion_impl(self, repo, mr, discussion_id, body, encoded=encoded),
        )

    def resolve_discussion(self, repo: str, mr: int, discussion_id: str, *, resolved: bool = True) -> tuple[str, int]:
        """Mark a discussion thread resolved or unresolved. Returns (message, exit_code).

        Gated by ``on_behalf_post_mode`` (#960, BLOCK under `ask` / `draft_or_ask`): a resolve flip is
        visible to colleagues (it closes the discussion under the user's
        identity), so it routes through the same recorded-approval gate.
        """
        blocked = check_on_behalf(repo, mr, "resolve_discussion")
        if blocked:
            return blocked, 1
        from teatree.cli.review_post_impl import resolve_discussion_impl  # noqa: PLC0415

        encoded = repo.replace("/", "%2F")
        return self._publish_or_blocked(
            repo,
            mr,
            "resolve_discussion",
            lambda: resolve_discussion_impl(self, repo, mr, discussion_id, resolved=resolved, encoded=encoded),
        )

    def update_note(self, repo: str, mr: int, note_id: int, body: str) -> tuple[str, int]:
        """Update a note (draft or published) on an MR.

        Tries draft-notes first; falls back to published-notes on 404.

        Gated by ``on_behalf_post_mode`` (#960, BLOCK under `ask` / `draft_or_ask`): an update to a
        *published* note is a colleague-visible edit; the gate covers
        both fallback paths uniformly so a published-note edit cannot
        slip through while a comment-create would be blocked.
        """
        blocked = check_on_behalf(repo, mr, "update_note")
        if blocked:
            return blocked, 1
        from teatree.cli.review_post_impl import update_note_impl  # noqa: PLC0415

        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        # Without diff coordinates here, treat the updated body as MR-level
        # prose — the tight cap applies. If the updated note is itself an
        # inline DiffNote the body will fit the inline cap too.
        shape_error = check_review_shape(api=api, encoded_repo=encoded, mr=mr, body=body, inline=False)
        if shape_error:
            return shape_error, 1
        return self._publish_or_blocked(
            repo, mr, "update_note", lambda: update_note_impl(self, repo, mr, note_id, body, encoded=encoded)
        )

    def delete_discussion(self, repo: str, mr: int, note_id: int) -> tuple[str, int]:
        """Delete a *published* note from an MR. Returns (message, exit_code).

        Use to clean up a published general discussion that should have
        been inline (or any other published note that needs removal).
        Distinct from :meth:`delete_draft_note`, which removes the user's
        own unpublished draft — that is not a colleague-visible mutation
        and stays ungated; this one is.

        Gated by ``ask_before_post_on_behalf`` (#960): the call is refused
        without any GitLab side effect when the gate is on and no recorded
        :class:`OnBehalfApproval` matches ``(<repo>!<mr>, "delete_discussion")``.
        """
        blocked = check_on_behalf(repo, mr, "delete_discussion")
        if blocked:
            return blocked, 1
        from teatree.cli.review_post_impl import delete_discussion_impl  # noqa: PLC0415

        encoded = repo.replace("/", "%2F")
        return self._publish_or_blocked(
            repo, mr, "delete_discussion", lambda: delete_discussion_impl(self, repo, mr, note_id, encoded=encoded)
        )

    def list_draft_notes(self, repo: str, mr: int) -> tuple[str, int]:
        """List draft notes. Returns (message, exit_code)."""
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        notes = api.get_json(f"projects/{encoded}/merge_requests/{mr}/draft_notes")
        if not isinstance(notes, list):
            return "No draft notes found", 0

        lines = []
        for n in notes:
            if not isinstance(n, dict):
                continue
            entry: dict[str, object] = n
            nid = entry.get("id")
            pos_raw = entry.get("position")
            pos = dict(pos_raw) if isinstance(pos_raw, dict) else {}
            fp = pos.get("new_path", "")
            ln = pos.get("new_line", "")
            body = str(entry.get("note", ""))[:60]
            lines.append(f"  {nid}  {fp}:{ln}  {body}...")
        return "\n".join(lines), 0

    def approve(self, repo: str, mr: int) -> tuple[str, int]:
        """Approve an MR — refuses unless the identity has already reviewed it.

        Returns (message, exit_code). The review-first precondition encodes
        the approve-on-review doctrine: an approval cannot be recorded
        without a prior reviewing footprint from the same identity.

        Gated by ``ask_before_post_on_behalf`` (#960/#1013): an approval is
        an outward post on the user's identity, so it routes through the
        same recorded-approval gate every other on-behalf method uses. Gate
        ON + no recorded :class:`OnBehalfApproval` matching
        ``(<repo>!<mr>, "approve")`` → refuse without any GitLab side
        effect; gate ON + recorded row → consume single-use and proceed.
        """
        blocked = check_on_behalf(repo, mr, "approve")
        if blocked:
            return blocked, 1
        encoded = repo.replace("/", "%2F")
        reviewed, error = identity_has_reviewed(self._get_api(), encoded, mr)
        if error:
            return error, 1
        if not reviewed:
            msg = (
                f"Refusing to approve !{mr}: review before approve — no review note authored by your "
                "identity exists on this MR yet. Post a review (`t3 review post-comment` / "
                "`post-draft-note`) first, then approve."
            )
            return msg, 1
        from teatree.cli.review_post_impl import approve_impl  # noqa: PLC0415

        return self._publish_or_blocked(repo, mr, "approve", lambda: approve_impl(self, repo, mr, encoded=encoded))

    def unapprove(self, repo: str, mr: int) -> tuple[str, int]:
        """Revoke this identity's approval on an MR. Returns (message, exit_code).

        No review-first precondition — removing an approval is the safe
        direction and must always be reachable.

        Gated by ``ask_before_post_on_behalf`` (#960/#1013): an unapproval
        is still a colleague-visible post on the user's identity, so it
        routes through the same recorded-approval gate as ``approve`` (and
        every other on-behalf method). The recorded row scopes to
        ``(<repo>!<mr>, "unapprove")``.
        """
        blocked = check_on_behalf(repo, mr, "unapprove")
        if blocked:
            return blocked, 1
        from teatree.cli.review_post_impl import unapprove_impl  # noqa: PLC0415

        encoded = repo.replace("/", "%2F")
        return self._publish_or_blocked(repo, mr, "unapprove", lambda: unapprove_impl(self, repo, mr, encoded=encoded))


# Register sibling-module typer commands. Kept out of this file so the
# OOP/LOC ceiling (`scripts/hooks/check_module_health.py`) stays
# satisfied — see `teatree.cli.review_on_behalf`,
# `teatree.cli.review_drafts`, `teatree.cli.review_live_approval`, and
# `teatree.cli.review_commands`.
from teatree.cli import review_commands as _review_commands  # noqa: E402 — registration side-effect
from teatree.cli.review_authorize import register as _register_authorize  # noqa: E402 — late, after typer app
from teatree.cli.review_commands import _require_token  # noqa: E402, F401 — re-exported for monkeypatch targets
from teatree.cli.review_live_approval import register as _register_live_approval  # noqa: E402 — late, after typer app
from teatree.cli.teatree_gate import register_fail_open_gate_commands as _register_fail_open  # noqa: E402

_register_on_behalf(review_app)
_register_drafts(review_app)
_register_live_approval(review_app)
_register_authorize(review_app)
_register_fail_open(review_app)
_ = _review_commands  # quiet "unused import" — module load is the side-effect
