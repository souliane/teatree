# GitLab Platform Reference

> Recipes for GitLab-specific operations. Skills reference this file via `See platforms/gitlab.md § <section>`.

---

## Self-Hosted GitLab

TeaTree supports self-hosted GitLab instances. Set the base URL via the overlay config:

```python
# overlay_settings.py
GITLAB_URL = "https://gitlab.example.com/api/v4"
```

Or in `~/.teatree.toml`:

```toml
[overlays.my-overlay]
gitlab_url = "https://gitlab.example.com/api/v4"
```

The default is `https://gitlab.com/api/v4`. All URL parsing (MR links, issue links, Slack review matching) works with any GitLab hostname.

## CLI Tool

`glab` — the GitLab CLI. Install via `brew install glab` or see [glab docs](https://gitlab.com/gitlab-org/cli).

## User Identity

### Get Authenticated User ID

When using the REST API directly (not `glab`), get the current user's ID from `/api/v4/user`:

```bash
TOKEN=$(glab config get token --host gitlab.com)
MY_ID=$(curl -s -H "PRIVATE-TOKEN: $TOKEN" "https://gitlab.com/api/v4/user" | jq '.id')
```

**Never use `members/all?search=<name>`** to look up your own user ID — search results are not ordered by relevance and may return a different user with a similar name.

## Authentication

### Token Extraction

**Preferred method** — clean output, no parsing needed:

```bash
TOKEN=$(glab config get token --host gitlab.com)
```

**Fallback** — `glab auth status -t` prints the token to **stderr** mixed with other output. Extract reliably:

```bash
TOKEN=$(glab auth status -t 2>&1 | grep -o 'glpat-[^ ]*')
```

- Always extract to a variable first — never inline in curl.
- **Never use `glab auth token`** — it outputs help text, not the token.
- Use `grep -o 'glpat-[^ ]*'` — the `Token:` prefix format varies between glab versions.
- **In Python:** shell variables are NOT inherited into heredocs. Use `os.popen("glab config get token --host gitlab.com").read().strip()` inside Python, or `export TOKEN` before the heredoc.

### Authenticated Username

```bash
glab auth status  # look for "Logged in to ... as <USERNAME>"
```

## Issues

### Fetch Issue

```bash
glab issue view <IID> -R <repo>
```

### List Issues by Label

```bash
glab api "groups/<GROUP>/issues?labels=<LABEL>&assignee_username=<USERNAME>&state=opened&per_page=100"
```

### Update Issue Labels

```bash
glab api "projects/<PROJECT_ID>/issues/<IID>" --method PUT \
  --field "add_labels=<NEW_LABEL>" \
  --field "remove_labels=<OLD_LABEL>"
```

### Update Issue Status (GraphQL)

REST API does not support issue status. Use GraphQL:

```bash
# 1. Resolve work item GID
glab api graphql --raw-field query='{
  project(fullPath: "<PROJECT_PATH>") {
    issue(iid: "<IID>") { id }
  }
}'

# 2. Update status
glab api graphql --raw-field query='mutation {
  workItemUpdate(input: {
    id: "<WORK_ITEM_GID>"
    statusWidget: { status: "<STATUS_GID>" }
  }) { errors }
}'
```

Status GIDs: `/1` = "To do", `/2` = "In progress", `/3` = "Done"

## Merge Requests

### List MRs

```bash
glab mr list --author=@me -R <repo>
```

> `glab mr list` defaults to open MRs — do not pass `--state` (removed in recent versions).

### View MR

```bash
glab mr view <IID> -R <repo>
glab mr view <IID> --output json -R <repo>  # JSON output
```

### Create MR

```bash
glab mr create --title '<title>' --description '<description>' \
  --squash-before-merge --remove-source-branch --assignee @me -R <repo>
```

> Use `--description`, NOT `--body` (that's the GitHub `gh` CLI flag).

### Update MR

```bash
glab mr update <IID> --title '<title>' -R <repo>
glab mr update <IID> --description '<description>' -R <repo>
```

When fixing descriptions, **preserve the full body** — read current description first with `glab mr view --output json`.

### MR Diff

```bash
glab mr diff <IID> -R <repo>
```

### MR Commits

```bash
glab api "projects/<URL-ENCODED-PROJECT>/merge_requests/<IID>/commits"
```

### Check Approval Status

```bash
glab api "projects/<PROJECT_ID>/merge_requests/<IID>/approvals" \
 | python3 -c "import sys,json; d=json.load(sys.stdin); print('approved' if d.get('approved') else 'pending')"
```

### List MR Notes

```bash
glab api "projects/<PROJECT_ID>/merge_requests/<IID>/notes?per_page=100"
```

## CI Pipelines

### Check Pipeline Status

```bash
glab ci status --branch <source_branch> -R <repo>
```

### Watch a Manually-Triggered Job by ID

`glab ci status --branch` only finds the latest pipeline; it misses manually-triggered stage jobs and old pipelines. When you already have a job URL (e.g., from `glab mr view`'s `head_pipeline.jobs[]`), hit the REST API directly:

```bash
TOKEN=$(glab config get token --host gitlab.com)
PROJ="<group>%2F<repo>"  # URL-encoded path
JOB=<job_id>

# Status + duration
curl -sL -H "PRIVATE-TOKEN: $TOKEN" \
  "https://gitlab.com/api/v4/projects/$PROJ/jobs/$JOB" | \
  python3 -c "import json,sys;j=json.load(sys.stdin);print(f\"{j['status']} {j.get('duration')}s\")"

# Live trace tail — test-by-test progress visible
curl -sL -H "PRIVATE-TOKEN: $TOKEN" \
  "https://gitlab.com/api/v4/projects/$PROJ/jobs/$JOB/trace" | tail -c 3000
```

Pair with `ScheduleWakeup` to poll at sensible intervals (5-10 min for multi-minute jobs) rather than tight loops.

## Code Review — Draft Notes

**Always use draft notes**, not direct discussions. Draft notes are only visible to the reviewer until explicitly submitted — this lets the user review, edit, and submit as a batch.

### Post Draft Notes via CLI (Mandatory)

**Always use the `t3 review` CLI.** It handles token extraction, diff refs, position serialization, and added-line validation. Never use raw `glab api` or `curl` for draft notes.

```bash
# Inline comment on a specific file and line
t3 review post-draft-note <REPO> <MR_IID> "Comment text" --file <path/to/file> --line <line_number>

# General (non-inline) comment
t3 review post-draft-note <REPO> <MR_IID> "Comment text"

# List existing draft notes
t3 review list-draft-notes <REPO> <MR_IID>

# Delete a draft note
t3 review delete-draft-note <REPO> <MR_IID> <NOTE_ID>

# Edit a note in place — works for drafts AND published notes; auto-detects which.
# The draft endpoint is tried first; on 404 it falls back to the published-notes endpoint.
t3 review update-note <REPO> <MR_IID> <NOTE_ID> "New comment body"
```

### Key Differences from `/discussions`

|  | `/discussions` | `/draft_notes` |
|---|---|---|
| Field name | `"body"` | `"note"` |
| Visibility | Immediately visible to everyone | Only visible to author until submitted |
| Endpoint | `.../discussions` | `.../draft_notes` |

### JSON Escaping for Draft Notes

When the note body contains backticks, single quotes, or other special characters, prefer **direct shell JSON** (single-quoted `--data '{...}'`) over Python `json.dumps` with embedded strings. Python's nested quoting layers (shell → Python string → JSON) are fragile and commonly mangle backticks into `""` or drop them entirely.

### Suggestion Blocks

```suggestion:-0+0
except (ObjectDoesNotExist, TypeError):
```

Use `:-N+M` to expand the range (N lines above, M lines below).

## File Uploads

```bash
TOKEN=$(glab config get token --host gitlab.com)
curl -s --request POST \
  --header "PRIVATE-TOKEN: $TOKEN" \
  --form "file=@<screenshot-path>" \
  "https://gitlab.com/api/v4/projects/<PROJECT_ID>/uploads" | jq -r '.markdown'
```

**Path format (Non-Negotiable):** Use the `.markdown` or `.url` field from the response (format: `/uploads/<hash>/<filename>`). **Never use `.full_path`** (format: `/-/project/<id>/uploads/...`) — it does NOT render in MR notes or comments.

## Reply to Discussion

Reply to an existing MR discussion thread (e.g., after addressing review feedback).

### Pre-Flight Checks (Non-Negotiable)

Before posting replies to discussions:

1. **List all discussions** via `GET .../merge_requests/<IID>/discussions` and read each first note's `body`.
2. **Match topic to reply.** Only reply to discussions whose topic matches your reply content.
3. **Check for existing replies.** If the discussion already has a reply from the user addressing the concern, skip it.

### Post Reply

```bash
TOKEN=$(glab config get token --host gitlab.com)
curl -s -X POST "https://gitlab.com/api/v4/projects/<PROJECT_ID>/merge_requests/<IID>/discussions/<DISCUSSION_ID>/notes" \
  -H "PRIVATE-TOKEN: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": "Done in `<commit_sha>` — <brief description of what changed>."}'
```

**Key field:** `"body"` (not `"note"` — that's for draft notes).

## MR Notes (Comments)

### Post Note

```bash
glab mr note <MR_NUMBER> -R <REPO_PATH> -m "$(cat <<'EOF'
Comment body here.
EOF
)"
```

### Post or Update Note with Images — Always Use Python

Shell variable interpolation and `jq --arg` both escape `!` to `\!`, breaking image syntax `![alt](url)`. **Always use Python:**

```bash
TOKEN=$(glab config get token --host gitlab.com)

NOTE_ID=$(curl -s "https://gitlab.com/api/v4/projects/<PROJECT_ID>/merge_requests/<MR_IID>/notes" \
  -H "PRIVATE-TOKEN: $TOKEN" | jq '[.[] | select(.body | startswith("## Test Plan"))][0].id')

python3 << 'PYEOF'
import json, urllib.request, os
body_text = """## Test Plan
...
![screenshot](/uploads/<hash>/screenshot.png)
"""
token = os.popen("glab config get token --host gitlab.com").read().strip()
payload = json.dumps({"body": body_text}).encode()
url = "https://gitlab.com/api/v4/projects/<PROJECT_ID>/merge_requests/<MR_IID>/notes"
req = urllib.request.Request(url, data=payload, method="POST", headers={
    "PRIVATE-TOKEN": token,
    "Content-Type": "application/json",
})
resp = urllib.request.urlopen(req)
print(json.loads(resp.read())["id"])
PYEOF
```

## Position Field Reference

| Field | Value |
|---|---|
| `old_path` / `new_path` | File path relative to repo root. |
| `new_line` | Line number in the **new** version. Use for added or modified lines. |
| `old_line` | Line number in the **old** version. Use for deleted lines. |

## Known CLI Quirks

- `--state` flag removed from `glab mr list` — defaults to open MRs.
- `--output json` renamed to `-F json` in some commands.
- `glab mr create` uses `--description`, NOT `--body`.
- Variable name `status` is read-only in zsh — use `ci_state` or similar.
- `glab api --raw-field` cannot serialize nested JSON — use `curl` for complex payloads (see § Draft Notes).

## Transition Logic

Shared pattern for updating both label and status on an issue:

```bash
# 1. Update Process label
glab api "projects/<PROJECT_ID>/issues/<IID>" --method PUT \
  --field "add_labels=<NEW_LABEL>" \
  --field "remove_labels=<OLD_LABEL>"

# 2. Update issue status via GraphQL (if status GID changes)
glab api graphql --raw-field query='mutation {
  workItemUpdate(input: {
    id: "<WORK_ITEM_GID>"
    statusWidget: { status: "<STATUS_GID>" }
  }) { errors }
}'
```
