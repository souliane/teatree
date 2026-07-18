#!/usr/bin/env bash
# Compatibility shim — the hook body now lives in the packaged portable set.
#
# Canonical source: src/teatree/hooks/portable/refuse-main-clone-commit.sh
# (#3312), runnable in any repo via `t3 hook run refuse-main-clone-commit`.
# Kept so teatree's own prek entry (scripts/hooks/refuse-main-clone-commit.sh)
# keeps working; it execs the single packaged copy so the #2614 hardening can
# never drift between the two paths.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${here}/../../src/teatree/hooks/portable/refuse-main-clone-commit.sh" "$@"
