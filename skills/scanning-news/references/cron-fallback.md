# Cron Fallback — read when the scanning-news scanner is not yet wired into the main loop

Until the dedicated `scanning_news` scanner ships (the architectural-review-style scanner that queues a daily Task for this skill), fall back to the same crontab pattern documented by `t3:followup`.

## Local crontab

Add to `crontab -e`:

```bash
# Daily AI-newsletter scan at 9am local
0 9 * * * <agent-cli-command> "/t3:scanning-news --periodic" >> "$T3_DATA_DIR/scanning-news.log" 2>&1
```

Replace `<agent-cli-command>` with the CLI invocation for your agent platform (e.g., `claude code --print`).

## `/schedule` (remote routine)

If a `/schedule` skill is available in the agent runtime:

```text
/schedule create --name daily-scanning-news --cron "0 9 * * *" --prompt "/t3:scanning-news --periodic"
```

## Time of day

TLDR AI lands in US morning. 9am local is a reasonable default for a US-aligned subscriber; adjust by timezone if needed.

## When to remove this fallback

Once the `scanning_news` scanner is wired into the main loop (mirroring `architectural_review`), delete the crontab line. The loop integration gives the scan access to the same observability, retry, and dedupe machinery as other periodic tasks.
