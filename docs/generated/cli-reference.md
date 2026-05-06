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
│ assess          Codebase health assessment.                                  │
│ overlay         Dev-mode overlay install/uninstall.                          │
│ infra           Teatree-wide infrastructure services.                        │
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
│ delete-draft-note    Delete a draft note from a GitLab MR.                   │
│ publish-draft-notes  Publish all draft notes on a GitLab MR (bulk submit).   │
│ list-draft-notes     List draft notes on a GitLab MR.                        │
│ reply-to-discussion  Reply to a GitLab MR discussion thread (immediate, not  │
│                      draft).                                                 │
│ update-note          Update a note on a GitLab MR — auto-detects draft vs    │
│                      published.                                              │
│ resolve-discussion   Mark a GitLab MR discussion thread resolved or          │
│                      unresolved.                                             │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 review post-draft-note`

```
Usage: t3 review post-draft-note [OPTIONS] REPO MR NOTE

 Post a draft note on a GitLab MR (inline or general).

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
│ --help                 Show this message and exit.                           │
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

### `t3 review-request`

```
Usage: t3 review-request [OPTIONS] COMMAND [ARGS]...

 Batch review requests.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ discover  Discover open merge requests awaiting review.                      │
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

### `t3 doctor`

```
Usage: t3 doctor [OPTIONS] COMMAND [ARGS]...

 Smoke-test hooks, imports, services.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ check  Verify imports, required tools, and editable-install sanity.          │
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
│ analyze-video    Decompose video into frames for AI analysis.                │
│ bump-deps        Bump pyproject.toml dependencies from uv.lock.              │
│ sonar-check      Run local SonarQube analysis via Docker.                    │
│ label-issues     Suggest labels for unlabeled open issues by                 │
│                  keyword-matching title and body.                            │
│ find-duplicates  Flag pairs of open issues with near-identical titles.       │
│ claude-handover  Show Claude handover telemetry and runtime recommendations. │
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

### `t3 setup`

```
Usage: t3 setup [OPTIONS] COMMAND [ARGS]...

 First-time setup and global skill management.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --claude-scope        TEXT  Claude plugin install scope: user or project.    │
│                             [default: user]                                  │
│ --skip-plugin               Skip Claude CLI plugin registration.             │
│ --help                      Show this message and exit.                      │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ slack-bot  Register a per-overlay Slack bot and store its tokens via         │
│            ``pass``.                                                         │
╰──────────────────────────────────────────────────────────────────────────────╯
```

#### `t3 setup slack-bot`

```
Usage: t3 setup slack-bot [OPTIONS]

 Register a per-overlay Slack bot and store its tokens via ``pass``.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ *  --overlay                TEXT  Overlay name as registered in              │
│                                   `~/.teatree.toml`.                         │
│                                   [required]                                 │
│    --reset                        Rotate the existing bot + app tokens; skip │
│                                   the manifest URL.                          │
│    --skip-smoke-test              Skip the round-trip DM verification.       │
│    --config                 PATH  Path to teatree config (default:           │
│                                   ~/.teatree.toml).                          │
│                                   [default: /Users/adrien/.teatree.toml]     │
│    --help                         Show this message and exit.                │
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

### `t3 teatree`

```
Usage: t3 teatree [OPTIONS] COMMAND [ARGS]...

 Commands for the t3-teatree overlay.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ resetdb      Drop the SQLite database and re-run all migrations.             │
│ worker       Start background task workers.                                  │
│ full-status  Show ticket, worktree, and session state summary.               │
│ ship         Code to MR — create merge request for the ticket.               │
│ daily        Daily followup — sync MRs, check gates, remind reviewers.       │
│ agent        Launch Claude Code with overlay context and auto-detected       │
│              skills.                                                         │
│ config       Overlay configuration.                                          │
│ worktree     Per-worktree FSM operations.                                    │
│ workspace    Ticket-level workspace operations (every worktree in the        │
│              ticket).                                                        │
│ run          Run services.                                                   │
│ e2e          E2E test commands.                                              │
│ db           Database operations.                                            │
│ pr           Pull request helpers.                                           │
│ tasks        Async task queue.                                               │
│ followup     Follow-up snapshots.                                            │
│ lifecycle    Session lifecycle and phase tracking.                           │
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

 Code to MR — create merge request for the ticket.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    ticket_id      TEXT  Ticket ID [required]                               │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --title        TEXT  MR title                                                │
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
 Allocates free host ports, refreshes the env cache, runs overlay
 pre-run steps, then ``docker compose up -d``. After the runner
 succeeds, runs the overlay's readiness probes — exits 1 if any fail.

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
 into a single canonical path.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path (auto-detects from PWD if empty).          │
