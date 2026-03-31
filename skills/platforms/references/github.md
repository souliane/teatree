# GitHub Platform Reference

> API recipes for GitHub-based projects. Skills reference this file — keep workflow logic in the skill, platform commands here.

---

## Authentication

```bash
# gh CLI handles auth automatically. Verify:
gh auth status
```

## Issues

### Fetch Issue

```bash
gh issue view <NUMBER> --repo <OWNER>/<REPO> --json title,body,comments,labels,state
```

### List Issues by Label

```bash
gh issue list --repo <OWNER>/<REPO> --label "<label>" --json number,title,state
```

### Update Labels

```bash
gh issue edit <NUMBER> --repo <OWNER>/<REPO> --add-label "label1" --remove-label "label2"
```

## Pull Requests

### List PRs

```bash
gh pr list --repo <OWNER>/<REPO> --json number,title,headRefName,state
```

### View PR

```bash
gh pr view <NUMBER> --repo <OWNER>/<REPO> --json title,body,headRefName,baseRefName,state,reviewDecision,commits
```

### Create PR

```bash
gh pr create --repo <OWNER>/<REPO> --title "<title>" --body "<body>" --base main --head <branch> --assignee @me
```

### PR Diff

```bash
gh pr diff <NUMBER> --repo <OWNER>/<REPO>
```

### PR Commits

```bash
gh api repos/<OWNER>/<REPO>/pulls/<NUMBER>/commits
```

## Code Review

### Post Review Comment (Inline)

```bash
gh api repos/<OWNER>/<REPO>/pulls/<NUMBER>/comments \
  -f body="<comment>" \
  -f commit_id="<SHA>" \
  -f path="<file_path>" \
  -F line=<line_number> \
  -f side="RIGHT"
```

### Post General Comment

```bash
gh pr comment <NUMBER> --repo <OWNER>/<REPO> --body "<comment>"
```

### Submit Review (Approve / Request Changes)

```bash
gh pr review <NUMBER> --repo <OWNER>/<REPO> --approve --body "LGTM"
gh pr review <NUMBER> --repo <OWNER>/<REPO> --request-changes --body "<feedback>"
```

### Suggestion Blocks

````markdown
```suggestion
corrected code here
```
````

## CI / Actions

### View Workflow Runs

```bash
gh run list --repo <OWNER>/<REPO> --branch <branch> --json databaseId,status,conclusion,name
```

### View Run Details

```bash
gh run view <RUN_ID> --repo <OWNER>/<REPO> --json status,conclusion,jobs
```

### Watch Run (Blocking)

```bash
gh run watch <RUN_ID> --repo <OWNER>/<REPO>
```

## File Uploads

GitHub does not support file uploads via API for issue/PR comments. Use external hosting or attach files via the web UI.

## Known CLI Quirks

- `gh pr create --body` (not `--description` — that's the GitLab CLI flag).
- `gh issue list` defaults to open issues. Use `--state all` or `--state closed` to include others.
- `gh api` returns JSON by default. Use `--jq` for field extraction: `gh api repos/.../pulls/1 --jq '.title'`.
