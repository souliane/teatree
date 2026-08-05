# The test-plan note, the manifest schema, and the artifact layout

The schemas and rendering details behind `/t3:e2e` § "Post Testing Evidence on the Ticket". That section carries the canonical command and the one-note-per-ticket rule; this file carries how the note renders, the flag and template tables, the manifest schema, and the artifact directory layout.

## How the note renders, its flags, and picking a template

The note renders as a **test plan**: a header (the ticket title, multi-repo MR links, the per-env commit provenance, and a dev-gap reconciliation line) followed by one block per workflow — the workflow heading, an optional **`How to test:` numbered step list** (the click-through a human follows to reproduce it manually), then the **side-by-side `Dev | Local` comparison table** — each workflow's video row first, then one row per screenshot pair (`—` where a side has no capture, e.g. dev not yet deployed).

In the header, each `repo \`sha\`` in the `Dev deployed:` / `Local tested:` lines is a **clickable commit link** — the full project path is derived by matching the repo short-name against the MR URLs in the note, so a repo with no matching MR renders a bare code-span (never a broken link). A `Dev ± Local:`line then states, per repo present on both sides, whether dev and local are on the **same** commit (`= same commit`) or **differ** (`≠ dev \`<sha>\` vs local \`<sha>\``).

Artifacts always upload to the **ticket's own project** (resolved from the issue URL the note posts on), never to a manifest MR's repo or the overlay's CI project — a note only renders the uploads its own project claims, so the upload target follows the note.

The note is keyed on a hidden ticket marker `<!-- t3-e2e-evidence ticket=<n> -->` and carries a hidden machine-readable state blob `<!-- t3-e2e-data {…} -->` that is the source of truth. Each run **merges** the env(s) its manifest carries over the prior state: a `local`-only manifest fills/refreshes the Local column and freezes Dev; after merge + deploy a `dev`-only manifest fills the Dev column (and clears the "⚠️ Not yet on dev" line) while freezing Local. You never hand-dedup; re-running is always safe.

The command refuses bad evidence before any upload or post: invalid manifest JSON, a referenced artifact that does not exist, or a file whose extension is the wrong media kind (an image listed under a video slot, etc.).

Flags (all keyword-only):

| Flag | Required | Notes |
|---|---|---|
| `--manifest` | yes | path to (or inline string of) the test-plan manifest JSON |
| `--ticket` | no | pk / issue number / issue URL; falls back to the resolved worktree's ticket |
| `--title` | no | overrides the `## Test Plan — <title>` heading |
| `--mrs` | no | MR/PR URL(s) (repeat or comma-separate) — supplements the manifest's `mrs` |
| `--template` | no | body template: `capture-matrix` (default) / `browser-click-first` / `link-api`; overrides the manifest's. Pick it from the AC's modality (below). |

**Pick the `--template` from the AC's modality (§ "Modality — classify each AC").** The flag is how the modality classification becomes the actual note shape:

- `capture-matrix` (default) — the side-by-side `Dev | Local` red-boxed screenshot table. Use for **UI-feature** ACs where screenshots are the per-step compare-against reference. This template runs the red-box pixel gate (`/t3:e2e` § "Screenshot Sanity Check"), so every image must carry the highlight.
- `link-api` — links + code blocks per workflow, **no table, no images**. Use for **route-guard / RBAC / redirect / backend-boundary** ACs (a URL to navigate + an expected redirect/HTTP status, or a `curl` transcript) — the evidence is the URL and the request/response, not a screenshot. Because it carries no images, it skips the red-box gate entirely, so it is also the correct shape when the proof is a status code or golden-data check with no single visible element to box.
- `browser-click-first` — numbered manual steps with inline screenshots, for a click-through a human reproduces.

```json
{
  "ticket": "8521",
  "mrs": ["https://gitlab.com/group/client/-/merge_requests/6331",
          "https://gitlab.com/group/product/-/merge_requests/7585"],
  "dev":   {"commits": {"client": "<deployed-sha>", "product": "<deployed-sha>"},
            "missing_on_dev": ["client!6331 (unmerged)", "product!7585 (draft)"]},
  "local": {"commits": {"client": "<branch-sha>", "product": "<branch-sha>"}},
  "workflows": [
    {"workflow": "<test name>",
     "steps": ["Open the app", "Click the Login button", "Expect the dashboard"],
     "dev":   {"video": null, "images": []},
     "local": {"video": "local/run.webm",
               "images": ["local/step1.png", "local/step2.png"]}}
  ]
}
```

- One object per workflow; each carries its `dev` and `local` captures. A side's captures may be empty (e.g. dev before deploy) → that column shows `—`.
- `steps` (optional, workflow-level — shared across dev/local) is the written test plan: the numbered "how to test / where to click" list rendered above that workflow's table. Omit it and the block is omitted. It persists across re-runs — a later steps-less run keeps the recorded steps.
- `images` and the optional `video` are file paths **relative to the manifest's own directory** — the manifest sits at `$T3_E2E_ARTIFACTS_DIR/<TICKET>/manifest.json`, so a capture in the per-env directory (see the layout rule below) is referenced as `<env>/<file>`. Just paste what Playwright captured there.
- `dev.missing_on_dev` lists the MRs whose commits are not yet deployed — the note renders them as an expected gap so a dev column of `—` reads as normal, not a failure.

E2E artifacts live in a **dedicated directory per environment**, **outside every repo working tree**. The **runner** exports the resolved root — the per-ticket workspace's `.t3-cache/artifacts` — as `T3_E2E_ARTIFACTS_DIR` (core owns the path, so the no-artifacts-in-a-repo rule is structural, not each overlay re-deriving it; #3331). Override it with `--artifacts-dir` (refused when it resolves inside a repo working tree). Honour the variable rather than hard-coding a path. A capture for `env ∈ {dev, local}` lives at `$T3_E2E_ARTIFACTS_DIR/<TICKET>/<env>/<file>`. Writing captures to a **worktree-root** `artifacts/` puts binaries inside a product repo — the exact mistake `/t3:e2e` § "Artifacts Are Never Committed to a Product Repo" forbids. Capture every screenshot and recording for a given env under that env's directory — never mix a dev and a local capture in one folder, and never dump artifacts at the ticket root. Examples:

```
$T3_E2E_ARTIFACTS_DIR/8521/local/run.webm
$T3_E2E_ARTIFACTS_DIR/8521/local/step1.png
$T3_E2E_ARTIFACTS_DIR/8521/dev/run.webm
$T3_E2E_ARTIFACTS_DIR/8521/dev/step1.png
```

This makes wrap-up and manifest assembly trivial — a side's captures are exactly the files under `$T3_E2E_ARTIFACTS_DIR/<TICKET>/<env>/`, so building the manifest's `dev`/`local` blocks is a directory listing, and a re-run for the other env never collides with the first. `t3 <overlay> e2e post-test-plan` resolves relative artifact paths against the **manifest's own directory**, so keep the manifest beside its captures at `$T3_E2E_ARTIFACTS_DIR/<TICKET>/manifest.json` and reference them as `<env>/<file>`.
