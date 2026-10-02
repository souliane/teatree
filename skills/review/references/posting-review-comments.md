# Posting review comments — the babysit-tier draft flow and the CLI

The procedure and CLI recipes behind `/t3:review` § "Step 3 — Post Draft Review Comments". That section carries which tier the flow belongs to; this file carries the pre-flight duplicate check, the `t3 review` subcommands, the anchoring rules, and the rejected-case catalogue.

This step is the **`autonomy = "babysit"`** flow. Under an autonomous tier (`full` / `notify`), follow `/t3:review` § "Colleague-MR Autonomy — Act on the Verdict, Don't Ask" instead: act on the verdict without a per-MR ask, and approve per the merge-safe rule. The tier removes the round-trip, not the egress gate — a live post still needs a permitting posture (`t3 loop preset use present --reason <why>`) or a recorded approval.

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

**Inline findings go through the MCP tool first.** `mcp__teatree__review_post_comment(repo, mr, finding, live)` and `mcp__teatree__review_post_draft_note(repo, mr, finding)` take the SAME finding shape as one `review_post_comments` entry — `{note, anchor: "path/to/file.py:LINE", evidence, force_general, allow_bloat}` — so the single-post tools are the batch at arity one and there is one shape to learn rather than three. `anchor` carries the CLI's `--file`/`--line` as one value and `evidence` carries `--evidence-json`, either as a JSON object or as its JSON string. All three route through the same gated `ReviewService`, so every shape / bloat / evidence / on-behalf gate fires identically — the MCP server is long-lived, so a finding costs one call instead of one containerized `t3` process start. A blank `anchor` posts a general note; a malformed one is refused before any network call. Use the CLI recipes below when the MCP server is not connected, or the tool is not offered.

**A whole review posts as ONE batch — `mcp__teatree__review_post_comments(repo, mr, comments, live)`.** One review is N findings and the authorization ceremony is per-REVIEW, so prefer the batch over N single posts: `comments` is a list of the same findings, every body scanned by the same gates on its own, and a single recorded authorization covering the review instead of one per finding. A malformed entry refuses the whole batch naming its position, before anything is posted. A comment that fails mid-batch stops the run and reports how many landed — the forge posts are not transactional, so the remainder is a fresh call; the authorization survives that retry, and the comments that DID land keep their `OnBehalfAudit` row.

**`evidence` is per-COMMENT, and most real findings need it.** The #1280 gate refuses a "X is wrong / broken / missing" body without receipts, which is most of what a review says — so pass the same record `t3 review post-comment --evidence-json` takes — a JSON object (`{"master_check_paths": ["src/teatree/cli/review/service.py:42"], "confidence": "verified"}`) or that object's JSON string — on the findings that make a claim, and leave it off the nits beside them. `force_general` and `allow_bloat` are the same per-call escapes the CLI flags name, per comment too.

