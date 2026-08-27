# Posting review comments — the babysit-tier draft flow and the CLI

The procedure and CLI recipes behind `/t3:review` § "Step 3 — Post Draft Review Comments". That section carries which tier the flow belongs to; this file carries the pre-flight duplicate check, the `t3 review` subcommands, the anchoring rules, and the rejected-case catalogue.

This step is the **`autonomy = "babysit"`** flow. Under an autonomous tier (`full` / `notify`), follow `/t3:review` § "Colleague-MR Autonomy — Act on the Verdict, Don't Ask" instead: act on the verdict without a per-MR ask, and approve per the merge-safe rule. The tier removes the round-trip, not the egress gate — a live post still needs `on_behalf_post_mode = "immediate"` or a recorded approval.

**Under babysit, always use draft notes** (or the platform's equivalent "pending review" feature), not direct/immediate comments. Draft notes are only visible to the reviewer until explicitly submitted — this lets the user review, edit, and submit all comments as a batch.

**Pre-flight: read existing comments (Non-Negotiable).** Before posting any new comments, fetch all existing discussions and notes on the PR (from all authors, not just the current user):

1. **List all discussions** via `GET .../merge_requests/<IID>/discussions?per_page=100` and read each note's `body`.
2. **For each finding**, check whether an existing comment already raises the same concern — same file, same line range, same substance. If so, **do not post a duplicate**.
3. **If you have something to add** to an existing discussion (additional context, a related concern on the same code), **reply in that thread** via `t3 review reply-to-discussion <REPO> <MR_IID> <DISCUSSION_ID> "body"` instead of creating a new top-level comment.
4. **Only post new draft notes** for findings not already covered by existing comments.

This prevents noise from multiple review passes or multiple reviewers covering the same ground.

**Post all *new* findings.** Don't self-censor or hold back comments because they seem minor. A draft note is colleague-invisible until the user submits it, so the user is the filter here exactly as the merge gate is on the verdict envelope — the suppression rules bind on what reaches a colleague, never on what reaches a curator. The user will review every draft note in the platform's UI, edit wording, and delete anything they don't want before submitting. Your job is to surface everything you noticed — the user curates. But "everything" means everything *not already said* — duplicating an existing comment wastes the author's time.

When reviewing an external MR/PR, **always post comments inline on the correct file and line** in the diff view. For comments that aren't tied to a specific line (e.g., description feedback), post a general note without position data.

**Extend the CLI, never inline API recipes.** If a `t3 review` operation is missing, implement it in `src/teatree/cli/review/service.py` — do NOT document a raw API snippet or inline script here. Skills describe what command to run, not how to replicate missing CLI functionality. Current subcommands: `run`, `post-comment`, `authorize`, `approve-live-post`, `delete-draft-note`, `delete-discussion`, `publish-draft-notes`, `list-draft-notes`, `update-note`, `reply-to-discussion`, `resolve-discussion`, `approve`, `unapprove`. (`post-draft-note` is deprecated — see below.)

**Read-only review-shape audit — `t3 review run <MR_URL>` (#1206).** Run before manually scanning the diff: the CLI emits a JSON summary (`changes.{files,additions,deletions}`, `complexity`, `existing_review.{open_discussions,draft_notes,approvals}`, `findings_catalog`, `verdict`) so every reviewer sub-agent starts from the same shape instead of improvising. The command never publishes; it just gathers what the reviewer needs to decide what to post via `post-comment` / `post-draft-note`. GitHub PR URLs and GitLab MR URLs both audit into the same payload shape; a URL naming neither forge exits 2 with `bad_url` — no masquerading success.

**Default-safe `t3 review post-comment` (Mandatory, #1207).** The subcommand creates a DRAFT by default and DMs the user the link — the CLI itself enforces the draft-by-default rule, so no separate prose check is required. To publish live (colleague-visible), authorize the MR in **one step** with `t3 review authorize <repo>!<mr> --approver <user-id>` (records the durable on-behalf authorization AND mints the single-use live-post token), then the agent re-runs with `--live`. Without an authorization `--live` refuses without any GitLab side effect, naming the `authorize` command in the refusal. The earlier two-command dance (`approve-on-behalf` + `approve-live-post --from-on-behalf`) still works and remains for the Slack-ts verification path, but `authorize` is the one-step collapse (#126).

```bash
t3 review post-comment <REPO> <MR_IID> "Comment text" --file <path/to/file> --line <line_number>
```

**Large evidence bodies — use `--body-file` (the scannable path, #32).** The comment body may come from the positional `NOTE`, `-m`/`--body <text>`, or `--body-file <path>` — exactly one source. For a large MR-thread evidence body (a verdict table, a multi-row reconciliation), write it to a file and pass `--body-file`, matching how `gh`/`glab` comment commands accept a body file. This avoids shell-quoting a huge positional and routes the body through the well-known flag the #1415 banned-terms gate reads and scans (the gate skips the `--file` diff anchor, which is a SOURCE path, not the body). `--file`/`--line` stay the inline diff anchor and compose with any body source.

```bash
t3 review post-comment <REPO> <MR_IID> --body-file <path/to/evidence.md>
```

The CLI validates the target line is an added (`+`) line in the MR diff before posting, and verifies the response anchored correctly (non-null `line_code`). When something goes wrong it refuses upfront — common rejected cases:

- **Context line:** the target is unchanged in the diff. CLI rejects and lists the nearby added lines so you can pick one.
- **File not in diff:** the file path isn't part of the MR. CLI rejects with the list of changed files.
- **Collapsed-diff file:** GitLab's draft-note anchoring fails on large files whose diff was collapsed server-side. CLI detects the null `line_code` after posting, deletes the broken draft, and suggests `post-comment` (below).

**Workaround for collapsed-diff files — `t3 review post-comment --live`.** When the file is too large for GitLab to anchor a draft, the post-flight anchor check refuses the draft. The historical workaround used the `/discussions` endpoint, which anchors even on collapsed diffs. Under #1207 that path requires a Slack-recorded approval — the user DMs an approval phrase ("post live" / "submit it" / "go ahead"), the agent records it via `t3 review approve-live-post <mr-url> --slack-ts <ts>`, and then re-runs:

```bash
t3 review post-comment <REPO> <MR_IID> "Comment text" --file <path/to/file> --line <line_number> --live
```

The `--live` post lands immediately instead of batching with a review. Reserve this for the cases where the default draft path explicitly errors with the collapsed-diff message AND the user has authorised the live post in Slack.

**The cost asymmetry is why `--live` is per-file, never per-review:** N findings cost N draft invocations and ONE human authorization for the batch publish, whereas `--live` costs TWO invocations per finding (`t3 review authorize` then `post-comment … --live`) because the live-post token is single-use — so only the collapsed-diff file goes live, and every other finding stays a draft.

**Pre-flight: the file you anchor on MUST be the file the body discusses.** If the comment body describes code in `foo.py` (e.g., "`foo.py`'s `bar()` is missing X that the sibling `baz.py` got"), anchor the comment on `foo.py` — not on `baz.py`, even if `baz.py` has more added lines in the diff. Two defensible patterns when `foo.py` has no added lines:

1. Pick the nearest added line in `foo.py` (even a whitespace or adjacent-line change) and open the body with "Note on an unchanged line below:" so the reader sees the anchor is a stand-in.
2. Post a general (PR-level) note instead of anchoring on a sibling.
A comment anchored on the wrong file is worse than a general note — the author opens `baz.py` looking for the problem, finds nothing, and loses trust in the review.

**Post-flight: verify response.** Response must confirm the comment landed on the correct file/line — if position data is missing in the response, the comment landed as a general comment (wrong). After posting all notes, list them via the API and confirm the count and positions match expectations.

**Do NOT submit the review without explicit user instruction.** By default, the user reviews draft notes in the platform's UI, edits if needed, and submits manually. If the user explicitly asks to publish (e.g., "post with t3 cli", "submit the review"), use:

```bash
t3 review publish-draft-notes <REPO> <MR_IID>
```

**If `t3 review delete-draft-note` returns 404** — the draft was already submitted (published to regular notes) by the user from the GitLab UI. Use `DELETE projects/{encoded_repo}/merge_requests/{iid}/notes/{note_id}` via the regular notes endpoint instead.
