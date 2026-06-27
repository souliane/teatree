# Recovered Work — Salvage Catalog

Salvage of unfinished/unmerged work from rescued worktree snapshots that were
captured on the owner's machine (367 snapshot directories, each a `git bundle`
plus a captured working-tree diff). Every label below is one branch; only the
latest snapshot per label was kept (345 older duplicate snapshots collapsed).

## Counts

| Metric | Count |
|---|---|
| Total snapshot directories | 367 |
| Distinct labels (after dedup) | 22 |
| Recovered — genuine in-repo work | 4 |
| Recovered — disposable scratch/test-fixture | 7 |
| Superseded — already on `origin/main` | 3 |
| Withheld — foreign/personal/restricted (kept off this public repo) | 7 |
| Failed — unrecoverable bundle | 1 |

## Restore instructions

From a clean worktree of the relevant repo:

- **Recovered commits:** `git am recovered-work/<label>/commits.patch`
- **Recovered uncommitted work:** `git apply recovered-work/<label>/worktree.diff`
- **Withheld / failed:** never committed here. The raw `branch.bundle` +
  `working-tree.diff` stay on the owner's machine in the local rescue directory.
  To locate one, match its tip SHA against the local bundles:

  ```bash
  RESCUE=~/.local/share/teatree/recovery-rescue-20260627
  sha=d68e3918   # the tip SHA from the table below
  for d in "$RESCUE"/*/; do \
    git bundle list-heads "$d/branch.bundle" 2>/dev/null | grep -q "$sha" && echo "$d"; \
  done
  # then, locally only:  git clone <dir>/branch.bundle restored && git -C restored apply ../<dir>/working-tree.diff
  ```

  A full owner-only index (real labels → directories → reasons) was written next to
  the bundles at `$RESCUE/RECOVERY-LOCAL-INDEX.md` and is intentionally **not** in this repo.

## Recovered

| Label | Branch | Kind | Unique commits | Uncommitted diff | Restore |
|---|---|---|---|---|---|
| `2746-feat-dream-cold-tier-memory-recall-surfa` | `2746-feat-dream-cold-tier-memory-recall-surfa` | teatree work | 2 | — | `git am recovered-work/2746-feat-dream-cold-tier-memory-recall-surfa/commits.patch` |
| `dream-promote-fix-and-merge-2663` | `dream-promote-fix-and-merge-2663` | teatree work | 1 | — | `git am recovered-work/dream-promote-fix-and-merge-2663/commits.patch` |
| `fix-metered-eval-green` | `fix/metered-eval-green` | teatree work | 4 | 22KB | `git am recovered-work/fix-metered-eval-green/commits.patch` ; `git apply recovered-work/fix-metered-eval-green/worktree.diff` |
| `substrate-ping-and-hold` | `substrate-ping-and-hold` | teatree work | 4 | — | `git am recovered-work/substrate-ping-and-hold/commits.patch` |
| `1205-feat-thing` | `1205-feat-thing` | scratch/test-fixture (disposable) | 2 | — | `git am recovered-work/1205-feat-thing/commits.patch` |
| `1462` | `ac-myrepo-1462-x` | scratch/test-fixture (disposable) | 1 | — | `git am recovered-work/1462/commits.patch` |
| `491` | `ac-teatree-491-x` | scratch/test-fixture (disposable) | 1 | — | `git am recovered-work/491/commits.patch` |
| `706` | `ac-myrepo-706-x` | scratch/test-fixture (disposable) | 2 | — | `git am recovered-work/706/commits.patch` |
| `859` | `ac-teatree-859-merge` | scratch/test-fixture (disposable) | 2 | — | `git am recovered-work/859/commits.patch` |
| `99` | `ac-myrepo-99-x` | scratch/test-fixture (disposable) | 1 | — | `git am recovered-work/99/commits.patch` |
| `orphan` | `snapshot-feat` | scratch/test-fixture (disposable) | 2 | — | `git am recovered-work/orphan/commits.patch` |

## Superseded (already merged to `origin/main`; nothing to restore)

| Label | Branch | Note |
|---|---|---|
| `2753` | `fix-eval-negative-contains-matcher` | patch-equivalent already on `origin/main`; empty working diff |
| `2753-fix-dream-budget-tier-can-t-converge-on-` | `2753-fix-dream-budget-tier-can-t-converge-on-` | patch-equivalent already on `origin/main`; empty working diff |
| `2755-dream-index-budget-bytes-not-lines` | `2755-dream-index-budget-bytes-not-lines` | patch-equivalent already on `origin/main`; empty working diff |

## Withheld & failed (kept OFF this public repo — restore locally)

Foreign repos (other products / a private overlay / personal dotfiles) and
customer-domain content that must never live in this public repo. Labels and
branches are redacted here; identify each by its tip SHA via the script above.

| Id | Category | Approx commits | Captured diff | Tip SHA (lookup) |
|---|---|---|---|---|
| W1 | foreign product repo — full unrelated history (customer code) | 15495 | — | `d68e3918` |
| W2 | foreign product repo — full history + ~17KB uncommitted customer diff | 15180 | 17KB | `c5bf433a` |
| W3 | foreign product repo — full unrelated history (customer code) | 15183 | — | `7235950d` |
| W4 | private overlay repo — full unrelated history (internal references) | 292 | — | `b3d2f226` |
| W5 | foreign product repo — full history (customer/feature-flag code) | 884 | — | `5e029cdd` |
| W6 | personal dotfiles repo — memory/config/secrets+PII risk; ~326KB uncommitted diff | 93 | 318KB | `a1bf1418` |
| W7 | foreign e2e repo — thin/corrupt bundle; ~1MB uncommitted diff in earlier snapshots | 0 | — | `67c6190a` |
| W8 | thin/incomplete bundle (missing delta-base); no working diff — unrecoverable | — | — | `2d544de1` |

## Notes

- **Scratch/test-fixture** recovered entries are unrelated-history throwaway repos
  (author `t <t@t>`, commit messages like `init` / `initial` / `keep.txt`), almost
  certainly created by this repo's own test suite and swept up by the rescue. They are
  retained only so nothing is silently dropped — safe to delete.
- **Withheld** entries share no history with this repo. Their bundles and diffs remain
  only on the owner's machine; see the owner-only local index next to the bundles.
- **Failed** entries have thin/incomplete bundles (a delta-base object was not captured)
  and no working diff, so nothing is recoverable from the snapshot.
- `recovered-work/<label>/commits.patch` are faithful `format-patch` outputs and are
  meant to be reapplied with `git am`; their contents are preserved verbatim.
