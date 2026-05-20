# CLI Reference

Generated from `t3` command tree.

## `t3`

```
Usage: t3 [OPTIONS] COMMAND [ARGS]...

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ startoverlay    Create a new TeaTree overlay package.                        │
│ docs            Serve the project documentation with mkdocs.                 │
│ agent           Launch Claude Code with auto-detected project context.       │
│ sessions        List recent Claude conversation sessions with resume         │
│                 commands.                                                    │
│ info            Show t3 installation, teatree/overlay sources, and editable  │
│                 status.                                                      │
│ config          Configuration and autoloading.                               │
│ ci              CI pipeline helpers.                                         │
│ review          Code review helpers.                                         │
│ review-request  Batch review requests.                                       │
│ doctor          Smoke-test hooks, imports, services.                         │
│ tool            Standalone utilities.                                        │
│ setup           First-time setup and global skill management.                │
│ update          Sync teatree core and registered overlays to their default   │
│                 branch.                                                      │
│ assess          Codebase health assessment.                                  │
│ overlay         Dev-mode overlay install/uninstall.                          │
│ infra           Teatree-wide infrastructure services.                        │
│ loop            Manage the tick-driven fat loop. Session-bound by design: it │
│                 runs only while a Claude Code session is open. The recurring │
│                 `t3 loop tick` cron is the driver — each tick the single     │
│                 tick-owner session atomically claims the next pending unit   │
│                 (`t3 loop claim-next`) and spawns one fresh bounded          │
│                 sub-agent for it. There is no roster of long-lived loop      │
│                 sub-agents to re-spawn (#786 WS3): if the owner session      │
│                 dies, the next open session becomes tick-owner and keeps     │
│                 ticking; with zero sessions open the loop is paused until    │
│                 the next session start (no OS daemon — accepted, not a       │
│                 defect). A per-agent Stop-hook self-pump re-continues the    │
│                 loop automatically while consolidated work remains — exactly │
│                 one consolidation loop per agent identity, deduped across    │
│                 all sessions (#786 WS4); it idles when none.                 │
│ slack           Slack integration commands.                                  │
│ teatree         Commands for the t3-teatree overlay.                         │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 startoverlay`

```
Usage: t3 startoverlay [OPTIONS] PROJECT_NAME DESTINATION

 Create a new TeaTree overlay package.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    project_name      TEXT  [required]                                      │
│ *    destination       PATH  [required]                                      │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --overlay-app            TEXT  Name of the overlay Django app                │
│                                [default: t3_overlay]                         │
│ --project-package        TEXT  Project package name (default: derived from   │
│                                project name)                                 │
│ --help                         Show this message and exit.                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 docs`

```
Usage: t3 docs [OPTIONS]

 Serve the project documentation with mkdocs.

 Requires the ``docs`` dependency group: ``uv sync --group docs``

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --host        TEXT     Host to bind to [default: 127.0.0.1]                  │
│ --port        INTEGER  Port to serve on [default: 8888]                      │
│ --help                 Show this message and exit.                           │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 agent`

```
Usage: t3 agent [OPTIONS] [TASK]

 Launch Claude Code with auto-detected project context.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   task      [TASK]  What to work on (e.g. 'fix the sync bug', 'add a new     │
│                     command')                                                │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --phase        TEXT  Explicit TeaTree phase override.                        │
│ --skill        TEXT  Explicit skill override. Repeat to load multiple        │
│                      skills.                                                 │
│ --help               Show this message and exit.                             │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 sessions`

```
Usage: t3 sessions [OPTIONS]

 List recent Claude conversation sessions with resume commands.

 By default shows sessions for the current working directory.
 Use --all to show sessions across all projects.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --project          TEXT     Filter by project dir substring                  │
│ --limit    -n      INTEGER  Max sessions to show [default: 20]               │
│ --all      -a               Show sessions from all projects                  │
│ --help                      Show this message and exit.                      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 info`

```
Usage: t3 info [OPTIONS]

 Show t3 installation, teatree/overlay sources, and editable status.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 config`

```
Usage: t3 config [OPTIONS] COMMAND [ARGS]...

 Configuration and autoloading.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ check-update       Check if a newer version of teatree is available.         │
│ show               Read-only view of config: text-file intent vs DB          │
│                    regenerable cache (#628).                                 │
│ write-skill-cache  Write overlay skill metadata + trigger index to XDG cache │
│                    for hook consumption.                                     │
│ autoload           List skill auto-loading rules from context-match.yml      │
│                    files.                                                    │
│ cache              Show the XDG skill-metadata cache content.                │
│ deps               Show resolved dependency chain for a skill.               │
│ test-trigger       Test which skill would be triggered for a given prompt.   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 config check-update`

```
Usage: t3 config check-update [OPTIONS]

 Check if a newer version of teatree is available.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 config show`

```
Usage: t3 config show [OPTIONS]

 Read-only view of config: text-file intent vs DB regenerable cache (#628).

 The intent section is ``~/.teatree.toml`` resolved — the user-authored
 source of truth. The derived section is DB / data-dir state that can be
 deleted and rebuilt from the text files; every entry is flagged
 regenerable so the cache-vs-intent invariant is visible. Reads only.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --json          Emit machine-readable JSON.                                  │
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 config write-skill-cache`

```
Usage: t3 config write-skill-cache [OPTIONS]

 Write overlay skill metadata + trigger index to XDG cache for hook
 consumption.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 config autoload`

```
Usage: t3 config autoload [OPTIONS]

 List skill auto-loading rules from context-match.yml files.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 config cache`

```
Usage: t3 config cache [OPTIONS]

 Show the XDG skill-metadata cache content.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 config deps`

```
Usage: t3 config deps [OPTIONS] SKILL

 Show resolved dependency chain for a skill.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    skill      TEXT  [required]                                             │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 config test-trigger`

```
Usage: t3 config test-trigger [OPTIONS] PROMPT

 Test which skill would be triggered for a given prompt.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    prompt      TEXT  [required]                                            │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 ci`

```
Usage: t3 ci [OPTIONS] COMMAND [ARGS]...

 CI pipeline helpers.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ cancel              Cancel stale CI pipelines for a branch.                  │
│ divergence          Check fork divergence from upstream.                     │
│ fetch-errors        Fetch error logs from the latest CI pipeline.            │
│ fetch-failed-tests  Extract failed test IDs from the latest CI pipeline.     │
│ trigger-e2e         Trigger E2E tests on CI.                                 │
│ coverage            Print current coverage and the configured floor;         │
│                     non-zero on failure.                                     │
│ quality-check       Run quality analysis (fetch test report from latest      │
│                     pipeline).                                               │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 ci cancel`

```
Usage: t3 ci cancel [OPTIONS] [BRANCH]

 Cancel stale CI pipelines for a branch.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   branch      [BRANCH]  Branch name (default: current branch)                │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 ci divergence`

```
Usage: t3 ci divergence [OPTIONS]

 Check fork divergence from upstream.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 ci fetch-errors`

```
Usage: t3 ci fetch-errors [OPTIONS] [BRANCH]

 Fetch error logs from the latest CI pipeline.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   branch      [BRANCH]  Branch name (default: current branch)                │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 ci fetch-failed-tests`

```
Usage: t3 ci fetch-failed-tests [OPTIONS] [BRANCH]

 Extract failed test IDs from the latest CI pipeline.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   branch      [BRANCH]  Branch name (default: current branch)                │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 ci trigger-e2e`

```
Usage: t3 ci trigger-e2e [OPTIONS] [BRANCH]

 Trigger E2E tests on CI.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   branch      [BRANCH]  Branch name (default: current branch)                │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 ci coverage`

```
Usage: t3 ci coverage [OPTIONS]

 Print current coverage and the configured floor; non-zero on failure.

 Reads `` fail_under`` and ``
 per_module_floors`` from ``pyproject.toml``. Loads ``.coverage`` for the
 measured percentages. Exits 1 if any floor is missed.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --json                       Output raw JSON                                 │
│ --coverage-file        PATH  Path to .coverage data file                     │
│                              [default: .coverage]                            │
│ --pyproject            PATH  Path to pyproject.toml                          │
│                              [default: pyproject.toml]                       │
│ --help                       Show this message and exit.                     │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 ci quality-check`

```
Usage: t3 ci quality-check [OPTIONS] [BRANCH]

 Run quality analysis (fetch test report from latest pipeline).

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   branch      [BRANCH]  Branch name (default: current branch)                │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 review`

```
Usage: t3 review [OPTIONS] COMMAND [ARGS]...

 Code review helpers.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ post-draft-note      Post a draft note on a GitLab MR (inline or general).   │
│ post-comment         Post a comment on a GitLab MR — DRAFT by default,       │
│                      ``--live`` requires Slack approval.                     │
│ reply-to-discussion  Reply to a GitLab MR discussion thread (immediate, not  │
│                      draft).                                                 │
│ approve              Approve a GitLab MR — only after you have reviewed it.  │
│ unapprove            Revoke your approval on a GitLab MR.                    │
│ approve-on-behalf    Record an :class:`OnBehalfApproval` that satisfies the  │
│                      on-behalf gate.                                         │
│ delete-draft-note    Delete a draft note from a GitLab MR.                   │
│ delete-discussion    Delete a *published* note (discussion) from a GitLab    │
│                      MR.                                                     │
│ publish-draft-notes  Publish all draft notes on a GitLab MR (bulk submit).   │
│ list-draft-notes     List draft notes on a GitLab MR.                        │
│ update-note          Update a note on a GitLab MR — auto-detects draft vs    │
│                      published.                                              │
│ resolve-discussion   Mark a GitLab MR discussion thread resolved or          │
│                      unresolved.                                             │
│ approve-live-post    Mint a Slack-recorded :class:`LivePostApproval` for     │
│                      ``<mr-url>``.                                           │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review post-draft-note`

```
Usage: t3 review post-draft-note [OPTIONS] REPO MR NOTE

 Post a draft note on a GitLab MR (inline or general).

 The inline-vs-general decision is explicit: pass ``--general`` for an
 MR-wide note, or pass both ``--file`` and ``--line`` for an inline
 draft. Pre-#72 the default silently degraded a missing flag pair into
 a general note — observed in !6220 where 4 of 5 cold-review drafts
 intended as inline became general. The validator
 :func:`teatree.cli.review_drafts.validate_inline_or_general` refuses
 both half-specified-inline and contradictory invocations before any
 GitLab API call is attempted.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    repo      TEXT     GitLab project path (e.g., my-org/my-repo)           │
│                         [required]                                           │
│ *    mr        INTEGER  Merge request IID [required]                         │
│ *    note      TEXT     Comment text (markdown) [required]                   │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --file           TEXT     File path for inline comment — REQUIRED unless     │
│                           --general is passed.                               │
│ --line           INTEGER  Line number in the new file (must be an added      │
│                           line) — REQUIRED unless --general is passed.       │
│ --general                 Post a general (MR-wide) note instead of an inline │
│                           one. Mutually exclusive with --file/--line.        │
│                           Without this flag, --file AND --line are both      │
│                           required — omitting either is refused upfront so a │
│                           missed-flag invocation can no longer silently      │
│                           degrade an intended-inline draft into a general    │
│                           note (souliane/teatree#72).                        │
│ --help                    Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review post-comment`

```
Usage: t3 review post-comment [OPTIONS] REPO MR NOTE

 Post a comment on a GitLab MR — DRAFT by default, ``--live`` requires Slack
 approval.

 Default behaviour (#1207): create a draft note via the same path as
 ``post-draft-note`` and DM the user the link, so the agent's job
 ends at the draft and the user submits. Pass ``--live`` to publish
 the comment directly — gated on a Slack-recorded
 :class:`~teatree.core.models.live_post_approval.LivePostApproval`
 for the MR (mint via ``t3 review approve-live-post``).

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    repo      TEXT     GitLab project path (e.g., my-org/my-repo)           │
│                         [required]                                           │
│ *    mr        INTEGER  Merge request IID [required]                         │
│ *    note      TEXT     Comment text (markdown) [required]                   │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --file        TEXT     File path for inline comment (omit for general note)  │
│ --line        INTEGER  Line number in the new file (must be an added line)   │
│                        [default: 0]                                          │
│ --live                 Publish a colleague-visible comment directly instead  │
│                        of creating a draft. Requires a single-use            │
│                        Slack-recorded approval token minted via `t3 review   │
│                        approve-live-post <mr-url> --slack-ts <ts>` (#1207).  │
│                        The default (no flag) creates a DRAFT and DMs the     │
│                        user the link — safe-by-default.                      │
│ --help                 Show this message and exit.                           │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review reply-to-discussion`

