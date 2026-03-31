---
name: platforms
description: Platform-specific API recipes for GitLab, GitHub, and Slack. Auto-loaded as a dependency by skills that interact with these platforms.
compatibility: any
metadata:
  version: 0.0.1
  subagent_safe: true
---

# Platform References

API recipes for issue trackers, CI systems, and chat platforms. Each platform has its own reference file — load the one matching your project's setup.

## References

| File | Platform |
|---|---|
| [`references/gitlab.md`](references/gitlab.md) | GitLab API, MR validation, pipeline control |
| [`references/github.md`](references/github.md) | GitHub API, PR workflows |
| [`references/slack.md`](references/slack.md) | Slack messaging, channel discovery |

Skills reference these via `See platforms/references/gitlab.md § <section>`.
