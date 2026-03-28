# E2E Test Coverage

## Panels to cover

- [x] summary
- [x] tickets
- [x] headless_queue
- [x] queue (interactive)
- [x] sessions
- [ ] action_required
- [ ] worktrees
- [ ] review_comments
- [ ] activity
- [ ] Task detail modal
- [ ] Session history endpoint
- [ ] SSE connection (real-time updates)
- [ ] Launch terminal flow (ttyd)
- [ ] Launch agent flow (headless execute)

## Debugging

Capture server stdout/stderr to a log file instead of DEVNULL:
```python
server = subprocess.Popen(..., stdout=log_file, stderr=log_file)
```