```
Usage: t3 review reply-to-discussion [OPTIONS] REPO MR DISCUSSION_ID BODY

 Reply to a GitLab MR discussion thread (immediate, not draft).

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    repo               TEXT     GitLab project path (e.g., my-org/my-repo)  │
│                                  [required]                                  │
│ *    mr                 INTEGER  Merge request IID [required]                │
│ *    discussion_id      TEXT     Discussion (thread) ID [required]           │
│ *    body               TEXT     Reply body (markdown) [required]            │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review approve`

```
Usage: t3 review approve [OPTIONS] REPO MR

 Approve a GitLab MR — only after you have reviewed it.

 Precondition: a review note/discussion authored by your identity must
 already exist on the MR (review before approve). Gated by
 `on_behalf_post_mode` (BLOCK under `ask` / `draft_or_ask`,
 souliane/teatree#960/#1013) — record an approval via
 ``t3 review approve-on-behalf <repo>!<mr> approve --approver
 <user-id>`` to satisfy the gate without switching mode to
 `immediate`.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    repo      TEXT     GitLab project path (e.g., my-org/my-repo)           │
│                         [required]                                           │
│ *    mr        INTEGER  Merge request IID [required]                         │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review unapprove`

```
Usage: t3 review unapprove [OPTIONS] REPO MR

 Revoke your approval on a GitLab MR.

 No review precondition (revoking is the safe direction). Gated by
 `on_behalf_post_mode` (BLOCK under `ask` / `draft_or_ask`,
 souliane/teatree#960/#1013) — record an approval via
 ``t3 review approve-on-behalf <repo>!<mr> unapprove --approver
 <user-id>`` to satisfy the gate without switching mode to
 `immediate`.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    repo      TEXT     GitLab project path (e.g., my-org/my-repo)           │
│                         [required]                                           │
│ *    mr        INTEGER  Merge request IID [required]                         │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review approve-on-behalf`

```
Usage: t3 review approve-on-behalf [OPTIONS] TARGET ACTION

 Record an :class:`OnBehalfApproval` that satisfies the on-behalf gate.

 The recorded-approval channel is the no-TTY satisfier for the
 ``on_behalf_post_mode`` pre-gate (#960, BLOCK verdict). It mirrors the
 #953 ``DbApproval`` / section 17.4 ``MergeClear`` shape:
 durable, single-use, strictly scoped to one
 ``(target, action)`` pair, maker!=checker enforced. After this
 command writes the row, the next on-behalf attempt matching
 ``(target, action)`` publishes and the row is consumed; an
 audit row records who/what/when.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    target      TEXT  Scope identifier the recorded approval is bound to —  │
│                        e.g. the MR ref `org/repo!42`, the PR url, or the     │
│                        ticket+transition compound the gate emitted in its    │
│                        `OnBehalfPostBlockedError` message.                   │
│                        [required]                                            │
│ *    action      TEXT  Action name the recorded approval authorises —        │
│                        exactly the string in the gate's blocked-post message │
│                        (`post_comment`, `reply_to_discussion`,               │
│                        `approval_reaction`, etc.). Single-use; consumed when │
│                        the next matching on-behalf attempt publishes.        │
│                        [required]                                            │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ *  --approver        TEXT  Identifier of the human user recording the        │
│                            approval. Refused if it names a                   │
│                            maker/coding-agent/loop role — the executing      │
│                            agent can never self-authorize the post (#960,    │
│                            mirrors DbApproval #953 / MergeClear section      │
│                            17.8).                                            │
│                            [required]                                        │
│    --help                  Show this message and exit.                       │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review delete-draft-note`

```
Usage: t3 review delete-draft-note [OPTIONS] REPO MR NOTE_ID

 Delete a draft note from a GitLab MR.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    repo         TEXT     GitLab project path [required]                    │
│ *    mr           INTEGER  Merge request IID [required]                      │
│ *    note_id      INTEGER  Draft note ID to delete [required]                │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review delete-discussion`

```
Usage: t3 review delete-discussion [OPTIONS] REPO MR NOTE_ID

 Delete a *published* note (discussion) from a GitLab MR.

 Use to clean up a published general comment that should have
 been inline, or any other published note that needs removal.
 Distinct from `delete-draft-note`, which removes a user's own
 pre-publication draft. Respects the `ask_before_post_on_behalf`
 pre-gate (souliane/teatree#960).

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    repo         TEXT     GitLab project path [required]                    │
│ *    mr           INTEGER  Merge request IID [required]                      │
│ *    note_id      INTEGER  Published note ID to delete [required]            │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review publish-draft-notes`

```
Usage: t3 review publish-draft-notes [OPTIONS] REPO MR

 Publish all draft notes on a GitLab MR (bulk submit).

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    repo      TEXT     GitLab project path (e.g., my-org/my-repo)           │
│                         [required]                                           │
│ *    mr        INTEGER  Merge request IID [required]                         │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review list-draft-notes`

```
Usage: t3 review list-draft-notes [OPTIONS] REPO MR

 List draft notes on a GitLab MR.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    repo      TEXT     GitLab project path [required]                       │
│ *    mr        INTEGER  Merge request IID [required]                         │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review update-note`

```
Usage: t3 review update-note [OPTIONS] REPO MR NOTE_ID BODY

 Update a note on a GitLab MR — auto-detects draft vs published.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    repo         TEXT     GitLab project path (e.g., my-org/my-repo)        │
│                            [required]                                        │
│ *    mr           INTEGER  Merge request IID [required]                      │
│ *    note_id      INTEGER  Note ID (draft or published) [required]           │
│ *    body         TEXT     New comment body (markdown) [required]            │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review resolve-discussion`

```
Usage: t3 review resolve-discussion [OPTIONS] REPO MR DISCUSSION_ID

 Mark a GitLab MR discussion thread resolved or unresolved.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    repo               TEXT     GitLab project path [required]              │
│ *    mr                 INTEGER  Merge request IID [required]                │
│ *    discussion_id      TEXT     Discussion (thread) ID [required]           │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --resolved    --no-resolved      Mark resolved (default) or re-open.         │
│                                  [default: resolved]                         │
│ --help                           Show this message and exit.                 │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review approve-live-post`

```
Usage: t3 review approve-live-post [OPTIONS] MR_URL

 Mint a Slack-recorded :class:`LivePostApproval` for ``<mr-url>``.

 After this command writes the row, the next
 ``t3 review post-comment <mr-url> ... --live`` invocation
 publishes (single-use, consumed by that call); any subsequent
 live post against the same MR requires a fresh approval.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    mr_url      TEXT  MR reference the live-post approval is scoped to —    │
│                        accepts the GitLab/GitHub URL (e.g.                   │
│                        ``https://gitlab.com/org/proj/-/merge_requests/42``)  │
│                        or the canonical ``<org/proj>!<iid>`` token.          │
│                        Single-use; consumed by the next matching ``t3 review │
│                        post-comment <mr-url> ... --live``.                   │
│                        [required]                                            │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ *  --slack-ts        TEXT  Slack timestamp (e.g. ``1700000000.0001``) of the │
│                            user's DM authorising the live post. The helper   │
│                            fetches that message, refuses unless it was       │
│                            authored by the configured user, is recent        │
│                            (within the TTL window), and contains an explicit │
│                            approval phrase (``post live`` / ``submit it`` /  │
│                            ``go ahead``).                                    │
│                            [required]                                        │
│    --help                  Show this message and exit.                       │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 review-request`

```
Usage: t3 review-request [OPTIONS] COMMAND [ARGS]...

 Batch review requests.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ discover  Discover open merge requests awaiting review.                      │
│ check     Race-safe pre-post dedup gate against LIVE Slack messages (#1084). │
│ post      Sanctioned authorized review-request post: #1094 dedup + #960      │
│           approval + post (#1098).                                           │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review-request discover`

```
Usage: t3 review-request discover [OPTIONS]

 Discover open merge requests awaiting review.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review-request check`

```
Usage: t3 review-request check [OPTIONS]

 Race-safe pre-post dedup gate against LIVE Slack messages (#1084).

 Run this in the SAME turn as a review-request post and abort on
 ``"action": "suppress"`` — it reads the live review channel with the
 post-token to detect a duplicate (agent re-post or a user's manual
 out-of-band post). It is strictly decision-only: it takes NO durable
 ``ReviewRequestPost`` claim (``peek_should_post_review_request``), so
 it can never leave an orphan that wedges a later real post (#1103).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ *  --mr-url        TEXT  Canonical MR/PR URL to dedup. [required]            │
│    --help                Show this message and exit.                         │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review-request post`

```
Usage: t3 review-request post [OPTIONS]

 Sanctioned authorized review-request post: #1094 dedup + #960 approval + post
 (#1098).

 One classifier-legible transaction: the #1084 live-channel dedup, the
 #960 recorded-approval chokepoint (``t3 review approve-on-behalf`` is
 the only way to satisfy it), then the post. Refuses with the exact
 ``approve-on-behalf`` remediation when no recorded approval matches.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ *  --mr-url          TEXT  Canonical MR/PR URL to post. [required]           │
│ *  --approver        TEXT  User id that recorded the #960 approval.          │
│                            [required]                                        │
│    --title           TEXT  Review-request subject (recommended).             │
│    --help                  Show this message and exit.                       │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 doctor`

```
Usage: t3 doctor [OPTIONS] COMMAND [ARGS]...

 Smoke-test hooks, imports, services.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ authorizations  Suggest absent recommended auto-mode authorizations          │
│                 (read-only).                                                 │
│ check           Verify imports, required tools, and editable-install sanity. │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 doctor authorizations`

```
Usage: t3 doctor authorizations [OPTIONS]

 Suggest absent recommended auto-mode authorizations (read-only).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 doctor check`

```
Usage: t3 doctor check [OPTIONS]

 Verify imports, required tools, and editable-install sanity.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 tool`

```
Usage: t3 tool [OPTIONS] COMMAND [ARGS]...

 Standalone utilities.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ privacy-scan     Scan text for privacy-sensitive patterns (emails, keys,     │
│                  IPs).                                                       │
│ validate-mr      Validate MR/PR title+description against the active         │
│                  overlay's rules.                                            │
│ repo-mode        Report whether the repo is solo (fix proactively) or        │
│                  collaborative (flag, don't fix).                            │
│ analyze-video    Decompose video into frames for AI analysis.                │
│ bump-deps        Bump pyproject.toml dependencies from uv.lock.              │
│ sonar-check      Run local SonarQube analysis via Docker.                    │
│ claude-handover  Show Claude handover telemetry and runtime recommendations. │
│ audit-memory     Scan Claude memory files for entries that should be         │
│                  promoted to skills.                                         │
│ notion-download  Download a Notion file attachment using the Brave browser   │
│                  session.                                                    │
│ ai-sig-scan      Refuse a PR body / commit message carrying an AI-signature  │
│                  trailer.                                                    │
│ diff-coverage    Per-diff coverage + mutation/revert gate (BLUEPRINT §17.6   │
│                  gate 12, #836).                                             │
│ label-issues     Suggest labels for unlabeled open issues by                 │
│                  keyword-matching title and body.                            │
│ find-duplicates  Flag pairs of open issues with near-identical titles.       │
│ triage-issues    Scan for resolved-but-open and stale issues.                │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 tool privacy-scan`

```
Usage: t3 tool privacy-scan [OPTIONS] [PATH]

 Scan text for privacy-sensitive patterns (emails, keys, IPs).

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   path      [PATH]  File or '-' for stdin [default: -]                       │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 tool validate-mr`

```
Usage: t3 tool validate-mr [OPTIONS]

 Validate MR/PR title+description against the active overlay's rules.

 Runs the active overlay's ``validate_pr`` (the same verdict used by
 ``t3 <overlay> pr create``). Exits non-zero and prints each error when
 the metadata is invalid. The pre-push hook invokes this by default so a
 bad title/description is rejected BEFORE the push — no env-var opt-in
 (#119).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --title              TEXT  MR/PR title                                       │
│ --description        TEXT  MR/PR description                                 │
│ --help                     Show this message and exit.                       │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 tool repo-mode`

```
Usage: t3 tool repo-mode [OPTIONS] [REPO]

 Report whether the repo is solo (fix proactively) or collaborative (flag,
 don't fix).

 One heuristic for every skill: ``git shortlog`` over the last 90 days on
 the default branch. A `` repo_mode`` config value overrides the
 detection. Result is cached 7 days per repo.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   repo      [REPO]  Repo path (default: current directory) [default: .]      │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --json             Emit machine-readable JSON.                               │
│ --refresh          Bypass the 7-day cache and re-detect.                     │
│ --help             Show this message and exit.                               │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 tool analyze-video`

```
Usage: t3 tool analyze-video [OPTIONS] VIDEO_PATH

 Decompose video into frames for AI analysis.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    video_path      TEXT  Path to video file [required]                     │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 tool bump-deps`

