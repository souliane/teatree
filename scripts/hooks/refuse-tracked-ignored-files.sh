#!/usr/bin/env bash
# .gitignore cannot stop a force-add or a merge; keep an intended file with a `!` negation.
# Only the repo's own .gitignore files count: a personal excludesFile is not the repo's contract.
set -euo pipefail

tracked_ignored=$(git ls-files --cached --ignored --exclude-per-directory=.gitignore)
if [[ -z "$tracked_ignored" ]]; then
  exit 0
fi

{
  echo "Tracked files match a .gitignore rule. Untrack them (git rm --cached), or carve them out with a '!' negation:"
  echo "$tracked_ignored"
} >&2
exit 1
