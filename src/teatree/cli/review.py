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

from http import HTTPStatus

import typer

from teatree.cli.review_approval import identity_has_reviewed, identity_in_approved_by
from teatree.cli.review_audit import ReviewAfterReceipt, notify_review_after_receipt, record_note_claim
from teatree.cli.review_diff import find_added_line, resolve_inline_position
from teatree.cli.review_drafts import register as _register_drafts
from teatree.cli.review_on_behalf import check_on_behalf, on_behalf_gate_active
from teatree.cli.review_on_behalf import register as _register_on_behalf
from teatree.cli.review_shape_gate import check_review_shape
from teatree.cli.review_todo_gate import InlineAnchor, check_todo_anchor
from teatree.utils.run import run_allowed_to_fail

# Re-exports — keep monkeypatch targets under the ``review`` namespace
# after extraction to :mod:`teatree.cli.review_diff` /
# :mod:`teatree.cli.review_on_behalf` for module-health LOC reasons.
_find_added_line = find_added_line
_on_behalf_gate_active = on_behalf_gate_active

review_app = typer.Typer(no_args_is_help=True, help="Code review helpers.")
_TOKEN_PARTS_COUNT = 2
_HTTP_OK_CODES = frozenset({HTTPStatus.OK, HTTPStatus.CREATED, HTTPStatus.NO_CONTENT})


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
        from teatree.backends.gitlab_api import GitLabAPI  # noqa: PLC0415

        return GitLabAPI(token=self.token, base_url=self._resolve_base_url())

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
        """The pre-gate-passed body of :meth:`post_draft_note` (see docstring)."""
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        endpoint = f"projects/{encoded}/merge_requests/{mr}/draft_notes"

        if not (file and line):
            result = api.post_json(endpoint, {"note": note})
            if not result:
                return "Failed to post draft note", 1
            note_id = dict(result).get("id")
            record_note_claim(self._resolve_base_url, repo, mr, note_id, endpoint="draft_notes")
            return f"OK draft_note_id={note_id}", 0

        position, error = resolve_inline_position(api, encoded, mr, file, line)
        if position is None:
            return error, 1

        result = api.post_json(endpoint, {"note": note, "position": position})
        if not result:
            return "Failed to post draft note", 1
        result_dict = dict(result) if isinstance(result, dict) else {}
        note_id = result_dict.get("id")
        line_code = result_dict.get("line_code")
        if line_code:
            record_note_claim(self._resolve_base_url, repo, mr, note_id, endpoint="draft_notes", file=file, line=line)
            return f"OK draft_note_id={note_id}\nline_code={line_code}", 0

        if isinstance(note_id, int):
            api.delete(f"{endpoint}/{note_id}")
        return (
            f"GitLab refused to anchor the draft on {file}:{line} (line_code came back null). "
            "This usually means the file diff is collapsed because of its size; the draft_notes "
            "API cannot anchor on collapsed-diff files. Workaround: "
            f"`t3 review post-comment {repo} {mr} ... --file {file} --line {line}` "
            "(creates an immediate non-draft inline discussion)."
        ), 1

    def post_draft_note(self, repo: str, mr: int, note: str, *, file: str = "", line: int = 0) -> tuple[str, int]:
        """Post a draft note. Returns (message, exit_code).

        For inline notes (file+line), validates that the target line is an added
        (``+``) line in the MR diff, then verifies after posting that GitLab
        actually anchored the draft (``line_code`` non-null). Broken drafts
        (anchor refused, usually because the file diff is collapsed) are
        deleted and surfaced as an error so they cannot be published silently.

        Gated by ``on_behalf_post_mode`` (#960). Under
        :attr:`~teatree.config.OnBehalfPostMode.IMMEDIATE` posts directly.
        Under :attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK` (the
        new default) posts the draft autonomously and DMs the user with
        the publish/delete commands — drafts are colleague-invisible and
        revocable, so the post proceeds without a recorded approval.
        Under :attr:`~teatree.config.OnBehalfPostMode.ASK` the call is
        refused without any GitLab side effect when no recorded
        :class:`OnBehalfApproval` matches
        ``(<repo>!<mr>, "post_draft_note")``.
        """
        blocked = check_on_behalf(repo, mr, "post_draft_note")
        if blocked:
            return blocked, 1
        encoded = repo.replace("/", "%2F")
        api = self._get_api()
        shape_error = check_review_shape(api=api, encoded_repo=encoded, mr=mr, body=note, inline=bool(file and line))
        if shape_error:
            return shape_error, 1
        todo_error = check_todo_anchor(
            api=api, encoded_repo=encoded, mr=mr, body=note, anchor=InlineAnchor(file=file, line=line)
        )
        if todo_error:
            return todo_error, 1
        return self._post_draft_note_impl(repo, mr, note, file=file, line=line)

    def _post_comment_impl(
        self,
        repo: str,
        mr: int,
        note: str,
        *,
        file: str,
        line: int,
    ) -> tuple[str, int]:
        """The pre-gate-passed body of :meth:`post_comment` (see docstring)."""
        api = self._get_api()
        encoded = repo.replace("/", "%2F")

        if not (file and line):
            result = api.post_json(f"projects/{encoded}/merge_requests/{mr}/notes", {"body": note})
            if not result:
                return "Failed to post comment", 1
            result_dict = dict(result) if isinstance(result, dict) else {}
            note_id = result_dict.get("id")
            record_note_claim(self._resolve_base_url, repo, mr, note_id, endpoint="notes")
            notify_review_after_receipt(
                self._resolve_base_url,
                repo,
                mr,
                review_action=ReviewAfterReceipt(
                    action="post_comment",
                    summary=f"posted comment note_id={note_id} on {repo}!{mr}",
                    note_web_url=str(result_dict.get("web_url", "")),
                ),
            )
            return f"OK note_id={note_id}", 0

        position, error = resolve_inline_position(api, encoded, mr, file, line)
        if position is None:
            return error, 1

        result = api.post_json(
            f"projects/{encoded}/merge_requests/{mr}/discussions",
            {"body": note, "position": position},
        )
        if not result:
            return "Failed to post comment", 1
        result_dict = dict(result) if isinstance(result, dict) else {}
        discussion_id = result_dict.get("id")
        notes = result_dict.get("notes")
        first_note = notes[0] if isinstance(notes, list) and notes else {}
        note_type = first_note.get("type") if isinstance(first_note, dict) else None
        if note_type != "DiffNote":
            return f"Comment posted but not anchored inline (type={note_type!r}). discussion_id={discussion_id}", 1
        record_note_claim(self._resolve_base_url, repo, mr, discussion_id, endpoint="discussions", file=file, line=line)
        notify_review_after_receipt(
            self._resolve_base_url,
            repo,
            mr,
            review_action=ReviewAfterReceipt(
                action="post_comment",
                summary=f"posted inline comment discussion_id={discussion_id} on {repo}!{mr}",
                note_web_url=str(first_note.get("web_url", "")) if isinstance(first_note, dict) else "",
            ),
        )
        return f"OK discussion_id={discussion_id} (inline DiffNote)", 0

    def post_comment(  # noqa: PLR0913 — public service method whose params map 1:1 to the ``t3 review post-comment`` CLI flags; ``live`` is the load-bearing #1207 default-flip and must stay a kwarg on this surface.
        self,
        repo: str,
        mr: int,
        note: str,
        *,
        file: str = "",
        line: int = 0,
        live: bool = False,
    ) -> tuple[str, int]:
        """Post an MR comment — DRAFT by default; ``--live`` needs a Slack-recorded LivePostApproval (#1207).

        Default path routes through :meth:`post_draft_note` (draft-form on-behalf carve-out).
        ``--live`` requires both a ``post_comment`` on-behalf approval and a LivePostApproval.
        """
        from teatree.cli.review_default_draft import check_live_post, notify_draft_created  # noqa: PLC0415

        if not live:
            msg, code = self.post_draft_note(repo, mr, note, file=file, line=line)
            if code == 0:
                notify_draft_created(repo=repo, mr=mr, body=note, message=msg)
            return msg, code
        blocked = check_on_behalf(repo, mr, "post_comment")
        if blocked:
            return blocked, 1
        encoded = repo.replace("/", "%2F")
        api = self._get_api()
        shape_error = check_review_shape(api=api, encoded_repo=encoded, mr=mr, body=note, inline=bool(file and line))
        if shape_error:
            return shape_error, 1
        todo_error = check_todo_anchor(
            api=api, encoded_repo=encoded, mr=mr, body=note, anchor=InlineAnchor(file=file, line=line)
        )
        if todo_error:
            return todo_error, 1
        blocked_live = check_live_post(repo=repo, mr=mr)
        if blocked_live:
            return blocked_live, 1
        return self._post_comment_impl(repo, mr, note, file=file, line=line)

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
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        status = api.post_status(f"projects/{encoded}/merge_requests/{mr}/draft_notes/bulk_publish")
        if status in {HTTPStatus.OK, HTTPStatus.NO_CONTENT}:
            record_note_claim(self._resolve_base_url, repo, mr, "bulk_publish", endpoint="draft_notes/bulk_publish")
            notify_review_after_receipt(
                self._resolve_base_url,
                repo,
                mr,
                review_action=ReviewAfterReceipt(
                    action="publish_draft_notes",
                    summary=f"published all draft notes on {repo}!{mr}",
                ),
            )
            return "OK — all draft notes published", 0
        return f"Failed: HTTP {status}", 1

    def reply_to_discussion(self, repo: str, mr: int, discussion_id: str, body: str) -> tuple[str, int]:
        """Reply to an existing discussion thread on an MR. Returns (message, exit_code).

        Gated by ``on_behalf_post_mode`` (#960, BLOCK under `ask` / `draft_or_ask`): the reply is refused
        without any GitLab side effect when the gate is on and no recorded
        :class:`OnBehalfApproval` matches ``(<repo>!<mr>, "reply_to_discussion")``.
        """
        blocked = check_on_behalf(repo, mr, "reply_to_discussion")
        if blocked:
            return blocked, 1
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        # Reply bodies are always inline (anchored on the existing discussion's
        # diff position), so the inline cap applies.
        shape_error = check_review_shape(api=api, encoded_repo=encoded, mr=mr, body=body, inline=True)
        if shape_error:
            return shape_error, 1
        result = api.post_json(
            f"projects/{encoded}/merge_requests/{mr}/discussions/{discussion_id}/notes",
            {"body": body},
        )
        if not result:
            return "Failed to post reply", 1
        result_dict = dict(result) if isinstance(result, dict) else {}
        note_id = result_dict.get("id")
        record_note_claim(
            self._resolve_base_url, repo, mr, note_id, endpoint="discussions/notes", discussion_id=discussion_id
        )
        notify_review_after_receipt(
            self._resolve_base_url,
            repo,
            mr,
            review_action=ReviewAfterReceipt(
                action="reply_to_discussion",
                summary=f"replied to discussion {discussion_id} (note_id={note_id}) on {repo}!{mr}",
                note_web_url=str(result_dict.get("web_url", "")),
            ),
        )
        return f"OK reply_note_id={note_id}", 0

    def resolve_discussion(self, repo: str, mr: int, discussion_id: str, *, resolved: bool = True) -> tuple[str, int]:
        """Mark a discussion thread resolved or unresolved. Returns (message, exit_code).

        Gated by ``on_behalf_post_mode`` (#960, BLOCK under `ask` / `draft_or_ask`): a resolve flip is
        visible to colleagues (it closes the discussion under the user's
        identity), so it routes through the same recorded-approval gate.
        """
        blocked = check_on_behalf(repo, mr, "resolve_discussion")
        if blocked:
            return blocked, 1
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        flag = "true" if resolved else "false"
        status = api.put_status(f"projects/{encoded}/merge_requests/{mr}/discussions/{discussion_id}?resolved={flag}")
        if status in {HTTPStatus.OK, HTTPStatus.NO_CONTENT}:
            record_note_claim(
                self._resolve_base_url,
                repo,
                mr,
                f"{discussion_id}#resolved={flag}",
                endpoint="discussions/resolve",
                resolved=resolved,
            )
            notify_review_after_receipt(
                self._resolve_base_url,
                repo,
                mr,
                review_action=ReviewAfterReceipt(
                    action="resolve_discussion",
                    summary=f"set discussion {discussion_id} resolved={resolved} on {repo}!{mr}",
                ),
            )
            return f"OK resolved={resolved}", 0
        return f"Failed: HTTP {status}", 1

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
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        # Without diff coordinates here, treat the updated body as MR-level
        # prose — the tight cap applies. If the updated note is itself an
        # inline DiffNote the body will fit the inline cap too.
        shape_error = check_review_shape(api=api, encoded_repo=encoded, mr=mr, body=body, inline=False)
        if shape_error:
            return shape_error, 1

        draft_status = api.put_status(
            f"projects/{encoded}/merge_requests/{mr}/draft_notes/{note_id}",
            {"note": body},
        )
        if draft_status == HTTPStatus.OK:
            record_note_claim(
                self._resolve_base_url, repo, mr, f"update:draft:{note_id}", endpoint="draft_notes/update"
            )
            notify_review_after_receipt(
                self._resolve_base_url,
                repo,
                mr,
                review_action=ReviewAfterReceipt(
                    action="update_note",
                    summary=f"updated draft_note_id={note_id} on {repo}!{mr}",
                ),
            )
            return f"OK updated draft_note_id={note_id}", 0
        if draft_status != HTTPStatus.NOT_FOUND:
            return f"Failed (draft): HTTP {draft_status}", 1

        pub_status = api.put_status(
            f"projects/{encoded}/merge_requests/{mr}/notes/{note_id}",
            {"body": body},
        )
        if pub_status == HTTPStatus.OK:
            record_note_claim(self._resolve_base_url, repo, mr, f"update:pub:{note_id}", endpoint="notes/update")
            notify_review_after_receipt(
                self._resolve_base_url,
                repo,
                mr,
                review_action=ReviewAfterReceipt(
                    action="update_note",
                    summary=f"updated published note_id={note_id} on {repo}!{mr}",
                ),
            )
            return f"OK updated note_id={note_id}", 0
        return f"Failed: HTTP {pub_status}", 1

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
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        status = api.delete(f"projects/{encoded}/merge_requests/{mr}/notes/{note_id}")
        if status == HTTPStatus.NO_CONTENT:
            notify_review_after_receipt(
                self._resolve_base_url,
                repo,
                mr,
                review_action=ReviewAfterReceipt(
                    action="delete_discussion",
                    summary=f"deleted published note_id={note_id} on {repo}!{mr}",
                ),
            )
            return f"OK deleted note_id={note_id}", 0
        return f"Failed: HTTP {status}", status

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
        api = self._get_api()
        status = api.post_status(f"projects/{encoded}/merge_requests/{mr}/approve")
        if status in _HTTP_OK_CODES:
            record_note_claim(self._resolve_base_url, repo, mr, "approve", kind="gitlab_approve", endpoint="approve")
            return f"OK approved !{mr}", 0
        # GitLab returns 401 for the idempotent already-approved case as
        # well as a genuine auth failure (#1029). Probe /approvals to
        # distinguish: identity already in approved_by → no-op success.
        if identity_in_approved_by(api, encoded, mr):
            return f"Already approved by {api.current_username()} (!{mr})", 0
        return f"Failed: HTTP {status}", 1

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
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        status = api.post_status(f"projects/{encoded}/merge_requests/{mr}/unapprove")
        if status in _HTTP_OK_CODES:
            record_note_claim(
                self._resolve_base_url, repo, mr, "unapprove", kind="gitlab_approve", endpoint="unapprove"
            )
            return f"OK unapproved !{mr}", 0
        return f"Failed: HTTP {status}", 1


# Register sibling-module typer commands. Kept out of this file so the
# OOP/LOC ceiling (`scripts/hooks/check_module_health.py`) stays
# satisfied — see `teatree.cli.review_on_behalf`,
# `teatree.cli.review_drafts`, `teatree.cli.review_live_approval`, and
# `teatree.cli.review_commands`.
from teatree.cli import review_commands as _review_commands  # noqa: E402 — registration side-effect
from teatree.cli.review_commands import _require_token  # noqa: E402, F401 — re-exported for monkeypatch targets
from teatree.cli.review_live_approval import register as _register_live_approval  # noqa: E402 — late, after typer app

_register_on_behalf(review_app)
_register_drafts(review_app)
_register_live_approval(review_app)
_ = _review_commands  # quiet "unused import" — module load is the side-effect
