---
name: full-speed
description: Deprecated alias for `/t3:speed boost`. Parallel backlog blast — classify the actionable backlog, fan out autonomous-safe work across isolated worktrees, surface the rest. Use when the user says "full speed", "blast the backlog", "parallel mode", "max throughput", or "go wide".
compatibility: any
requires:
  - speed
triggers:
  priority: 70
  keywords:
    - '\b(full[- ]speed|blast the backlog|parallel mode|max throughput|go wide|parallel backlog)\b'
    - '\b(work in parallel|fan[- ]out|all tickets at once|tackle everything)\b'
search_hints:
  - full speed
  - blast backlog
  - parallel mode
  - max throughput
  - go wide
  - fan out
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Full-Speed — alias for `/t3:speed boost`

The parallel-backlog-blast behaviour this skill used to carry now lives in the [`../speed/SKILL.md`](../speed/SKILL.md) dial as the **`boost`** level. `/t3:full-speed` is kept as a thin alias for one release so existing phrasing ("full speed", "blast the backlog", "go wide") keeps working.

**Run `/t3:speed boost`** — one parallel-backlog-blast wave (bucket classification → fan-out of bucket (a) → dependency-aware merge serialization), clamped to `max_concurrent_auto_starts`. The full procedure, hard rails, and result-tracking format live in [`../speed/SKILL.md`](../speed/SKILL.md) § "`boost` — one parallel-backlog-blast wave".

For a self-sustaining burst (each wave re-classifies and re-fans-out), use `/t3:speed full` instead, which arms `/loop /t3:speed boost`.

> Deprecation: prefer `/t3:speed` directly. This alias will be removed once the rename has settled.
