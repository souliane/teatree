#!/usr/bin/env bash
# Pre-commit hook: reject files containing banned terms.
#
# The banned-terms list is DB-home: it is read from the canonical ConfigSetting
# store (the DB is PRIVATE to the operator). Set it with:
#   t3 <overlay> config_setting set banned_terms '["term1","term2"]'
#   # Optional company-identifier carve-out (#1415 over-block): the org's OWN
#   # compound identifiers / internal-URL namespaces — never customer PII. Each
#   # entry's whole-token run is blanked BEFORE matching, so a shorter banned
#   # term (a bare org slug) never surfaces inside a longer company identifier.
#   t3 <overlay> config_setting set banned_terms_allowlist '["myorg-engineering","myorg-product"]'
# The T3_BANNED_TERMS env value (comma-separated) still WINS over the DB.
#
# Example .pre-commit-config.yaml entry (the CLI reads the DB itself, so the
# hook passes only the staged files and --diff-only):
#   entry: scripts/hooks/check-banned-terms.sh
#
# An explicit empty list exits 0 (no-op). A genuinely UNSET list (no
# banned_terms row and no env) WARNS loud and exits 0 by default — an unset list
# is not a banned-term violation on a dev/solo box (#3247); it exits 2 (fail
# loud) only when banned_terms_required is set (a deployment that MUST scrub).
# A store that could not be READ (locked, corrupt, table-less) is NOT an unset
# list and exits 2 whatever banned_terms_required says: an errored read carries
# no information about what the operator configured (#4008).
#
# This is a THIN wrapper: all matching is delegated to
# ``teatree.hooks.banned_terms_cli`` (which uses ``teatree.hooks.term_match``),
# so the shell hook, the in-process posting gate and the overlay-leak gate all
# run ONE matcher. The hook reimplemented the tokenizer in bash-inlined Python
# before; that second copy could drift silently from term_match. A parity
# meta-test (tests/test_banned_terms_parity.py) pins all entry points to the
# same verdict on a golden corpus so they cannot diverge again.
#
# Exit-code contract (consumed by ``teatree.hooks.banned_terms_scanner`` and
# prek): 0 = clean (incl. an unset list when banned_terms_required is off, #3247),
# 1 = banned term found, 2 = the scanner COULD NOT RUN, the term store could not
# be READ, or an unset list when banned_terms_required is on.
# A security gate that fails OPEN on a crash is the bug class: the codebase
# requires Python >= 3.13, so under an old system ``python3`` the matcher
# import crashes (PEP-604 unions) and exits 1 — colliding with "banned term
# found" — and the caller, parsing an empty report, turned that into ALLOW.
# The ``python3`` fallback now probe-imports the module first and exits 2
# (fail closed, loud) when it cannot run, never letting a crash equal ALLOW.
#
# The interpreter is resolved through ``lib/resolve-uv.sh``, never a bare
# ``command -v uv``. This hook runs with its CWD inside an ARBITRARY repo, and a
# version-manager shim fronting ``uv`` selects its interpreter from THAT repo's
# ``.python-version`` — exiting 127 before uv runs when the pinned version is
# not installed. That wedged the gate closed in every such repo while uv itself
# was installed and healthy, so the resolver verifies a candidate by RUNNING it
# and falls through to a real uv binary rather than trusting PATH.

set -euo pipefail

# Resolve the repo root from this script's own location
# (scripts/hooks/check-banned-terms.sh -> repo root) so the CLI runs against
# THIS clone's teatree install regardless of the caller's cwd.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

