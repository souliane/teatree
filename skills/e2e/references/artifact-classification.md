# Artifact, fixture, manifest — which file belongs where

The classification behind `/t3:e2e` § "Artifacts Are Never Committed to a Product Repo". That section carries the rule; this file carries the three-kind table, where run provenance lives, and what a private test repo may legitimately track.

Three kinds of file get confused for one another — classify before deciding where each belongs:

| Kind | Example | Home |
|---|---|---|
| **Artifact** — *records* a run | `step1.png`, `run.webm` | Never committed to a product repo; cited by artifacts-root-relative path in the plan file. |
| **Fixture** — *produces* state | flag/message seed, API seed | The spec's own `beforeAll` / fixture, in the specs tree. Never a loose script under `artifacts/`. |
| **Manifest** — *authored intent* | `manifest.json` (workflow names, human `steps`, claim→capture mapping) | Hand-written, but it is an artifact too: it lives beside the captures it maps, at `$T3_E2E_ARTIFACTS_DIR/<TICKET>/manifest.json` — outside every working tree, never committed to a product repo. Once written, the plan file's hidden state blob holds the durable copy. |

The **run provenance** is DB-home, not in the tree — never re-derive it from files. `Ticket.extra['e2e_recipe']` records the run's sha and env; the rubric score lives on the `Rubric` model and the posted-note URL on `E2eMandatoryRun.posted_url`.

A **private** test repo that legitimately tracks its manifest (the artifacts are the deliverable there) must commit only the *authored* half — never the per-run commit SHAs / `missing_on_dev`, which churn the file on every push (#3092). `t3 <overlay> e2e tracked-manifest --manifest <path>` prints that authored half (the top-level `dev`/`local` provenance blocks removed) so two runs produce a byte-identical tracked file. Keep the full manifest out-of-repo for `write-test-plan`; commit the stripped output.

A loose `seed-*.py` under `artifacts/` is a smell: fixture logic escaped the spec. Fold it into the spec's fixture and delete the script, or the next run silently depends on a human having executed it by hand.