│ --help              Show this message and exit.                              │
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
Usage: t3 teatree workspace provision [OPTIONS]

 Provision every worktree in the current ticket workspace.

 Iterates ``ticket.worktrees`` and fires ``Worktree.provision()``
 for each. Each transition enqueues its worker via on_commit; the
 runner also runs synchronously so the operator gets streaming
 feedback. Stops at the first failure so the operator can fix the
 offending worktree before retrying.

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

 Allocates one shared port set across the workspace, then fires
 ``Worktree.start_services()`` on each worktree (CLI runs the
 runner synchronously). After every worktree starts, runs each
 overlay's readiness probes — exits 1 if any probe fails.

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
 final summary.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path inside the workspace (auto-detects from    │
│                     PWD).                                                    │
│ --help              Show this message and exit.                              │
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

 On-demand reconciler for the daily followup sync. Use when merged-MR
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

 Start the backend via docker-compose.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path (auto-detects from PWD if empty).          │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree run frontend`

```
Usage: t3 teatree run frontend [OPTIONS]

 Start the frontend dev server on the host.

 Angular's nx serve needs 6GB+ RAM which exceeds typical Docker memory
 limits. The frontend always runs on the host; backend/redis stay in Docker.
 In CI, use build-frontend + nginx instead (see docker-compose.e2e.yml).

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path        TEXT  Worktree path (auto-detects from PWD if empty).          │
│ --help              Show this message and exit.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
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
Usage: t3 teatree e2e run [OPTIONS]

 Run E2E tests — the one command that works for every overlay.

 Dispatches to the ``project`` runner (in-repo pytest-playwright) or the
 ``external`` runner (remote playwright repo) based on what the overlay's
 ``get_e2e_config()`` returns. The overlay declares ``"runner": "project"``
 or ``"runner": "external"``; when absent, ``test_dir`` implies ``project``
 and ``project_path`` implies ``external`` for compatibility.

 Runner-specific flags (``--repo``, ``--playwright-args``) stay on the
 explicit ``external`` subcommand to keep this entry point overlay-agnostic.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --test-path                                    TEXT                          │
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

 Discovers the frontend port from docker-compose (or local process)
 and reads the tenant variant from the env cache.

 Extra Playwright flags (--config, --timeout, --grep, etc.) can be
 passed via --playwright-args: ``--playwright-args="--config x.ts --timeout
 120000"``

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --test-path                                    TEXT                          │
│ --repo                                         TEXT                          │
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

 Pass ``--update-snapshots`` to regenerate ``pytest-playwright-visual``
 baselines. Always do this inside the Docker image (the default) — the
 CI runner's Chromium renders fonts at different heights than macOS, so
 locally-generated baselines mismatch in CI.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --test-path                                    TEXT                          │
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

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --path                           TEXT  Worktree path (auto-detects from PWD  │
│                                        if empty).                            │
│ --dslr-snapshot                  TEXT  Force a specific DSLR snapshot name.  │
│ --dump-path                      TEXT  Path to a .pgsql dump file to restore │
│                                        from.                                 │
│ --force            --no-force          [default: no-force]                   │
│ --help                                 Show this message and exit.           │
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

#### `t3 teatree pr`

```
Usage: t3 teatree pr [OPTIONS] COMMAND [ARGS]...

 Pull request helpers.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ create         Create a merge request for the ticket's branch.               │
│ ensure-pr      Create a PR for an orphan branch (idempotent).                │
│ check-gates    Check whether session gates allow a phase transition.         │
│ fetch-issue    Fetch issue details from the configured tracker.              │
│ detect-tenant  Detect the current tenant variant from the overlay.           │
│ post-evidence  Post test evidence as an MR comment.                          │
│ sweep          List your open PRs across the forge for the /t3:sweeping-prs  │
│                skill.                                                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree pr create`

```
Usage: t3 teatree pr create [OPTIONS] TICKET_ID

 Validate ship gates and trigger the ship transition.

 On success the ``execute_ship`` worker pushes the branch, opens the MR,
 and advances ``SHIPPED → IN_REVIEW``. The return value reports the MR
 URL once the worker completes (synchronous in interactive mode).

 ``ticket_id`` accepts the internal DB pk, the full issue URL, or the
 bare issue number (resolved against ``Ticket.issue_url``).

 ``--title`` overrides the MR title (default: last commit subject).
 Stored on ``ticket.extra['mr_title_override']`` so the worker reads it.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    ticket_id      TEXT  [required]                                         │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --title                                      TEXT                            │
│ --dry-run            --no-dry-run                  [default: no-dry-run]     │
│ --skip-validation    --no-skip-validation          [default:                 │
│                                                    no-skip-validation]       │
│ --skip-visual-qa                             TEXT                            │
│ --help                                             Show this message and     │
│                                                    exit.                     │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree pr ensure-pr`

```
Usage: t3 teatree pr ensure-pr [OPTIONS]

 Create a PR for an orphan branch (idempotent, no-op when a PR already exists).

 An orphan is a branch with commits not on ``origin/main`` (after
 subject-match + tree-equality checks) and no open PR/MR. When this
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

 Post test evidence as an MR comment. Uploads files and updates existing notes.

 Files (screenshots, videos) are uploaded and embedded as ``!(url)`` in the
 body.
 If an existing note contains ``## Test Plan``, it is updated instead of
 creating a new one.

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
│ refresh  Return counts of tickets and tasks.                                 │
│ sync     Synchronize followup data from MRs.                                 │
│ remind   Return list of pending user input tasks.                            │
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

##### `t3 teatree followup remind`

```
Usage: t3 teatree followup remind [OPTIONS]

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
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
│ visit-phase  Mark a phase as visited on the ticket's latest session.         │
╰──────────────────────────────────────────────────────────────────────────────╯
```

##### `t3 teatree lifecycle visit-phase`

```
Usage: t3 teatree lifecycle visit-phase [OPTIONS] TICKET_ID PHASE

 Mark a phase as visited on the ticket's latest session.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    ticket_id      INTEGER  [required]                                      │
│ *    phase          TEXT     [required]                                      │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```