scanner_unavailable() {
  echo "ERROR: banned-terms scanner could not run: $1" >&2
  if [ "${uv_resolution_rc:-0}" -eq 2 ]; then
    # The candidate walk never finished, so "nothing answers" was never
    # established. Naming uv here is the misdirection that made this class
    # expensive: it sends the reader to reinstall a tool that was never at fault.
    echo "  Cause class: this hook's own SHELL execution — the uv candidate walk" >&2
    echo "  did not finish, so whether a uv answers was never determined. The uv on" >&2
    echo "  this box is NOT implicated: installing one cannot help, and neither can" >&2
    echo "  pointing T3_UV at a perfectly good binary." >&2
    echo "  Running: ${BASH_VERSION:-unknown} (${BASH:-unknown path})." >&2
    echo "  Look for a shell feature the resolver uses that THIS bash lacks — macOS" >&2
    echo "  selects /bin/bash 3.2 for '#!/usr/bin/env bash', where bash 4+ builtins" >&2
    echo "  are absent and fail SILENTLY — or a permission error mid-walk." >&2
    echo "  Reproduce it directly:" >&2
    echo "    bash -c 'set -u; . ${script_dir}/lib/resolve-uv.sh; resolve_uv'; echo \$?   # 2 = this class" >&2
  else
    echo "  Interpreter RESOLUTION found nothing that answers: candidates were listed" >&2
    echo "  and no uv on this box ran '--version' (a version-manager SHIM exiting 127" >&2
    echo "  is skipped, never accepted), and python3 could not import the matcher." >&2
    echo "  Point T3_UV at a working uv binary, or install a Python >= 3.13." >&2
  fi
  echo "  Failing CLOSED (exit 2): a crash must never be treated as a clean scan." >&2
  exit 2
}

# shellcheck source=lib/resolve-uv.sh
. "${script_dir}/lib/resolve-uv.sh"

# Prefer ``uv run`` so the matcher comes from this repo's environment (critical
# in a worktree, where a bare ``python3`` may import a different editable
# install). ``resolve_uv`` probes rather than trusting PATH, so a broken shim
# falls through here instead of wedging the gate; ``uv_project_run_prefix`` keeps
# the run from reconciling an environment on the other side of a bind mount.
# `resolve_uv` fails two ways that need opposite remedies (lib/resolve-uv.sh):
# 1 = candidates were listed and none ran, 2 = the listing itself crashed. Keep
# the code so the report names the one that actually happened. `|| rc=$?` rather
# than `if !`, whose `$?` is the NEGATION, not the resolver's status.
uv_resolution_rc=0
uv_bin=""
uv_bin="$(resolve_uv)" || uv_resolution_rc=$?

# `--frozen` because dependency RESOLUTION is unbounded, runs on EVERY scan, and is
# charged against the caller's own scan budget — so on a contended venue it presents
# as a timeout and the gate blocks clean content. `--no-sync` is deliberately NOT the
# flag: `uv_project_run_prefix` redirects to a hook-owned UV_PROJECT_ENVIRONMENT that
# nothing else provisions, so skipping the sync makes a fresh venue fail closed
# permanently (`ModuleNotFoundError: teatree`) instead of intermittently.
if [ "${uv_resolution_rc}" -eq 0 ]; then
  uv_project_run_prefix "${uv_bin}" "${repo_root}"
  exec "${UV_PROJECT_RUN[@]}" run --frozen --project "${repo_root}" python -m teatree.hooks.banned_terms_cli "$@"
fi

# ``python3`` fallback. Probe-import the matcher BEFORE running the scanner: an
# interpreter below the >= 3.13 floor crashes the import and exits 1, which is
# indistinguishable from "banned term found". The probe converts that crash
# into a loud, distinct fail-closed exit (2) instead.
fallback_env=(env "PYTHONPATH=${repo_root}/src${PYTHONPATH:+:${PYTHONPATH}}")

if ! command -v python3 >/dev/null 2>&1; then
  scanner_unavailable "no python3 interpreter on PATH"
fi

if ! "${fallback_env[@]}" python3 -c 'import teatree.hooks.banned_terms_cli' >/dev/null 2>&1; then
  scanner_unavailable "python3 cannot import teatree.hooks.banned_terms_cli (interpreter too old or env broken)"
fi

# The probe passed, so the interpreter can run the scanner. Run it WITHOUT exec
# so an unexpected non-{0,1} exit (a crash that slipped past the probe) is still
# converted to the fail-closed code rather than propagating an ambiguous code.
set +e
"${fallback_env[@]}" python3 -m teatree.hooks.banned_terms_cli "$@"
rc=$?
set -e
case "${rc}" in
  0 | 1) exit "${rc}" ;;
  *) scanner_unavailable "scanner exited with unexpected code ${rc}" ;;
esac