```
Usage: t3 tool bump-deps [OPTIONS]

 Bump pyproject.toml dependencies from uv.lock.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 tool sonar-check`

```
Usage: t3 tool sonar-check [OPTIONS] [REPO_PATH]

 Run local SonarQube analysis via Docker.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   repo_path      [REPO_PATH]  Path to repo (default: current directory)      │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --skip-baseline    --no-skip-baseline      Reuse previous baseline           │
│                                            [default: no-skip-baseline]       │
│ --remote           --no-remote             Push to CI server instead of      │
│                                            local                             │
│                                            [default: no-remote]              │
│ --remote-status    --no-remote-status      Fetch CI Sonar results            │
│                                            [default: no-remote-status]       │
│ --help                                     Show this message and exit.       │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 tool claude-handover`

```
Usage: t3 tool claude-handover [OPTIONS]

 Show Claude handover telemetry and runtime recommendations.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --current-runtime        TEXT  Current CLI runtime. Defaults to the          │
│                                highest-priority configured runtime.          │
│ --session-id             TEXT  Claude session ID to inspect. Defaults to     │
│                                latest telemetry.                             │
│ --state-dir              PATH  Override the Claude statusline telemetry      │
│                                directory.                                    │
│ --json                         Emit machine-readable JSON.                   │
│ --help                         Show this message and exit.                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 tool audit-memory`

```
Usage: t3 tool audit-memory [OPTIONS]

 Scan Claude memory files for entries that should be promoted to skills.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --verbose  -v        Show matched patterns for each entry.                   │
│ --help               Show this message and exit.                             │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 tool notion-download`

```
Usage: t3 tool notion-download [OPTIONS] URL

 Download a Notion file attachment using the Brave browser session.

 Accepts the `file://`-prefixed reference string that `t3`'s notion-fetch
 emits for `<file>` blocks; the signed URL is resolved server-side, so no
 manual browser click is required.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    url      TEXT  Either the `file://%7B…%7D` src from `notion-fetch`      │
