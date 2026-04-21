# `followup.json` Schema

The followup cache at `$T3_DATA_DIR/followup.json` is the single source of truth for all in-flight work. It is platform-neutral — the core schema covers what t3:followup needs; project overlays add fields via `followup_enrich_data`.

```json
{
  "generated_at": "ISO timestamp",
  "tickets": {
    "<ticket_id>": {
      "title": "Human-readable title",
      "url": "Issue tracker URL or null",
      "tracker_status": "Platform-neutral status string",
      "feature_flag": "Flag name or null",
      "mrs": ["<repo>!<iid>", ...]
    }
  },
  "mrs": {
    "<repo>!<iid>": {
      "url": "MR web URL",
      "repo": "Repository short name",
      "project_id": 12345,
      "title": "MR title",
      "branch": "Source branch name",
      "ticket": "<ticket_id>",
      "pipeline_status": "success|failed|running|pending|null",
      "pipeline_url": "URL or null",
      "review_requested": true,
      "review_channel": "#channel-name",
      "review_permalink": "Chat permalink or null",
      "review_comments": { "status": "addressed|pending|null", "details": "..." },
      "e2e_test_plan_url": "URL to MR comment with test plan, or null",
      "approvals": { "count": 0, "required": 1 },
      "skipped": false,
      "skip_reason": null
    }
  },
  "review_comments_tracking": {
    "<repo>!<iid>": {
      "url": "MR web URL",
      "status": "waiting_reviewer|addressed|needs_reply",
      "details": "Human-readable summary"
    }
  },
  "draft_mrs": {
    "<repo>!<iid>": {
      "url": "MR web URL",
      "repo": "Repository short name",
      "title": "MR title (without Draft: prefix)",
      "pipeline_status": "success|failed|running|pending|null",
      "pipeline_url": "URL or null"
    }
  },
  "actions_log": ["Action description", ...]
}
```

Project overlays can add extra fields to ticket and MR entries (e.g., `notion_status`, `tenant`). The core schema ignores unknown fields — overlays read/write their own fields alongside the core ones.
