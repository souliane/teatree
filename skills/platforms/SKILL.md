---
name: platforms
description: Platform-specific API recipes for GitLab, GitHub, Slack, and X (Twitter). Auto-loaded as a dependency by skills that interact with these platforms.
eval_exempt: pure API-recipe reference auto-loaded as a dependency; carries no standalone agent behaviour of its own
compatibility: any
metadata:
  version: 0.0.1
---

# Platform References

API recipes for issue trackers, CI systems, and chat platforms. Each platform has its own reference file — load the one matching your project's setup.

## References

| File | Platform |
|---|---|
| [`references/gitlab.md`](references/gitlab.md) | GitLab API, PR validation, pipeline control |
| [`references/github.md`](references/github.md) | GitHub API, PR workflows |
| [`references/slack.md`](references/slack.md) | Slack messaging, channel discovery |
| [`references/x-twitter.md`](references/x-twitter.md) | X (Twitter) post reading via JSON mirror |

Skills reference these via `See platforms/references/gitlab.md § <section>`.

A repo whose MRs are authored under a non-owner credential gets its MR from `mcp__teatree__pr_create` or `t3 <overlay> pr create <ticket-id>`, never `glab mr create`, which writes as the owner and leaves the MR unapprovable; the recipe stays in `references/gitlab.md` § Create MR.