│                     (resolved automatically via Notion's API — no browser    │
│                     click needed) or a pre-signed file.notion.so URL.        │
│                     [required]                                               │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --dest  -d      PATH  Destination directory. [default: .]                    │
│ --help                Show this message and exit.                            │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 tool ai-sig-scan`

```
Usage: t3 tool ai-sig-scan [OPTIONS] [PATH]

 Refuse a PR body / commit message carrying an AI-signature trailer.

 Enforces the "No AI Signature on Posts Made on the User's Behalf" rule
 (BLUEPRINT §17.6 gate 15, #836) as deterministic code — previously prose
 only in /t3:rules and unenforced at the PR-body layer (PR #831 leak).

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   path      [PATH]  File or '-' for stdin (PR body / commit message)         │
│                     [default: -]                                             │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 tool diff-coverage`

```
Usage: t3 tool diff-coverage [OPTIONS]

 Per-diff coverage + mutation/revert gate (BLUEPRINT §17.6 gate 12, #836).

 Measures coverage on the *diff's* added production lines (not the global
 ``fail_under``) and requires every new/changed production symbol to be
 imported by a changed test (the test-a-local-copy anti-vacuity check).
 Exits non-zero when a new line is uncovered or a symbol is unreferenced.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --repo                 PATH  Repo root (default: cwd)                        │
│                              [default: <bound method PathBase.cwd of <class  │
│                              'pathlib._local.Path'>>]                        │
│ --coverage-file        PATH  Path to .coverage data file                     │
│                              [default: .coverage]                            │
│ --json                       Emit machine-readable JSON.                     │
│ --help                       Show this message and exit.                     │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 tool label-issues`

```
Usage: t3 tool label-issues [OPTIONS] REPO

 Suggest labels for unlabeled open issues by keyword-matching title and body.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    repo      TEXT  Repository in owner/name form (e.g. souliane/teatree)   │
│                      [required]                                              │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --apply          Apply labels via `gh issue edit` (default: print only).     │
│ --help           Show this message and exit.                                 │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 tool find-duplicates`

```
Usage: t3 tool find-duplicates [OPTIONS] REPO

 Flag pairs of open issues with near-identical titles.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    repo      TEXT  Repository in owner/name form (e.g. souliane/teatree)   │
│                      [required]                                              │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --threshold        FLOAT RANGE [0.0<=x<=1.0]  Similarity ratio required to   │
│                                               flag a pair (0.0-1.0).         │
│                                               [default: 0.75]                │
│ --help                                        Show this message and exit.    │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 tool triage-issues`

```
Usage: t3 tool triage-issues [OPTIONS] REPO

 Scan for resolved-but-open and stale issues.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    repo      TEXT  Repository in owner/name form (e.g. souliane/teatree)   │
│                      [required]                                              │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --stale-days            INTEGER  Inactivity threshold for stale-issue        │
│                                  detection.                                  │
│                                  [default: 30]                               │
│ --close-resolved                 Close resolved-but-open issues (with        │
│                                  comment linking the merged PR).             │
│ --help                           Show this message and exit.                 │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 setup`

```
Usage: t3 setup [OPTIONS] COMMAND [ARGS]...

 First-time setup and global skill management.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --skip-plugin          Skip Claude CLI plugin registration.                  │
│ --help                 Show this message and exit.                           │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ slack-bot         Register or update a per-overlay Slack bot and store its   │
│                   tokens via ``pass``.                                       │
│ slack-user-token  Re-authorize the personal Slack xoxp token and store it    │
│                   via ``pass``.                                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 setup slack-bot`

```
Usage: t3 setup slack-bot [OPTIONS]

 Register or update a per-overlay Slack bot and store its tokens via ``pass``.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ *  --overlay                TEXT  Overlay name as registered in              │
│                                   `~/.teatree.toml`.                         │
│                                   [required]                                 │
│    --reset                        Rotate the existing bot + app tokens; skip │
│                                   the manifest URL.                          │
│    --update                       Force the in-place manifest update path    │
│                                   (prompts for the app id if none recorded). │
│    --skip-smoke-test              Skip the round-trip DM verification.       │
│    --config                 PATH  Path to teatree config (default:           │
│                                   ~/.teatree.toml).                          │
│                                   [default: /Users/adrien/.teatree.toml]     │
│    --help                         Show this message and exit.                │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 setup slack-user-token`

```
Usage: t3 setup slack-user-token [OPTIONS]

 Re-authorize the personal Slack xoxp token and store it via ``pass``.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --reset               Overwrite the existing token without prompting.        │
│ --config        PATH  Path to teatree config (default: ~/.teatree.toml).     │
│                       [default: /Users/adrien/.teatree.toml]                 │
│ --help                Show this message and exit.                            │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 update`

```
Usage: t3 update [OPTIONS] COMMAND [ARGS]...

 Sync teatree core and registered overlays to their default branch.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 assess`

```
Usage: t3 assess [OPTIONS] COMMAND [ARGS]...

 Codebase health assessment.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ run      Run deterministic codebase metrics on a repository.                 │
│ history  Show assessment history for a repository.                           │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 assess run`

```
Usage: t3 assess run [OPTIONS]

 Run deterministic codebase metrics on a repository.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --root                 PATH  Repository root to assess                       │
│ --json                       Output raw JSON                                 │
│ --save    --no-save          Save results to .t3/assessments/                │
│                              [default: save]                                 │
│ --help                       Show this message and exit.                     │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 assess history`

```
Usage: t3 assess history [OPTIONS]

 Show assessment history for a repository.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --root           PATH     Repository root                                    │
│ --limit  -n      INTEGER  Number of recent assessments to show [default: 10] │
│ --help                    Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 overlay`

```
Usage: t3 overlay [OPTIONS] COMMAND [ARGS]...

 Dev-mode overlay install/uninstall.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ install    Install an overlay editable into the current teatree worktree for │
│            dogfooding.                                                       │
│ uninstall  Uninstall an overlay from the current teatree worktree venv.      │
│ status     Show overlays currently installed into this teatree worktree.     │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 overlay install`

```
Usage: t3 overlay install [OPTIONS] NAME

 Install an overlay editable into the current teatree worktree for dogfooding.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    name      TEXT  Overlay name as configured in ~/.teatree.toml.          │
│                      [required]                                              │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 overlay uninstall`

```
Usage: t3 overlay uninstall [OPTIONS] NAME

 Uninstall an overlay from the current teatree worktree venv.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    name      TEXT  Overlay name to uninstall. [required]                   │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 overlay status`

```
Usage: t3 overlay status [OPTIONS]

 Show overlays currently installed into this teatree worktree.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 infra`

```
Usage: t3 infra [OPTIONS] COMMAND [ARGS]...

 Teatree-wide infrastructure services.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ redis  Shared Redis container (teatree-redis).                               │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 infra redis`

```
Usage: t3 infra redis [OPTIONS] COMMAND [ARGS]...

 Shared Redis container (teatree-redis).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ up      Start the shared Redis container (idempotent).                       │
│ down    Stop the shared Redis container.                                     │
│ status  Print the shared Redis container status.                             │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 infra redis up`

```
Usage: t3 infra redis up [OPTIONS]

 Start the shared Redis container (idempotent).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 infra redis down`

```
Usage: t3 infra redis down [OPTIONS]

 Stop the shared Redis container.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 infra redis status`

```
Usage: t3 infra redis status [OPTIONS]

 Print the shared Redis container status.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 loop`

```
Usage: t3 loop [OPTIONS] COMMAND [ARGS]...

 Manage the tick-driven fat loop. Session-bound by design: it runs only while a
 Claude Code session is open. The recurring `t3 loop tick` cron is the driver —
 each tick the single tick-owner session atomically claims the next pending
 unit (`t3 loop claim-next`) and spawns one fresh bounded sub-agent for it.
 There is no roster of long-lived loop sub-agents to re-spawn (#786 WS3): if
 the owner session dies, the next open session becomes tick-owner and keeps
 ticking; with zero sessions open the loop is paused until the next session
 start (no OS daemon — accepted, not a defect). A per-agent Stop-hook self-pump
 re-continues the loop automatically while consolidated work remains — exactly
 one consolidation loop per agent identity, deduped across all sessions (#786
 WS4); it idles when none.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ tick           Run one tick: scan in parallel, dispatch, render statusline.  │
│ status         Show the loop's last-rendered statusline.                     │
│ pending-spawn  List pending Tasks (read-only probe; legacy — prefer          │
│                ``claim-next``).                                              │
│ spawn-claim    Claim a Task by id (legacy — prefer atomic ``claim-next``).   │
│ start          Spawn a Claude Code session with the fat loop pre-registered. │
│ stop           Print the slot id to stop in the Claude Code session.         │
│ claim          Claim the session-scoped loop-owner slot for this Claude      │
│                session (#1073).                                              │
│ owner          Show which session currently owns the loop-owner slot         │
│                (#1073).                                                      │
│ release        Release this session's loop-owner claim (#1073).              │
│ claim-next     Atomically claim the oldest pending dispatchable Task, then   │
│                emit it.                                                      │
│ self-improve   Self-improving monitor — scheduled smell detection with a     │
│                tiered action ladder. Runs in the same loop-owner session as  │
│                `t3 loop tick` on a separate LoopLease so a long self-improve │
│                cycle never blocks a fast regular tick (BLUEPRINT § 5.7).     │
│ slack-answer   Reactive, token-cheap Slack-answer loop — the third `/loop`   │
│                slot. Runs on a tight cadence (default 20s) in the same       │
│                loop-owner session as `t3 loop tick`, on a separate LoopLease │
│                so a long answer cycle never blocks a fast regular tick.      │
│                Complementary to the inbound prompt-drain, never a            │
│                double-answer (#1014).                                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 loop tick`

```
Usage: t3 loop tick [OPTIONS]

 Run one tick: scan in parallel, dispatch, render statusline.

 Delegates to the ``loop_tick`` Django management command so that
 Django is bootstrapped by the management framework (not manual
 ``django.setup()``).  All heavy imports (ORM, backends, scanners)
 live in the management command module, not here.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --statusline-file        PATH  Override the statusline output path (test     │
│                                hook).                                        │
│ --overlay                TEXT  Restrict scanning to the named overlay        │
│                                (default: scan every registered overlay).     │
│ --json                         Emit the tick report as JSON.                 │
│ --help                         Show this message and exit.                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 loop status`

```
Usage: t3 loop status [OPTIONS]

 Show the loop's last-rendered statusline.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 loop pending-spawn`

```
Usage: t3 loop pending-spawn [OPTIONS]

 List pending Tasks (read-only probe; legacy — prefer ``claim-next``).

 Reads the dispatch DB (``Task`` rows in PENDING status) and prints
 each with its ``subagent`` hint. This is a pure read with NO claim:
 the spawn-then-``spawn-claim`` flow it used to drive was the
 double-dispatch race #786 WS1 replaced with the atomic
 ``t3 loop claim-next`` (claim-then-spawn). Retained for compatibility
 and as a non-mutating "is there pending work?" probe (e.g. the
 Stop-hook self-pump); the ``/loop`` slot should drive dispatch with
 ``claim-next``, not this + ``spawn-claim``.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --json          Emit pending list as JSON.                                   │
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 loop spawn-claim`

```
Usage: t3 loop spawn-claim [OPTIONS] TASK_ID

 Claim a Task by id (legacy — prefer atomic ``claim-next``).

 The retired spawn-then-claim flow called this AFTER dispatching
 ``Agent(...)``, leaving a window where two concurrent ticks both
 dispatched the same Task. #786 WS1's ``t3 loop claim-next`` claims
 atomically BEFORE the spawn (claim == spawn boundary) and is what the
 ``/loop`` slot should use. Retained for compatibility / explicit
 by-id claims; ``complete`` still happens when the sub-agent reports
 back via the standard TaskAttempt flow.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    task_id      INTEGER  Task PK to mark claimed. [required]               │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --claimed-by        TEXT  [default: loop-slot]                               │
│ --help                    Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 loop start`

```
Usage: t3 loop start [OPTIONS]

 Spawn a Claude Code session with the fat loop pre-registered.

 Looks for ``claude`` on ``PATH`` and runs it with an initial
 ``/loop <cadence> !t3 loop tick`` prompt so the loop is registered
 before the user types anything. When ``claude`` is not available or
 the caller is already inside a Claude Code session, falls back to
 printing the slash command for manual entry.

 Durability (by design; #786 WS3): the loop is session-bound and
 tick-driven. The SessionStart hook records ONE Django-free tick-owner
 record (``_OWNER_LOOP``: session_id/agent_id/pid/heartbeat — no
 per-loop briefs) in the machine-wide loop registry. There is no
 roster to re-spawn: the ``t3 loop tick`` cron drives the loop, each
 tick atomically claiming the next pending unit (``t3 loop
 claim-next``) and spawning one fresh bounded sub-agent for it. If
 this session dies, the next open session prunes the dead owner,
 becomes tick-owner, and keeps ticking. With no session open the loop
 is paused until the next session start; there is deliberately no
 OS-scheduler/launchd fallback.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --print-only          Print the /loop slot definition instead of spawning a  │
│                       Claude Code session.                                   │
│ --help                Show this message and exit.                            │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 loop stop`

```
Usage: t3 loop stop [OPTIONS]

 Print the slot id to stop in the Claude Code session.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 loop claim`

```
Usage: t3 loop claim [OPTIONS]

 Claim the session-scoped loop-owner slot for this Claude session (#1073).

 Without ``--take-over`` a live claimant blocks the claim. With it,
 the claim is unconditional — the hijacking session's next ``t3 loop
 tick`` SKIPs within one tick, no restart needed. Exits 2 when not
 running inside a Claude Code session (no session id to claim with).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --take-over              Evict a live claimant — the chat-only user's loop   │
│                          hand-off (#1073).                                   │
│ --slot             TEXT  Loop-owner slot name (default: loop-owner).         │
│                          [default: loop-owner]                               │
│ --json                   Emit JSON.                                          │
│ --help                   Show this message and exit.                         │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 loop owner`

```
Usage: t3 loop owner [OPTIONS]

 Show which session currently owns the loop-owner slot (#1073).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --slot        TEXT  Loop-owner slot name (default: loop-owner).              │
│                     [default: loop-owner]                                    │
│ --json              Emit JSON.                                               │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 loop release`

```
Usage: t3 loop release [OPTIONS]

 Release this session's loop-owner claim (#1073).

 CAS on session id — a non-owner release is a no-op and never evicts
 a live owner.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --slot        TEXT  Loop-owner slot name (default: loop-owner).              │
│                     [default: loop-owner]                                    │
│ --json              Emit JSON.                                               │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 loop claim-next`

```
Usage: t3 loop claim-next [OPTIONS]

 Atomically claim the oldest pending dispatchable Task, then emit it.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --claimed-by        TEXT  Worker identifier stored on the claim.             │
│ --json                    Emit the claimed dispatch as JSON.                 │
│ --help                    Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 loop self-improve`

```
Usage: t3 loop self-improve [OPTIONS] COMMAND [ARGS]...

 Self-improving monitor — scheduled smell detection with a tiered action
 ladder. Runs in the same loop-owner session as `t3 loop tick` on a separate
 LoopLease so a long self-improve cycle never blocks a fast regular tick
 (BLUEPRINT § 5.7).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ run     Run one self-improve schedule cycle for the given tier.              │
│ status  List the most recent SelfImproveFiring rows.                         │
│ start   Print the ``/loop <cadence>`` slot definition for the self-improve   │
│         monitor.                                                             │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 loop self-improve run`

```
Usage: t3 loop self-improve run [OPTIONS]

 Run one self-improve schedule cycle for the given tier.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --tier        TEXT  Cost tier: cheap|medium|expensive|all (default: cheap;   │
│                     Phase 1 ships cheap only).                               │
│                     [default: cheap]                                         │
│ --json              Emit the cycle report as JSON.                           │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 loop self-improve status`

```
Usage: t3 loop self-improve status [OPTIONS]

 List the most recent SelfImproveFiring rows.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --limit        INTEGER  Max firings to show (default 20). [default: 20]      │
│ --help                  Show this message and exit.                          │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 loop self-improve start`

```
Usage: t3 loop self-improve start [OPTIONS]

 Print the ``/loop <cadence>`` slot definition for the self-improve monitor.

 Mirrors ``t3 loop start --print-only``: it prints the slash command
 the user pastes inside the loop-owner Claude Code session to register
 the second ``/loop`` slot.  The cheap tier runs by default; override
 via ``T3_SELF_IMPROVE_CHEAP_CADENCE`` (seconds).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 loop slack-answer`

```
Usage: t3 loop slack-answer [OPTIONS] COMMAND [ARGS]...

 Reactive, token-cheap Slack-answer loop — the third `/loop` slot. Runs on a
 tight cadence (default 20s) in the same loop-owner session as `t3 loop tick`,
 on a separate LoopLease so a long answer cycle never blocks a fast regular
 tick. Complementary to the inbound prompt-drain, never a double-answer
 (#1014).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ run     Run one reactive Slack-answer cycle.                                 │
│ status  Show the reactive Slack-answer loop's unreplied queue depth.         │
│ start   Print the ``/loop <cadence>`` slot definition for the Slack-answer   │
│         loop.                                                                │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 loop slack-answer run`

```
Usage: t3 loop slack-answer run [OPTIONS]

 Run one reactive Slack-answer cycle.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --json          Emit the cycle report as JSON.                               │
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 loop slack-answer status`

```
Usage: t3 loop slack-answer status [OPTIONS]

 Show the reactive Slack-answer loop's unreplied queue depth.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 loop slack-answer start`

```
Usage: t3 loop slack-answer start [OPTIONS]

 Print the ``/loop <cadence>`` slot definition for the Slack-answer loop.

 Mirrors ``t3 loop self-improve start``: prints the slash command the
 user pastes inside the loop-owner Claude Code session to register the
 third ``/loop`` slot. Override the cadence via ``T3_SLACK_ANSWER_CADENCE``
 (seconds; floor 15).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 slack`

```
Usage: t3 slack [OPTIONS] COMMAND [ARGS]...

 Slack integration commands.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ listen  Run the Socket Mode receiver for all (or one) slack-enabled          │
│         overlays.                                                            │
│ check   Drain the event queue, ack with 👀, and print new user messages.     │
│ status  Check if the Socket Mode listener is running.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 slack listen`

```
Usage: t3 slack listen [OPTIONS]

 Run the Socket Mode receiver for all (or one) slack-enabled overlays.

 Maintains one WebSocket per overlay, writes events to a JSONL queue
 file that the fat loop tick drains. Runs until SIGTERM or SIGINT.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --overlay           TEXT  Restrict to a single overlay (default: all).       │
│ --queue-file        PATH  Override the event queue path (test hook).         │
│ --help                    Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 slack check`

```
Usage: t3 slack check [OPTIONS]

 Drain the event queue, ack with 👀, and print new user messages.

 Reads the JSONL queue written by ``t3 slack listen``, filters for
 user messages (ignoring bot posts), reacts with ``eyes`` on each
 to signal the bot has seen it, then prints each as a JSON line.
 Returns exit code 0 when messages were found, 1 when the queue
 was empty. Designed to be called from a fast cron (every 30s).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 slack status`

```
Usage: t3 slack status [OPTIONS]

 Check if the Socket Mode listener is running.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `t3 teatree`

```
Usage: t3 teatree [OPTIONS] COMMAND [ARGS]...

 Commands for the t3-teatree overlay.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ resetdb       Drop the SQLite database and re-run all migrations.            │
│ worker        Start background task workers.                                 │
│ full-status   Show ticket, worktree, and session state summary.              │
│ ship          Code to PR — create pull request for the ticket.               │
│ daily         Daily followup — sync MRs, check gates, remind reviewers.      │
│ agent         Launch Claude Code with overlay context and auto-detected      │
│               skills.                                                        │
│ config        Overlay configuration.                                         │
│ worktree      Per-worktree FSM operations.                                   │
│ workspace     Ticket-level workspace operations (every worktree in the       │
│               ticket).                                                       │
│ run           Run services.                                                  │
│ e2e           E2E test commands.                                             │
│ db            Database operations.                                           │
│ pr            Pull request helpers.                                          │
│ tasks         Async task queue.                                              │
│ followup      Follow-up snapshots.                                           │
│ standup       Auto-generated daily update (read-only).                       │
│ lifecycle     Session lifecycle and phase tracking.                          │
│ env           Inspect and mutate the worktree env cache.                     │
│ ticket        Ticket state management.                                       │
│ availability  24/7 dual question-mode (#58, BLUEPRINT §17.1 invariant 9).    │
│ questions     Manage the away-mode deferred-question backlog (#58).          │
│ pending_chat  Manage the inbound Slack-DM queue (#1063).                     │
│ notify        Bot→user Slack DM from the shell (#1030).                      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree resetdb`

```
Usage: t3 teatree resetdb [OPTIONS]

 Drop the SQLite database and re-run all migrations.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree worker`

```
Usage: t3 teatree worker [OPTIONS]

 Start background task workers.

 Singleton across the machine: a second invocation refuses to start
 while one is alive, since both would drain the same canonical DB.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --count           INTEGER  Number of worker processes [default: 3]           │
│ --interval        FLOAT    Polling interval in seconds [default: 1.0]        │
│ --help                     Show this message and exit.                       │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree full-status`

```
Usage: t3 teatree full-status [OPTIONS]

 Show ticket, worktree, and session state summary.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree ship`

```
Usage: t3 teatree ship [OPTIONS] TICKET_ID

 Code to PR — create pull request for the ticket.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    ticket_id      TEXT  Ticket ID [required]                               │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --title        TEXT  PR title                                                │
│ --help               Show this message and exit.                             │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree daily`

```
Usage: t3 teatree daily [OPTIONS]

 Daily followup — sync MRs, check gates, remind reviewers.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree agent`

```
Usage: t3 teatree agent [OPTIONS] [TASK]

 Launch Claude Code with overlay context and auto-detected skills.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   task      [TASK]  What to work on                                          │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --phase        TEXT  Explicit TeaTree phase override.                        │
│ --skill        TEXT  Explicit skill override. Repeat to load multiple        │
│                      skills.                                                 │
│ --help               Show this message and exit.                             │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree config`

```
Usage: t3 teatree config [OPTIONS] COMMAND [ARGS]...

 Overlay configuration.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree worktree`

```
Usage: t3 teatree worktree [OPTIONS] COMMAND [ARGS]...

 Per-worktree FSM operations.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ provision   Run DB import + env cache + direnv + prek + overlay setup steps  │
│             for one worktree.                                                │
│ start       Boot ``docker compose up`` for one worktree.                     │
│ verify      Run overlay health checks for one worktree.                      │
│ ready       Run runtime readiness probes for one worktree.                   │
│ teardown    Stop docker, drop DB, remove git worktree, delete row.           │
│ status      Report FSM state, branch, and allocated host ports for one       │
│             worktree.                                                        │
│ diagnose    Print a structured health checklist for one worktree.            │
│ smoke-test  Quick health check: overlay loads, CLI responds, imports OK.     │
│ diagram     Print a state diagram as Mermaid. Models: worktree, ticket,      │
│             task.                                                            │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree worktree provision`

```
Usage: t3 teatree worktree provision [OPTIONS]

 Run DB import + env cache + direnv + prek + overlay setup steps for one
 worktree.

 Thin wrapper around ``Worktree.provision()``: the FSM flips
 CREATED → PROVISIONED and enqueues ``execute_worktree_provision``;
 the runner also runs synchronously here so the operator sees
 immediate output. Idempotent — re-running is safe.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path                               TEXT  Worktree path (auto-detects from  │
│                                            PWD if empty).                    │
│ --variant                            TEXT  Tenant variant. Updates ticket if │
│                                            provided.                         │
│ --overlay                            TEXT  Overlay name (auto-detects if     │
│                                            empty).                           │
│ --slow-import    --no-slow-import          Allow slow DB fallbacks           │
│                                            (pg_restore, remote dump).        │
│                                            DSLR-only by default.             │
│                                            [default: no-slow-import]         │
│ --help                                     Show this message and exit.       │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree worktree start`

```
Usage: t3 teatree worktree start [OPTIONS]

 Boot ``docker compose up`` for one worktree.

 Thin wrapper around ``Worktree.start_services()``: the FSM advances
 to SERVICES_UP and enqueues ``execute_worktree_start``; the runner
 also runs synchronously here so the operator sees immediate output.
 Refreshes the env cache, runs overlay pre-run steps, then
 ``docker compose up -d``. Docker auto-maps host ports; the actual
 ports are then queried via ``docker compose port`` and stored on
 ``Worktree.extra["ports"]``. After the runner succeeds, runs the
 overlay's readiness probes — exits 1 if any fail.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path (auto-detects from PWD if empty).          │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree worktree verify`

```
Usage: t3 teatree worktree verify [OPTIONS]

 Run overlay health checks for one worktree.

 Thin wrapper around ``Worktree.verify()``: SERVICES_UP → READY +
 runner records URLs and reports failed checks. After the runner,
 runs the overlay's readiness probes — exits 1 if any fail.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path (auto-detects from PWD if empty).          │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree worktree ready`

```
Usage: t3 teatree worktree ready [OPTIONS]

 Run runtime readiness probes for one worktree.

 Strict: exits 0 iff every probe declared by
 ``OverlayBase.get_readiness_probes``
 passes. Does not mutate worktree state. Use after ``start`` to verify
 the env is actually serving — answers the question ``verify`` cannot
 (HTTP, CORS round-trip, end-to-end auth, fixture seed integrity).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path (auto-detects from PWD if empty).          │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree worktree teardown`

```
Usage: t3 teatree worktree teardown [OPTIONS]

 Stop docker, drop DB, remove git worktree, delete row.

 Thin wrapper around ``Worktree.teardown()``: the FSM resets to
 CREATED and enqueues ``execute_worktree_teardown``; the runner
 also runs synchronously here so the operator sees immediate
 output. Folds the previous ``teardown`` + ``clean`` commands
 into a single canonical path. Refuses to remove a worktree whose
 branch carries unpushed commits unless ``--force`` is passed.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path                   TEXT  Worktree path (auto-detects from PWD if       │
│                                empty).                                       │
│ --force    --no-force          Tear down even when the branch has commits    │
│                                not on any remote (data loss).                │
│                                [default: no-force]                           │
│ --help                         Show this message and exit.                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree worktree status`

```
Usage: t3 teatree worktree status [OPTIONS]

 Report FSM state, branch, and allocated host ports for one worktree.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path (auto-detects from PWD if empty).          │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree worktree diagnose`

```
Usage: t3 teatree worktree diagnose [OPTIONS]

 Print a structured health checklist for one worktree.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path (auto-detects from PWD if empty).          │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree worktree smoke-test`

```
Usage: t3 teatree worktree smoke-test [OPTIONS]

 Quick health check: overlay loads, CLI responds, imports OK.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree worktree diagram`

```
Usage: t3 teatree worktree diagram [OPTIONS]

 Print a state diagram as Mermaid. Models: worktree, ticket, task.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --model         TEXT     [default: worktree]                                 │
│ --ticket        INTEGER                                                      │
│ --help                   Show this message and exit.                         │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree workspace`

```
Usage: t3 teatree workspace [OPTIONS] COMMAND [ARGS]...

 Ticket-level workspace operations (every worktree in the ticket).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ ticket        Create or update a ticket and trigger worktree provisioning.   │
│ provision     Provision every worktree in the current ticket workspace.      │
│ start         Start docker for every worktree in the current ticket          │
│               workspace.                                                     │
│ ready         Run readiness probes for every worktree in the ticket          │
│               workspace.                                                     │
│ teardown      Tear down every worktree in the current ticket workspace.      │
│ finalize      Squash worktree commits and rebase on the default branch.      │
│ doctor        Detect state drift across every store; optionally fix it.      │
│ clean-merged  Tear down every worktree whose ticket is already MERGED.       │
│ clean-all     Prune merged worktrees, stale branches, orphaned stashes,      │
│               orphan DBs, old DSLR snapshots.                                │
│ list-orphans  List orphan branches (commits not on main, no open PR).        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree workspace ticket`

```
Usage: t3 teatree workspace ticket [OPTIONS] ISSUE_URL

 Create or update a ticket and trigger worktree provisioning.

 Thin wrapper around the FSM (BLUEPRINT §4): persist branch + description
 on ``ticket.extra``, advance ``NOT_STARTED → SCOPED → STARTED`` via
 ``scope()`` and ``start()``, and let ``execute_provision`` materialise
 the per-repo git worktrees on the worker side.

 Idempotent: re-running over an already-started ticket merges new repos
 into ``ticket.repos`` so the next ``execute_provision`` picks them up.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    issue_url      TEXT  [required]                                         │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --variant            TEXT                                                    │
│ --repos              TEXT                                                    │
│ --description        TEXT                                                    │
│ --help                     Show this message and exit.                       │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree workspace provision`

```
Usage: t3 teatree workspace provision [OPTIONS] [TICKET_ID]

 Provision every worktree in the current ticket workspace.

 Iterates ``ticket.worktrees`` and fires ``Worktree.provision()``
 for each. Stops at the first failure so the operator can fix
 the offending worktree before retrying. #941: an optional
 positional ``ticket_id`` is a no-op alias for PWD auto-detect
 (agents typed ``provision <id>`` from habit; typer used to reject it with
 rc=1).

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   ticket_id      [TICKET_ID]  Optional ticket id (alias for PWD auto-detect; │
│                               #941).                                         │
│                               [default: 0]                                   │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path                               TEXT  Worktree path inside the          │
│                                            workspace (auto-detects from      │
│                                            PWD).                             │
│ --slow-import    --no-slow-import          Allow slow DB fallbacks.          │
│                                            [default: no-slow-import]         │
│ --help                                     Show this message and exit.       │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree workspace start`

```
Usage: t3 teatree workspace start [OPTIONS]

 Start docker for every worktree in the current ticket workspace.

 Fires ``Worktree.start_services()`` on each worktree (CLI runs the
 runner synchronously). Each runner brings up docker-compose, which
 auto-maps host ports; the actual ports are then queried via
 ``docker compose port`` and stored on ``Worktree.extra["ports"]``.
 After every worktree starts, runs each overlay's readiness probes —
 exits 1 if any probe fails.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path inside the workspace (auto-detects from    │
│                     PWD).                                                    │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree workspace ready`

```
Usage: t3 teatree workspace ready [OPTIONS]

 Run readiness probes for every worktree in the ticket workspace.

 Strict: exits 0 iff every probe across every worktree passes. No
 per-worktree skip flag and no env-var escape — if a probe doesn't
 apply to a variant, the overlay's ``get_readiness_probes`` returns
 an empty list (or omits that probe) for that worktree.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path inside the workspace (auto-detects from    │
│                     PWD).                                                    │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree workspace teardown`

```
Usage: t3 teatree workspace teardown [OPTIONS]

 Tear down every worktree in the current ticket workspace.

 Fires ``Worktree.teardown()`` on each worktree. Continues past
 per-worktree failures to maximise cleanup; surfaces them in the
 final summary. Refuses to remove a worktree whose branch carries
 unpushed commits unless ``--force`` is passed.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path                   TEXT  Worktree path inside the workspace            │
│                                (auto-detects from PWD).                      │
│ --force    --no-force          Tear down even when a branch has commits not  │
│                                on any remote (data loss).                    │
│                                [default: no-force]                           │
│ --help                         Show this message and exit.                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree workspace finalize`

```
Usage: t3 teatree workspace finalize [OPTIONS] TICKET_ID

 Squash worktree commits into one, then rebase on the default branch.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    ticket_id      INTEGER  [required]                                      │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --message        TEXT                                                        │
│ --help                 Show this message and exit.                           │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree workspace doctor`

```
Usage: t3 teatree workspace doctor [OPTIONS]

 Detect state drift across every store; optionally fix it.

 Checks Django ↔ git worktrees, Postgres DBs, docker containers, redis
 slots, env cache files.  Without ``--fix`` prints drift; with
 ``--fix`` cleans orphan containers, drops orphan DBs, regenerates
 missing env caches, and prunes stale worktree dirs.  Every action
 uses :func:`run_checked` — no silent swallow.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --ticket                INTEGER  Reconcile just this ticket pk; 0 = all      │
│                                  tickets.                                    │
│                                  [default: 0]                                │
│ --fix       --no-fix             Apply fixes instead of just listing drift.  │
│                                  [default: no-fix]                           │
│ --help                           Show this message and exit.                 │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree workspace clean-merged`

```
Usage: t3 teatree workspace clean-merged [OPTIONS]

 Tear down every worktree whose ticket is already MERGED.

 On-demand reconciler for the daily followup sync. Use when merged-PR
 cleanup silently failed and stale docker containers, branches, or
 databases linger. Errors are surfaced inline — no suppression.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree workspace clean-all`

```
Usage: t3 teatree workspace clean-all [OPTIONS]

 Prune merged worktrees, stale branches, orphaned stashes, orphan databases,
 and old DSLR snapshots.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --keep-dslr        INTEGER  Number of DSLR snapshots to keep per tenant.     │
│                             [default: 1]                                     │
│ --help                      Show this message and exit.                      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree workspace list-orphans`

```
Usage: t3 teatree workspace list-orphans [OPTIONS]

 List orphan branches (commits ahead of origin/main AND no open PR) across the
 workspace.

 Used by the session-end hook and the ``workspace ticket`` warning to
 surface work that would otherwise be lost when a session closes or a
 new worktree is created. Emits a JSON-serialisable list — one entry
 per orphan.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree run`

```
Usage: t3 teatree run [OPTIONS] COMMAND [ARGS]...

 Run services.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ verify          Verify worktree state and return URLs.                       │
│ services        Return configured run commands.                              │
│ backend         Start the backend dev server.                                │
│ frontend        Start the frontend dev server.                               │
│ build-frontend  Build the frontend for production/testing.                   │
│ tests           Run the project test suite.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree run verify`

```
Usage: t3 teatree run verify [OPTIONS]

 Check that dev services respond via HTTP, then advance FSM.

 Discovers ports from running docker-compose containers via
 ``docker compose port``.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path (auto-detects from PWD if empty).          │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree run services`

```
Usage: t3 teatree run services [OPTIONS]

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path (auto-detects from PWD if empty).          │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree run backend`

```
Usage: t3 teatree run backend [OPTIONS]

 Start the backend via docker-compose. Host port is auto-mapped.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path (auto-detects from PWD if empty).          │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree run frontend`

```
Usage: t3 teatree run frontend [OPTIONS]

 Start the frontend dev server.
```

##### `t3 teatree run build-frontend`

```
Usage: t3 teatree run build-frontend [OPTIONS]

 Build the frontend app for production/testing.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path (auto-detects from PWD if empty).          │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree run tests`

```
Usage: t3 teatree run tests [OPTIONS]

 Run the project test suite.

 Extra arguments after ``--`` are appended to the test command
 (e.g. ``t3 <overlay> run tests -- path/to/test.py -k name``).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path (auto-detects from PWD if empty).          │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree e2e`

```
Usage: t3 teatree e2e [OPTIONS] COMMAND [ARGS]...

 E2E test commands.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ run         Run E2E tests — dispatches to project or external runner based   │
│             on overlay config.                                               │
│ trigger-ci  Trigger E2E tests on a remote CI pipeline.                       │
│ external    Run Playwright tests from the external test repo                 │
│             (T3_PRIVATE_TESTS).                                              │
│ project     Run E2E tests from the project's own test directory.             │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree e2e run`

```
Usage: t3 teatree e2e run [OPTIONS] [WORK_ITEM]

 Run E2E tests — the one command that works for every overlay.

 ``work_item`` (the #794 keystone) is a Ticket reference — a pk, an
 issue number, or an issue URL. When given, ``e2e run <work-item>``
 resolves the work item by its Ticket natural key, applies the default
 environment ladder, auto-provisions at the resolved ref, runs, and
 records ``{sha, result, timestamp}`` to the DB-durable recipe so a
 rerun never re-discovers prerequisites serially. ``--at
 last-green|main`` overrides the ladder. When ``work_item`` is empty
 the legacy cwd-resolved behaviour is unchanged.

 Otherwise dispatches to the ``project`` runner (in-repo
 pytest-playwright) or the ``external`` runner (remote playwright repo)
 based on what the overlay's ``get_e2e_config()`` returns. The overlay
 declares ``"runner": "project"`` or ``"runner": "external"``; when
 absent, ``test_dir`` implies ``project`` and ``project_path`` implies
 ``external`` for compatibility.

 ``--target dev|local`` selects the dual-env target and is forwarded to
 whichever runner handles the overlay (see ``external`` for semantics).

 Runner-specific flags (``--repo``, ``--playwright-args``) stay on the
 explicit ``external`` subcommand to keep this entry point overlay-agnostic.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   work_item      [WORK_ITEM]  Ticket reference (pk, issue number, or issue   │
│                               URL) — the #794 keystone.                      │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --test-path                                    TEXT                          │
│ --at                                           TEXT                          │
│ --target                                       TEXT                          │
│ --headed              --no-headed                    [default: no-headed]    │
│ --update-snapshots    --no-update-snapshots          [default:               │
│                                                      no-update-snapshots]    │
│ --docker              --no-docker                    [default: docker]       │
│ --help                                               Show this message and   │
│                                                      exit.                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree e2e trigger-ci`

```
Usage: t3 teatree e2e trigger-ci [OPTIONS]

 Trigger E2E tests on a remote CI pipeline.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --branch        TEXT                                                         │
│ --help                Show this message and exit.                            │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree e2e external`

```
Usage: t3 teatree e2e external [OPTIONS]

 Run Playwright tests from the external test repo (T3_PRIVATE_TESTS or --repo).

 Two sources for the Playwright working directory:

 - ``--repo <name>``: clone/update the named repo from ```` in
     ``~/.teatree.toml`` and use its ``e2e_dir`` subdirectory.
 - Default: resolve from ``T3_PRIVATE_TESTS`` env var or ``.private_tests``
     config key.

 ``--target dev|local`` selects the dual-env target deterministically:

 - ``dev``: keep the pre-set ``BASE_URL`` (deployed env), no port scan.
 - ``local``: always discover the local frontend, even if a stray
     ``BASE_URL`` is exported (``--target local`` never hits a
     deployed env silently).
 - empty: back-compat — infer ``dev`` if ``BASE_URL`` is set,
     else ``local``.

 The resolved value is exported as ``T3_E2E_TARGET`` so a dual-mode
 spec branches on ``process.env.T3_E2E_TARGET === 'dev'`` rather than
 re-deriving the target from a ``BASE_URL`` host regex.

 Discovers the frontend port from docker-compose (or local process)
 and reads the tenant variant from the env cache.

 Extra Playwright flags (--config, --timeout, --grep, etc.) can be
 passed via --playwright-args: ``--playwright-args="--config x.ts --timeout
 120000"``

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --test-path                                    TEXT                          │
│ --repo                                         TEXT                          │
│ --target                                       TEXT                          │
│ --headed              --no-headed                    [default: no-headed]    │
│ --update-snapshots    --no-update-snapshots          [default:               │
│                                                      no-update-snapshots]    │
│ --playwright-args                              TEXT                          │
│ --help                                               Show this message and   │
│                                                      exit.                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree e2e project`

```
Usage: t3 teatree e2e project [OPTIONS]

 Run E2E tests from the project's own test directory.

 ``--target dev|local`` is exported as ``T3_E2E_TARGET`` for the in-repo
 suite (same contract as the ``external`` runner); empty falls back to
 ``BASE_URL``-based inference.

 Pass ``--update-snapshots`` to regenerate ``pytest-playwright-visual``
 baselines. Always do this inside the Docker image (the default) — the
 CI runner's Chromium renders fonts at different heights than macOS, so
 locally-generated baselines mismatch in CI.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --test-path                                    TEXT                          │
│ --target                                       TEXT                          │
│ --headed              --no-headed                    [default: no-headed]    │
│ --docker              --no-docker                    [default: docker]       │
│ --update-snapshots    --no-update-snapshots          [default:               │
│                                                      no-update-snapshots]    │
│ --help                                               Show this message and   │
│                                                      exit.                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree db`

```
Usage: t3 teatree db [OPTIONS] COMMAND [ARGS]...

 Database operations.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ refresh          Re-import the worktree database from dump/DSLR.             │
│ restore-ci       Restore database from the latest CI dump.                   │
│ reset-passwords  Reset all user passwords to a known dev value.              │
│ query            Run a read-only SQL query against the control DB; emit rows │
│                  as JSON.                                                    │
│ shell            Drop into a Django shell against the resolved (gate)        │
│                  control DB.                                                 │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree db refresh`

```
Usage: t3 teatree db refresh [OPTIONS]

 Re-import the worktree database from DSLR snapshot or dump.

 Without --force: tries DSLR restore first (fast), then full reimport.
 With --force: drops existing DB first, then reimports from scratch.
 Use --dslr-snapshot to force a specific snapshot (skip auto-discovery).
 Use --dump-path to restore from a specific dump file.
 Use --fresh-dump to pull a fresh dump from the remote DEV env — this
 is the only sanctioned remote-dump path and it requires an explicit
 per-invocation approval (#777). The approval has two sanctioned
 channels of the same gate: a human at a TTY typing ``yes``, or a
 recorded single-use user ``DbApproval`` re-presented via
 --user-authorized <id> (#953). An unattended agent can never
 self-approve either channel.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path                                  TEXT  Worktree path (auto-detects    │
│                                               from PWD if empty).            │
│ --dslr-snapshot                         TEXT  Force a specific DSLR snapshot │
│                                               name.                          │
│ --dump-path                             TEXT  Path to a .pgsql dump file to  │
│                                               restore from.                  │
│ --force              --no-force               [default: no-force]            │
│ --fresh-dump         --no-fresh-dump          Pull a fresh dump from the     │
│                                               remote DEV environment for     │
│                                               this tenant. Requires explicit │
│                                               per-invocation approval on     │
│                                               every run.                     │
│                                               [default: no-fresh-dump]       │
│ --user-authorized                       TEXT  Id of the user who recorded an │
│                                               explicit DbApproval for this   │
│                                               exact op+tenant (#953). Lets a │
│                                               non-TTY caller satisfy the     │
│                                               #777 gate via the              │
│                                               recorded-approval channel;     │
│                                               consumed single-use and        │
│                                               audited. Empty ⇒               │
│                                               interactive-TTY approval is    │
│                                               required instead.              │
│ --help                                        Show this message and exit.    │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree db restore-ci`

```
Usage: t3 teatree db restore-ci [OPTIONS]

 Restore the worktree database from the latest CI dump.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path (auto-detects from PWD if empty).          │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree db reset-passwords`

```
Usage: t3 teatree db reset-passwords [OPTIONS]

 Reset all user passwords to a known dev value.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path (auto-detects from PWD if empty).          │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree db query`

```
Usage: t3 teatree db query [OPTIONS] SQL

 Run a read-only SQL query against the control DB; emit rows as JSON.

 The query runs through the live Django connection, so it resolves
 the *same* control DB the shipping gate reads. Canonical vs
 worktree-isolated is decided once, at settings-load time, by
 ``teatree.paths.CANONICAL_DB`` — there is no separate resolver to
 drift from ``pr create`` / ``lifecycle visit-phase``. This removes
 the ``manage.py shell -c "..."`` detour that forced weaker
 API-only introspection during handoffs (#774).

 Two-layer read-only enforcement (defense in depth):

 Layer 1 is a best-effort leading-keyword pre-filter: it rejects the
 obvious write/DDL cases early with a clear message — only a single
 ``SELECT``/``PRAGMA``/``EXPLAIN`` statement gets past it, and a
 ``PRAGMA`` setter (``=``) is rejected here too.

 Layer 2 is the binding guarantee: the statement runs inside an
 enforced read-only transaction (Postgres ``SET TRANSACTION READ
 ONLY``, SQLite ``PRAGMA query_only=ON``). A data-modifying CTE
 (``WITH t AS (DELETE … RETURNING …)``) or ``SELECT … INTO`` that
 slips past layer 1 is still blocked by the database itself —
 enforcement does not depend on parsing SQL.

 A write path needs a separate, explicitly-guarded command, never
 this one.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    sql      TEXT  [required]                                               │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree db shell`

```
Usage: t3 teatree db shell [OPTIONS]

 Drop into a Django shell against the resolved (gate) control DB.

 Delegates to Django's own ``shell`` so the same connection and
 worktree-isolated-vs-canonical DB the gate reads is reused — never
 a separately-resolved sqlite file (the #774 asymmetry that caused
 global ``t3`` and worktree ``manage.py`` to disagree).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree pr`

```
Usage: t3 teatree pr [OPTIONS] COMMAND [ARGS]...

 Pull request helpers.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ create         Create a pull request for the ticket's branch.                │
│ ensure-pr      Create a PR for an orphan branch (idempotent).                │
│ check-gates    Check whether session gates allow a phase transition.         │
│ fetch-issue    Fetch issue details from the configured tracker.              │
│ detect-tenant  Detect the current tenant variant from the overlay.           │
│ post-evidence  Post test evidence as a PR comment.                           │
│ sweep          List your open PRs across the forge for the /t3:sweeping-prs  │
│                skill.                                                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree pr create`

```
Usage: t3 teatree pr create [OPTIONS] TICKET_ID

 Validate ship gates and trigger the ship transition.

 Default (async): the ship is *queued* — ``execute_ship`` pushes the
 branch and opens the PR only when a worker drains the django-tasks
 queue. The result carries an explicit ``warning`` so a no-worker
 context does not look like a completed ship (#708).

 ``--sync``: run ``execute_ship`` inline in this process so the push
 and PR happen before the command returns — no worker required. Use
 this for interactive / ``uv run`` invocations where nothing is
 draining the queue.

 ``ticket_id`` accepts the internal DB pk, the full issue URL, or the
 bare issue number (resolved against ``Ticket.issue_url``).

 ``--title`` overrides the PR title (default: last commit subject).
 Stored on ``ticket.extra['pr_title_override']`` so the ship reads it.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    ticket_id      TEXT  [required]                                         │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --title                                      TEXT                            │
│ --dry-run            --no-dry-run                  [default: no-dry-run]     │
│ --skip-validation    --no-skip-validation          [default:                 │
│                                                    no-skip-validation]       │
│ --skip-visual-qa                             TEXT                            │
│ --sync               --no-sync                     [default: no-sync]        │
│ --help                                             Show this message and     │
│                                                    exit.                     │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree pr ensure-pr`

```
Usage: t3 teatree pr ensure-pr [OPTIONS]

 Create a PR for an orphan branch (idempotent, no-op when a PR already exists).

 An orphan is a branch with commits not on ``origin/main`` (after
 subject-match + tree-equality checks) and no open PR. When this
 runs inside a git pre-push hook for a *first* push, the branch is not
 yet on the remote — creating the PR is deferred with a warning so the
 push proceeds and the agent can re-run this command afterwards.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --branch        TEXT                                                         │
│ --repo          TEXT                                                         │
│ --help                Show this message and exit.                            │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree pr check-gates`

```
Usage: t3 teatree pr check-gates [OPTIONS] TICKET_ID

 Check whether session gates allow a phase transition.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    ticket_id      INTEGER  [required]                                      │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --target-phase        TEXT  [default: shipping]                              │
│ --help                      Show this message and exit.                      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree pr fetch-issue`

```
Usage: t3 teatree pr fetch-issue [OPTIONS] ISSUE_URL

 Fetch issue details with embedded image URLs and external links.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    issue_url      TEXT  [required]                                         │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree pr detect-tenant`

```
Usage: t3 teatree pr detect-tenant [OPTIONS]

 Detect the current tenant variant from the overlay.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree pr post-evidence`

```
Usage: t3 teatree pr post-evidence [OPTIONS] MR_IID

 Post test evidence as a PR comment. Uploads files and updates existing notes.

 Files (screenshots, videos) are uploaded and embedded as ``!(url)`` in the
 body.
 If an existing note contains ``## Test Plan``, it is updated instead of
 creating a new one.

 Gated by ``on_behalf_post_mode`` (#960, BLOCK under ``ask`` /
 ``draft_or_ask``): the call is refused with no upload or host side
 effect when no recorded :class:`OnBehalfApproval` matches
 ``(<repo>!<mr>, "post_evidence")``. The gate is inlined here (not
 at the ``code_host`` layer) so PR creation — which is not an
 on-behalf colleague-facing post — remains ungated.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    mr_iid      INTEGER  [required]                                         │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --repo         TEXT                                                          │
│ --title        TEXT  [default: Test Plan]                                    │
│ --body         TEXT                                                          │
│ --files        TEXT                                                          │
│ --help               Show this message and exit.                             │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree pr sweep`

```
Usage: t3 teatree pr sweep [OPTIONS]

 List all open PRs/MRs authored by the current user across the forge.

 Output is consumed by the ``/t3:sweeping-prs`` agent skill, which walks
 each PR sequentially: merges the default branch, fixes conflicts,
 monitors CI, and pushes — never rebases. The CLI itself only
 discovers; mutating actions live in the skill so the agent can
 prompt for non-default-base PRs and conflict resolution.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree tasks`

```
Usage: t3 teatree tasks [OPTIONS] COMMAND [ARGS]...

 Async task queue.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ cancel                Cancel a task by ID.                                   │
│ claim                 Claim the next available task.                         │
│ create                Enqueue the next-phase task for a ticket.              │
│ list                  List tasks with optional filters.                      │
│ start                 Claim and run the next interactive task in the current │
│                       terminal.                                              │
│ work-next-sdk         Claim and execute an headless task.                    │
│ work-next-user-input  Claim and execute a user input task.                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree tasks cancel`

```
Usage: t3 teatree tasks cancel [OPTIONS] TASK_ID

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    task_id      INTEGER  [required]                                        │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --confirm    --no-confirm      [default: no-confirm]                         │
│ --help                         Show this message and exit.                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree tasks claim`

```
Usage: t3 teatree tasks claim [OPTIONS]

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --execution-target        TEXT  [default: headless]                          │
│ --claimed-by              TEXT  [default: worker]                            │
│ --help                          Show this message and exit.                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree tasks create`

```
Usage: t3 teatree tasks create [OPTIONS] TICKET

 Enqueue the next-phase task for a ticket.

 Used by `/t3:next` to hand off from one phase to the next. Headless by default
 so a worker
 claims it immediately; pass `--interactive` for tasks that require human
 input.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    ticket      INTEGER  Ticket PK (see `ticket_id` in `tasks list`).       │
│                           [required]                                         │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --phase                              TEXT  Phase: scoping, coding, testing,  │
│                                            reviewing, shipping.              │
│ --reason                             TEXT  Prompt body for the worker. Use   │
│                                            '-' to read from stdin. Overrides │
│                                            --reason-file.                    │
│ --reason-file                        PATH  Read the prompt body from a file. │
│ --interactive    --no-interactive          Create an interactive task        │
│                                            instead of the default headless   │
│                                            one.                              │
│                                            [default: no-interactive]         │
│ --help                                     Show this message and exit.       │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree tasks list`

```
Usage: t3 teatree tasks list [OPTIONS]

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --status                  TEXT  Filter by status                             │
│ --execution-target        TEXT  Filter by execution target                   │
│ --help                          Show this message and exit.                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree tasks start`

```
Usage: t3 teatree tasks start [OPTIONS] [TASK_ID]

 Claim an interactive task and exec ``claude`` in the current terminal.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   task_id      [TASK_ID]  Task ID; omit to start the next pending            │
│                           interactive task.                                  │
│                           [default: 0]                                       │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --claimed-by        TEXT  Worker identifier stored on the claim.             │
│                           [default: cli]                                     │
│ --help                    Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree tasks work-next-sdk`

```
Usage: t3 teatree tasks work-next-sdk [OPTIONS]

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --claimed-by        TEXT  [default: worker]                                  │
│ --help                    Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree tasks work-next-user-input`

```
Usage: t3 teatree tasks work-next-user-input [OPTIONS]

 Claim and execute a user input task.
```

#### `t3 teatree followup`

```
Usage: t3 teatree followup [OPTIONS] COMMAND [ARGS]...

 Follow-up snapshots.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ refresh       Return counts of tickets and tasks.                            │
│ sync          Synchronize followup data from MRs.                            │
│ discover-mrs  List the user's open non-draft PRs/MRs awaiting a review       │
│               request.                                                       │
│ remind        Return list of pending user input tasks.                       │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree followup refresh`

```
Usage: t3 teatree followup refresh [OPTIONS]

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree followup sync`

```
Usage: t3 teatree followup sync [OPTIONS]

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree followup discover-mrs`

```
Usage: t3 teatree followup discover-mrs [OPTIONS]

 List the user's open, non-draft PRs/MRs awaiting a review request.

 Backs ``t3 review-request discover`` (BLUEPRINT.md §10.1). Mirrors
 ``glab api /merge_requests?scope=created_by_me&state=opened``
 filtered to non-draft MRs; each entry carries ``repo``, ``iid``,
 ``title`` and ``url`` so the result is suitable for the
 review-request batch ping or a human paste into Slack.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree followup remind`

```
Usage: t3 teatree followup remind [OPTIONS]

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree standup`

```
Usage: t3 teatree standup [OPTIONS] COMMAND [ARGS]...

 Auto-generated daily update (read-only).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ generate  Generate a standup from transition + attempt data (read-only).     │
│ stale     List tickets with no activity past the staleness threshold         │
│           (read-only).                                                       │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree standup generate`

```
Usage: t3 teatree standup generate [OPTIONS]

 Generate the standup from existing transition + attempt data (read-only).

 Returns ``{since, yesterday, blockers, markdown}`` — ``markdown`` is
 the pre-rendered human view alongside the structured rows.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --days         INTEGER  Window size in days (default: last business day).    │
│                         [default: 1]                                         │
│ --since        TEXT     ISO timestamp override for the window start.         │
│ --help                  Show this message and exit.                          │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree standup stale`

```
Usage: t3 teatree standup stale [OPTIONS]

 List tickets with no activity past the threshold (read-only).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --days        INTEGER  Inactivity threshold in days. [default: 3]            │
│ --help                 Show this message and exit.                           │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree lifecycle`

```
Usage: t3 teatree lifecycle [OPTIONS] COMMAND [ARGS]...

 Session lifecycle and phase tracking.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ visit-phase   Mark a phase as visited on the ticket's latest session.        │
│ clear-ledger  Clear a reused ticket's stale phase ledger (sanctioned         │
│               session-retire).                                               │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree lifecycle visit-phase`

```
Usage: t3 teatree lifecycle visit-phase [OPTIONS] TICKET_ID PHASE

 Mark a phase as visited and advance the ticket FSM if applicable.

 ``ticket_id`` accepts the same identifier set as ``pr create`` — DB
 pk, forge issue number, or full issue URL (#694). The phase is
 normalized to the canonical vocabulary so both the short verbs the
 skills emit (``code``, ``test``, ``review``, ``ship``, ``retro``,
 ``scope``) and the older gerunds advance the FSM. The resulting
 ``ticket.state`` is included in the output so a skipped or refused
 transition is visible rather than silently swallowed.

 ``--agent-id`` records the recording agent's identity into the
 ``phase_visits`` audit trail. Resolution is delegated to
 ``Session.recording_identity`` so the attribution is **never
 empty** even when neither ``--agent-id`` nor ``Session.agent_id``
 is set.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    ticket_id      TEXT  [required]                                         │
│ *    phase          TEXT  [required]                                         │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --agent-id        TEXT  Recording agent identity stamped into phase_visits   │
│                         (audit trail).                                       │
│ --help                  Show this message and exit.                          │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree lifecycle clear-ledger`

```
Usage: t3 teatree lifecycle clear-ledger [OPTIONS] TICKET_ID

 Clear a reused ticket's stale phase ledger (sanctioned session-retire).

 §17.6 enforcement candidate (9): reused tickets accumulate a stale
 phase ledger from a prior workstream — the shipping gate then sees a
 passing aggregate that no longer reflects the new work (the
 anti-vacuous attestation gap). Hand-editing ``phase_visits`` /
 ``visited_phases`` was the only escape, which is exactly the
 out-of-band state mutation invariant 8 prohibits. This is the
 sanctioned ``t3`` path: it retires every session's phase ledger for
 the ticket in one transaction so the next workstream re-earns its
 attestations from scratch. Requires ``--confirm`` (destructive).

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    ticket_id      TEXT  [required]                                         │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --confirm    --no-confirm      Required: confirm the destructive             │
│                                phase-ledger clear.                           │
│                                [default: no-confirm]                         │
│ --help                         Show this message and exit.                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree env`

```
Usage: t3 teatree env [OPTIONS] COMMAND [ARGS]...

 Inspect and mutate the worktree env cache.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ show             Print the env cache as the DB would render it.              │
│ set-var          Persist an override on the worktree and refresh the cache.  │
│ unset            Delete an override row and refresh the cache.               │
│ overrides        List user-declared overrides for this worktree.             │
│ check            Exit non-zero if the on-disk cache diverges from the DB     │
│                  render.                                                     │
│ migrate-secrets  Move POSTGRES_PASSWORD literals out of .t3-env.cache into   │
│                  pass.                                                       │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree env show`

```
Usage: t3 teatree env show [OPTIONS]

 Print the current env as the DB would render it.

 Never reads the cache file — always renders fresh from the DB.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path          TEXT  Worktree path (auto-detects from PWD if empty).        │
│ --format        TEXT  shell | json [default: shell]                          │
│ --help                Show this message and exit.                            │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree env set-var`

```
Usage: t3 teatree env set-var [OPTIONS] KEY_VALUE

 Persist an override on the worktree and refresh the cache.

 Rejects keys owned by core (edit the model field instead).

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    key_value      TEXT  KEY=VALUE. [required]                              │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path (auto-detects from PWD if empty).          │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree env unset`

```
Usage: t3 teatree env unset [OPTIONS] KEY

 Delete an override row and refresh the cache.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    key      TEXT  Override key to remove. [required]                       │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path (auto-detects from PWD if empty).          │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree env overrides`

```
Usage: t3 teatree env overrides [OPTIONS]

 List user-declared overrides for this worktree.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path (auto-detects from PWD if empty).          │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree env check`

```
Usage: t3 teatree env check [OPTIONS]

 Exit non-zero if the on-disk cache diverges from the DB render.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path (auto-detects from PWD if empty).          │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree env migrate-secrets`

```
Usage: t3 teatree env migrate-secrets [OPTIONS]

 Move ``POSTGRES_PASSWORD`` literals out of ``.t3-env.cache`` into ``pass``.

 For each targeted worktree this command:

 1. Reads the literal ``POSTGRES_PASSWORD=`` line from the on-disk cache.
 2. Stores it in ``pass`` under the canonical key for that worktree.
 3. Regenerates the cache so it now contains only the symbolic
     ``POSTGRES_PASSWORD_PASS_KEY`` reference.

 Idempotent — caches that already lack a literal are reported as
 ``already migrated`` and left alone.  Exits 0 when every targeted
 worktree finished successfully, non-zero when at least one needs
 attention (no pass installed, cache missing, etc.).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path (migrates only this worktree). Empty =     │
│                     migrate every worktree.                                  │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree ticket`

```
Usage: t3 teatree ticket [OPTIONS] COMMAND [ARGS]...

 Ticket state management.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ transition        Transition a ticket to a new state.                        │
│ clear             Issue a per-diff CLEAR — the orchestrator's only merge     │
│                   output (BLUEPRINT §17.4.2).                                │
│ merge             Execute the IN_REVIEW → MERGED keystone transition         │
│                   (BLUEPRINT §17.4).                                         │
│ list              List tickets, optionally filtered by state and/or overlay. │
│ sync-completions  Check post-ship tickets against upstream issues and        │
│                   advance completed ones.                                    │
│ comment           Post a comment to an issue or work item by its URL.        │
│ context           Durable per-ticket knowledge store: show / add / edit      │
│                   (#627).                                                    │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree ticket transition`

```
Usage: t3 teatree ticket transition [OPTIONS] TICKET_ID TRANSITION_NAME

 Transition a ticket to a new state.

 Accepts any of the allowed transition names: scope, start, code, test,
 review, ship, request_review, mark_merged, retrospect, mark_delivered,
 rework, mark_review_no_action.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    ticket_id            INTEGER  [required]                                │
│ *    transition_name      TEXT     [required]                                │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree ticket clear`

```
Usage: t3 teatree ticket clear [OPTIONS] PR_ID SLUG REVIEWED_SHA

 Issue a per-diff CLEAR — the orchestrator's only merge output (BLUEPRINT
 §17.4.2).

 Records the orchestrator's reviewed/verified judgment as a durable
 ``MergeClear`` row the durable loop later acts on by id via
 ``ticket merge``. This is the missing issuance seam: #863 added the
 consume side but no command created the row. The CLEAR is the
 compaction-surviving handoff — the orchestrator may be restarted
 before the loop picks it up, so it lives in the DB, not a session
 file.

 §17.8 clause 3 is enforced here: ``--reviewer-identity`` must name an
 independent cold reviewer — a maker/coding-agent/loop role is refused
 (the author cannot rubber-stamp their own CLEAR). ``reviewed_sha``
 must be a hex commit id (not a branch ref) so the loop can bind the
 merge to the exact reviewed tree.

 ``--human-authorize`` is valid ONLY with ``--blast-class substrate``:
 it records *who approved* a substrate merge (the gate) so the
 otherwise approval-gated / draft-locked substrate change can be
 merged BY THE AGENT through the SAME sanctioned ``ticket merge``
 transition (invariant 8 — never raw ``gh``, never a human-performed
 merge), with the human approval durably on the CLEAR.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    pr_id             INTEGER  [required]                                   │
│ *    slug              TEXT     [required]                                   │
│ *    reviewed_sha      TEXT     [required]                                   │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --reviewer-identity              TEXT     Independent cold reviewer identity │
│                                           (NOT a maker/coding-agent/loop     │
│                                           role — §17.8 clause 3).            │
│ --gh-verify-result               TEXT     Audit-only snapshot of gh checks   │
│                                           at review time: green / pending /  │
│                                           failed.                            │
│                                           [default: green]                   │
│ --blast-class                    TEXT     Orchestrator judgment: substrate / │
│                                           logic / docs (§17.4.2).            │
│                                           [default: logic]                   │
│ --ticket-id                      INTEGER  Optional teatree Ticket id this    │
│                                           CLEAR authorises the merge for.    │
│                                           [default: 0]                       │
│ --human-authorize                TEXT     ONLY for blast_class=substrate:    │
│                                           the human/owner id authorising the │
│                                           substrate merge.                   │
│ --executing-loop-identity        TEXT     The loop that will execute the     │
│                                           merge; the reviewer must differ    │
│                                           (§17.8 clause 3).                  │
│                                           [default: merge-loop]              │
│ --help                                    Show this message and exit.        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree ticket merge`

```
Usage: t3 teatree ticket merge [OPTIONS] CLEAR_ID

 Execute the missing IN_REVIEW → MERGED keystone transition (BLUEPRINT §17.4).

 The ONLY sanctioned merge path. Raw ``gh pr merge`` / ``glab mr
 merge`` is mechanically refused on teatree-managed tickets (the
 prohibition guard in ``hook_router``); they bypass the ledger
 update, attestation binding, and ``mark_merged()`` and leave the
 FSM incoherent.

 Pre-condition (§17.4.3): a valid, actionable ``MergeClear`` (CLI
 arg ``clear_id``), CI green on the exact PR head, an independent
 cold-review CLEAR (``reviewer_identity`` != ``--loop-identity``),
 SHA-match, not-draft, and ``blast_class`` != substrate. The merge
 is bound to ``expected_head_oid`` and fails closed on head drift.
 Post hook: atomic CLEAR-consume + ``MergeAudit`` + attestation
 binding + ``ticket.mark_merged()``.

 ``--human-authorized`` is the sanctioned substrate approval path
 (invariant 8): the loop NEVER auto-merges substrate, but the recorded
 human approval id (set on the CLEAR via ``ticket clear …
 --human-authorize``) is re-presented here and **the agent executes**
 the substrate merge through THIS SAME transition — not raw ``gh``,
 never a human-performed merge (approval is the gate, the agent is the
 executor). It cannot unlock a non-substrate CLEAR, so it can never
 bypass independent loop review of logic/docs.

 On a pre-condition failure the FSM is left untouched and the
 result is flagged ``escalated`` so the durable backlog re-escalation
 is visible (the loop never self-issues a replacement CLEAR).

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    clear_id      INTEGER  [required]                                       │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --loop-identity           TEXT  Identity of the executing loop (must differ  │
│                                 from the CLEAR reviewer — §17.8 clause 3).   │
│                                 [default: merge-loop]                        │
│ --human-authorized        TEXT  Substrate-only: the recorded human           │
│                                 authoriser id, re-presented to merge a       │
│                                 substrate CLEAR.                             │
│ --help                          Show this message and exit.                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree ticket list`

```
Usage: t3 teatree ticket list [OPTIONS]

 List tickets, optionally filtered by state and/or overlay.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --state          TEXT                                                        │
│ --overlay        TEXT                                                        │
│ --help                 Show this message and exit.                           │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree ticket sync-completions`

```
Usage: t3 teatree ticket sync-completions [OPTIONS]

 Check post-ship tickets against upstream issues and advance completed ones.

 Walks tickets in shipped/in_review/merged states, calls the overlay's
 ``is_issue_done()`` for each, and transitions completed tickets toward
 delivered. Use ``--dry-run`` to preview without touching state.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --dry-run    --no-dry-run      Show what would transition without acting.    │
│                                [default: no-dry-run]                         │
│ --help                         Show this message and exit.                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree ticket comment`

```
Usage: t3 teatree ticket comment [OPTIONS] ISSUE_URL

 Post a comment to an issue or work item by its URL.

 Resolves the code host per-URL across all registered overlays, so it
 works for any tracker an overlay is configured for (GitLab issues and
 work items, GitHub issues). Pass the body inline with ``--body`` or
 from a file with ``--body-file``.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    issue_url      TEXT  [required]                                         │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --body             TEXT  Comment body text.                                  │
│ --body-file        TEXT  Path to a file containing the comment body.         │
│ --help                   Show this message and exit.                         │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree ticket context`

```
Usage: t3 teatree ticket context [OPTIONS] COMMAND [ARGS]...

 Durable per-ticket knowledge store (#627).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ show  Print the ticket's durable context store.                              │
│ add   Append a timestamped ``<key>: <value>`` line to the context store.     │
│ edit  Open the full context store in ``$EDITOR`` and replace it.             │
╰──────────────────────────────────────────────────────────────────────────────╯
```

###### `t3 teatree ticket context show`

```
Usage: t3 teatree ticket context show [OPTIONS] TICKET_ID

 Print the ticket's durable context store.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    ticket_id      INTEGER  [required]                                      │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

###### `t3 teatree ticket context add`

```
Usage: t3 teatree ticket context add [OPTIONS] TICKET_ID ENTRY

 Append a timestamped ``<key>: <value>`` line to the context store.

 Append-only: parallel sessions never overwrite each other (open
 question 2). A blank entry is refused with a nonzero exit.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    ticket_id      INTEGER  [required]                                      │
│ *    entry          TEXT     [required]                                      │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

###### `t3 teatree ticket context edit`

```
Usage: t3 teatree ticket context edit [OPTIONS] TICKET_ID

 Open the full context store in ``$EDITOR`` and replace it.

 Unlike ``add``, ``edit`` is a full-field rewrite — for pruning stale
 entries or restructuring. An aborted edit (editor exits without
 saving) leaves the store untouched.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    ticket_id      INTEGER  [required]                                      │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree availability`

```
Usage: t3 teatree availability [OPTIONS] COMMAND [ARGS]...

 24/7 dual question-mode (#58, BLUEPRINT §17.1 invariant 9).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ away     Set manual away-mode override (questions queue as DeferredQuestion  │
│          rows).                                                              │
│ present  Set manual present-mode override (questions ask interactively).     │
│ auto     Clear manual override and fall back to schedule/default.            │
│ show     Print the currently resolved mode and source                        │
│          (override/schedule/default).                                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree availability away`

```
Usage: t3 teatree availability away [OPTIONS]

 Force away-mode (deferred questions) until *until* — or forever.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --until        TEXT  ISO8601 timestamp when the override expires (e.g.       │
│                      2026-05-19T18:00:00+02:00).                             │
│ --help               Show this message and exit.                             │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree availability present`

```
Usage: t3 teatree availability present [OPTIONS]

 Force present-mode (interactive questions) until *until* — or forever.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --until        TEXT  ISO8601 timestamp when the override expires.            │
│ --help               Show this message and exit.                             │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree availability auto`

```
Usage: t3 teatree availability auto [OPTIONS]

 Clear the manual override; the cron schedule decides again.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree availability show`

```
Usage: t3 teatree availability show [OPTIONS]

 Print the current resolved mode and which layer decided it.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree questions`

```
Usage: t3 teatree questions [OPTIONS] COMMAND [ARGS]...

 Manage the away-mode deferred-question backlog (#58).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ record   Record a deferred question (used by the PreToolUse away-mode hook). │
│ list     List pending deferred questions, oldest first.                      │
│ answer   Resolve a pending question with a user answer.                      │
│ dismiss  Dismiss a pending question without answering it.                    │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree questions record`

```
Usage: t3 teatree questions record [OPTIONS] QUESTION

 Record a deferred question (called by the PreToolUse away-mode hook).

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    question      TEXT  The question text. [required]                       │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --options            TEXT  Verbatim JSON-encoded ``AskUserQuestion``         │
│                            options.                                          │
│ --session            TEXT  Originating session id.                           │
│ --tool-use-id        TEXT  Originating tool_use id.                          │
│ --help                     Show this message and exit.                       │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree questions list`

```
Usage: t3 teatree questions list [OPTIONS]

 List pending deferred questions, oldest first.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --all     --pending      Include answered/dismissed rows. [default: pending] │
│ --help                   Show this message and exit.                         │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree questions answer`

```
Usage: t3 teatree questions answer [OPTIONS] QUESTION_ID TEXT

 Resolve a pending question with a user answer.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    question_id      INTEGER  [required]                                    │
│ *    text             TEXT     The user's answer. [required]                 │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --resolver        TEXT  Identity of the resolver (audit trail).              │
│ --help                  Show this message and exit.                          │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree questions dismiss`

```
Usage: t3 teatree questions dismiss [OPTIONS] QUESTION_ID

 Dismiss a pending question without answering it.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    question_id      INTEGER  [required]                                    │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --reason          TEXT  Why the question is being dropped (audit trail).     │
│                         [default: no longer relevant]                        │
│ --resolver        TEXT  Identity of the resolver (audit trail).              │
│ --help                  Show this message and exit.                          │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree pending_chat`

```
Usage: t3 teatree pending_chat [OPTIONS] COMMAND [ARGS]...

 Manage the inbound Slack-DM queue (#1063).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ list           List inbound rows from the last hour (or --all).              │
│ mark-answered  Stamp ``answered_at`` on rows matching a Slack ts.            │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree pending_chat list`

```
Usage: t3 teatree pending_chat list [OPTIONS]

 List inbound Slack-DM rows; the last hour by default.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --all     --recent      Include rows older than 1h; default is last hour     │
│                         only.                                                │
│                         [default: recent]                                    │
│ --help                  Show this message and exit.                          │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree pending_chat mark-answered`

```
Usage: t3 teatree pending_chat mark-answered [OPTIONS] SLACK_TS

 Stamp ``answered_at = now`` on rows matching ``(overlay, slack_ts)``.

 Idempotent: zero rows is a successful no-op (the second call
 sees the row already stamped). Empty ``slack_ts`` is rejected.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    slack_ts      TEXT  The Slack ts of the question being answered.        │
│                          [required]                                          │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --overlay        TEXT  Scope the stamp to one overlay (default: empty / v1   │
│                        single-overlay).                                      │
│ --help                 Show this message and exit.                           │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 teatree notify`

```
Usage: t3 teatree notify [OPTIONS] COMMAND [ARGS]...

 Bot→user Slack DM from the shell (#1030).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ send  DM the user; exit 0 on delivery, 1 otherwise (sub-agent direct         │
│       notify).                                                               │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree notify send`

```
Usage: t3 teatree notify send [OPTIONS] BODY

 Send a bot→user Slack DM (exit 0 on delivery, 1 otherwise).

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    body      TEXT  Slack mrkdwn body. Use ``-`` to read the body from      │
│                      stdin.                                                  │
│                      [required]                                              │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ *  --idempotency-key        TEXT  Required dedupe key (the helper enforces   │
│                                   it).                                       │
│                                   [required]                                 │
│    --user-id                TEXT  Slack user id to DM (defaults to the       │
│                                   configured user).                          │
│    --kind                   TEXT  Notification kind: info | answer |         │
│                                   question.                                  │
│                                   [default: info]                            │
│    --overlay                TEXT  Set T3_OVERLAY_NAME for the call           │
│                                   (per-overlay bot routing).                 │
│    --help                         Show this message and exit.                │
╰──────────────────────────────────────────────────────────────────────────────╯
```
