#!/usr/bin/env bash
# A fleet claim ref (refs/teatree/claims/*) carries claim metadata, never a branch's work, so branch gates skip it.
# prek names the pushed remote ref in PRE_COMMIT_REMOTE_BRANCH; the leak gate is never wrapped and still runs on claims.
set -euo pipefail

case "${PRE_COMMIT_REMOTE_BRANCH:-}" in
  refs/teatree/claims/*) exit 0 ;;
esac
exec "$@"
