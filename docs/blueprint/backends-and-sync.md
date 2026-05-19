# BLUEPRINT Appendix — Backend Protocols and Sync

Detail behind [BLUEPRINT.md](../../BLUEPRINT.md) §7 and §9. Consumer cross-references such as `BLUEPRINT §3.6` (Slack bot setup) and §7 protocols resolve here.

## 7. Backend Protocols and ABCs

### 7.1 API Protocols (`backends/protocols.py`)

Each external API concern is a `@runtime_checkable Protocol` in `teatree.backends.protocols`. Request parameters are grouped into frozen dataclasses (e.g. `PullRequestSpec`, `MessageSpec`) so signatures stay small and extensible.

**Naming convention:** PR is the canonical term in core. GitLab implementations translate MR ↔ PR at the API edge — overlay code may use either term internally, but everything inside `src/teatree/` says PR.

| Protocol | Methods | Implementations |
|---|---|---|
| `CodeHostBackend` | `create_pr(PullRequestSpec)`, `current_user()`, `list_my_prs(*, author, updated_after=None)`, `list_review_requested_prs(*, reviewer, updated_after=None)`, `post_pr_comment(*, repo, pr_iid, body)`, `update_pr_comment(*, repo, pr_iid, comment_id, body)`, `list_pr_comments(*, repo, pr_iid)`, `upload_file(*, repo, filepath)`, `get_issue(issue_url)`, `list_assigned_issues(*, assignee)`, `get_review_state(*, pr_url, reviewer)` | `GitHubCodeHost` (in `backends/github.py`), `GitLabCodeHost` (in `backends/gitlab.py`) |
| `CIService` | `cancel_pipelines(*, project, ref)`, `fetch_pipeline_errors(*, project, ref)`, `fetch_failed_tests(*, project, ref)`, `trigger_pipeline(*, project, ref, variables=None)`, `quality_check(*, project, ref)` | `GitLabCIService` (GitHub Actions CI not yet implemented) |
| `MessagingBackend` | `fetch_mentions(*, since="")`, `fetch_dms(*, since="")`, `post_message(*, channel, text, thread_ts="")`, `post_reply(*, channel, ts, text)`, `react(*, channel, ts, emoji)`, `resolve_user_id(handle)` | `SlackBotBackend`, `NoopMessagingBackend` |

`PullRequestSpec(repo, branch, title, description, target_branch="", labels=[], assignee="", draft=False)` and `MessageSpec(channel, text, thread_ts="")` are the two frozen request dataclasses; both are `slots=True`. `repo + pr_iid` is the natural unit on both APIs (GitLab `merge_requests/<iid>`, GitHub `pulls/<number>`) — neither protocol method accepts a free-form PR URL.

`backends.sentry.SentryClient` is a concrete client (no Protocol). The `IssueTracker` and `ChatNotifier` protocols are folded into `CodeHostBackend` (issue methods) and `MessagingBackend` (post methods) respectively — the previous split duplicated state across protocols and forced overlays to configure two backends for one platform.

### 7.2 Code-Host Selection

Per-overlay configuration in `~/.teatree.toml` (see § 10.1) declares which code host an overlay targets via `code_host = "github" | "gitlab"`. The loader resolves the overlay's selected backend with no platform branches in caller code:

```python
def get_code_host(overlay: OverlayBase) -> CodeHostBackend:
    match overlay.config.code_host:
        case "github":
            return GitHubCodeHost(token=overlay.config.get_github_token())
        case "gitlab":
            return GitLabCodeHost(token=overlay.config.get_gitlab_token(), url=overlay.config.gitlab_url)
        case other:
            raise ValueError(f"Unknown code_host: {other!r}")
```

The loop's PR-sweep scanners (§ 5.6) iterate registered overlays, instantiate each overlay's `CodeHostBackend`, and aggregate. Two overlays on the same code host with different tokens (e.g. personal vs. work GitHub) are first-class.

### 7.3 Messaging Selection

Per-overlay `messaging_backend` declaration follows the same pattern. Default is `"noop"` — overlays opt in. A single Slack workspace can serve multiple overlays (one bot, one token, distinct channel routing), or each overlay can declare its own bot via `slack_token_ref` (a `pass` entry name prefix; see § 10.1).