**Default-safe `t3 review post-comment` (Mandatory, #1207).** The subcommand creates a DRAFT by default and DMs the user the link — the CLI itself enforces the draft-by-default rule, so no separate prose check is required. To publish live (colleague-visible), authorize the MR in **one step** with `t3 review authorize <repo>!<mr> --approver <user-id>` (records the durable on-behalf authorization AND mints the single-use live-post token), then the agent re-runs with `--live`. Without an authorization `--live` refuses without any GitLab side effect, naming the `authorize` command in the refusal. The earlier two-command dance (`approve-on-behalf` + `approve-live-post --from-on-behalf`) still works and remains for the Slack-ts verification path, but `authorize` is the one-step collapse (#126).

```bash
t3 review post-comment <REPO> <MR_IID> "Comment text" --file <path/to/file> --line <line_number>
```

**Body files — use `--body-file` when the body already lives in a file (#32).** The comment body may come from the positional `NOTE`, `-m`/`--body <text>`, or `--body-file <path>` — exactly one source. The file avoids shell-quoting and routes the body through the well-known flag the #1415 banned-terms gate reads and scans (the gate skips the `--file` diff anchor, which is a SOURCE path, not the body). It does not relax the colleague-post shape: target at most ~300 characters and send the evidence chain to the owner in chat, never the MR/PR thread. Long-form own-MR self-review summaries remain exempt. `--file`/`--line` stay the inline diff anchor and compose with any body source.

```bash
t3 review post-comment <REPO> <MR_IID> --body-file <path/to/comment.md>
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

**Approval and severity are coupled — decide the VERDICT first, then write the notes to match it (Non-Negotiable).** An approval asserts that nothing you found blocks the merge, so every comment published on an MR you approve must read as non-blocking. A finding you would **not** approve over means **withholding the approval** — never post a blocking finding and approve anyway, and never soften a real blocker so you can approve. This drifts because a sub-agent's severity ladder is calibrated for the **verdict envelope** (recall-oriented: nothing published, everything recorded with its severity); the posted body carries no taxonomy, so severity reaches the author only through the approve-or-withhold decision. The lane split is `skills/review/SKILL.md` § "Two Lanes — a Colleague-Facing Post, and the Verdict Envelope".

**Live posts still need a token, even under `full`.** The `--live` colleague-visible publish is gated by the #1207 single-use `LivePostApproval` token (`teatree.core.gates.live_post_gate.require_live_post_approval`), which `publish_live_post` enforces **orthogonally to `autonomy`** — it is *not* in the collapsed-gate set, so `post-comment --live` is refused with no token regardless of tier. It has exactly ONE waiver, and it is the egress posture's, not the tier's: under a posture that permits egress — the owner's global opt-in to autonomous posting — `resolve_live_authorization` (#126) declares the post authorized with no token at all and `publish_live_post` honours that one verdict, so the two gates cannot declare a post authorized and refuse it one call later. Every OTHER route to a proceed verdict still burns a token: notably `on_behalf_auto_actions`, which is the owner's routine self-documentation allowlist and never a licence to publish a colleague-visible comment unsealed. Under an autonomous tier on a forbidding posture, mint the token in the same one step that records the on-behalf authorization — `t3 review authorize <repo>!<mr> --approver <user-id>` (#126) — then post live; or post the verdict as a **draft note** (`t3 review post-comment`, the default), which needs no token. Either path keeps the autonomous "act on the verdict, don't ask the user per-MR" posture; the token is a single-use idempotency/audit seal on the outward publish, not a per-MR user decision.

## Colleague-MR comment shape — the full rule

**Single terse inline finding on a colleague MR.** On a colleague's MR (the MR's author is not your identity), each posted finding is one inline comment anchored on the motivating file:line — `t3 review post-comment <repo> <mr> "<finding> — <pointer>" --file <path> --line <N>` (the anchor flag is `--file`, never `--path`): the finding plus one concrete pointer (that file:line, a symbol, or the failing input shape), targeting at most ~300 characters. The limit is on length, not sentence count: two sentences that pack a whole evidence chain still break it. The posted body is either a plain finding or starts with the literal `Nit:`; never prefix it with HIGH/MED/LOW or blocker/major/minor taxonomy. Never post LGTM notes, or an MR-level verdict note when inline findings already exist. Probe output, before/after payloads, spec-row transcriptions, and why CI missed it go to the owner in chat, never into the thread; terseness must not reduce a finding to a bare assertion, so one concrete pointer always survives. Never a multi-section Problem/Fix/Verification dump. The two lanes deliberately differ: the unpublished, server-recorded `review_verdict` envelope still records every finding with its full severity and confidence, while only the colleague-visible posted body drops the taxonomy; severity shows through what the comment says, not a label. The colleague-MR shape gate in `src/teatree/cli/review/shape_gate.py` (souliane/teatree#1114, loosened in #1159) is only a coarser backstop against abusive bodies: it refuses more than 3 blank-line-separated paragraphs or 200 words, but does not enforce the ~300-character target. Own-MR reviews remain exempt (long-form self-review summaries are fine).