**Inbound events.** `t3 slack listen` runs a global singleton Socket Mode receiver that opens one WebSocket per slack-enabled overlay. Events are partitioned across two append-only JSONL queues so independent scanners each own an atomic-rename drain without racing on a shared inode: `app_mention` / `message.im` go to `$XDG_DATA_HOME/teatree/slack-events.jsonl` (drained by `SlackMentionsScanner`), `reaction_added` goes to `$XDG_DATA_HOME/teatree/slack-reactions.jsonl` (drained by `SlackReviewIntentScanner`, #1047). When the receiver is not running, `fetch_dms` falls back to `conversations.history` API polling. Install the optional `slack_sdk` dependency with `uv tool install --editable '.[slack]'`.

```python
def get_messaging(overlay: OverlayBase) -> MessagingBackend:
    match overlay.config.messaging_backend:
        case "slack":
            return SlackBotBackend(
                bot_token=pass_get(overlay.config.slack_token_ref + "-bot"),
                app_token=pass_get(overlay.config.slack_token_ref + "-app"),
                user_id=overlay.config.slack_user_id,
            )
        case "noop" | "":
            return NoopMessagingBackend()
        case other:
            raise ValueError(f"Unknown messaging_backend: {other!r}")
```

### 7.4 Sync ABC (`core/sync.py`)

`SyncBackend` is an ABC defined in `teatree.core.sync`. Every file under `backends/` that performs data sync into the Django DB must implement it.

```python
class SyncBackend(ABC):
    def is_configured(self, overlay: object) -> bool: ...   # has credentials?
    def sync(self, overlay: object) -> SyncResult: ...      # run the sync
```

Implementations: `GitHubSyncBackend` (`backends/github_sync.py`), `GitLabSyncBackend` (`backends/gitlab_sync.py`). Both consume the `CodeHostBackend` Protocol — the platform-specific code lives only in the Protocol implementation, not in the sync logic.

**Convention:** `sync()` and `is_configured()` are instance methods. All internal helpers are `@classmethod` (no instance state needed).

**Loading** (`loader.py`): Each backend has a `get_<concern>(overlay)` function decorated with `@lru_cache(maxsize=1)` keyed on the overlay's identity. These functions auto-configure from `overlay.config` — no `TEATREE_*` settings or `import_string()` involved.

**Cache reset:** `reset_backend_caches()` clears all lru_cache entries (used in testing).

---

## 9. Code Host Sync (sync.py)

`sync_followup()` → `SyncResult`:

Runs all configured backends and merges results via `_merge_results()`. Iterates registered overlays; for each, instantiates the overlay's `CodeHostBackend` (§ 7.2) and runs the corresponding `SyncBackend.sync()`. Both `GitHubSyncBackend` and `GitLabSyncBackend` are first-class — selection is per-overlay, not global.

**Common sync flow** (platform-agnostic, lives in `core/sync.py`):

1. Resolve the overlay's `CodeHostBackend`
2. Fetch all open PRs authored by the current user (incremental via cached `updated_after` timestamp)
3. For each PR: `_upsert_ticket_from_pr()`:
   - Extract `issue_url` from PR description/title via regex
   - Enrich non-draft PRs with pipeline status, approvals, review threads
   - Infer ticket state from PR data via `_infer_state_from_prs()`
   - Upsert ticket by issue_url (or PR URL if no issue linked)
4. `_fetch_issue_metadata()`: fetch issue details, store `tracker_status` (from `Process::` labels or platform-specific status widget) and `issue_title`
5. `_detect_merged_prs()`: find recently merged PRs and advance matching tickets to `merged`
6. Return `SyncResult(prs_found, tickets_created, tickets_updated, labels_fetched, prs_merged, errors)`

The platform-specific code (work-item API shape, label syntax, draft-detection rules) lives only in the `CodeHostBackend` implementation; `core/sync.py` is platform-agnostic.

**State inference:** `_infer_state_from_prs()` derives a minimum ticket state from PR metadata, bypassing FSM transitions (which have side effects like task creation). On creation, the inferred state becomes the default. On update, the ticket advances forward only — never regresses.

| PR data | Inferred state |
|---------|---------------|
| Draft PR | `started` |
| Non-draft PR | `shipped` |
| Non-draft + review requested or approvals > 0 | `in_review` |

Multiple PRs: the highest inferred state wins.

**Review-thread classification:** `_classify_review_threads()` categorizes PR threads as `waiting_reviewer` (last comment is mine), `needs_reply` (last comment is theirs), or `addressed` (all resolved).

**Draft comments detection:** During sync, `get_draft_notes_count()` checks each non-draft PR for unpublished draft notes (GitLab "draft notes" / GitHub "pending review"). When present, `draft_comments_pending: true` and `draft_comments_count: N` are set on the PR entry. The statusline's "Action needed" zone shows a `review_draft` item prompting the user to review and publish the loop's draft comments.
