# Ambient semantic projections

Enter normally. Explicit `.mncs/projections.json` inventories name semantic
subjects (owner identity, file and JSON pointer), renderer/schema versions,
ownership, protected edit policy and validation. `.mncs/project.json` points
to that inventory and pins an optional MNCDS structure profile.

Environment observes selected values; Automation admits targeted reconciliation;
Doctor diagnoses health/admissible repair; Forge executes pure owner artifacts;
Store retains CAS state and exact provenance. The semantic render protocol
returns content rather than accepting arbitrary ambient write arguments.
Only claimed, revalidated target preimages are replaced. Pending write
identities allow interrupted reconciliation to recover without adopting edits.

Whole-file ownership requires missing/current output or an exact migration
preimage. Mixed ownership protects the delimited body and preserves surrounding
bytes. Generated writes are not source events. Renderer/schema identities and
actual selected renderer inputs invalidate cached state. Identical calls have
no renderer/planner launches or projection-state writes.

Inspect without maintaining another file:

```bash
./scripts/mncs-env projections SESSION --interpret capabilities --repository mncs-doc
./scripts/mncs-env projections SESSION --interpret blockers --repository mncs-doc
./scripts/mncs-env projections SESSION --interpret architecture --repository mncs-doc
./scripts/mncs-env projections SESSION --interpret structure --repository mncs-doc
./scripts/mncs-env projections SESSION --interpret why --repository mncs-doc
```

Repositories without declarations retain normal behavior. Migrate authored
artifacts to mixed ownership first; an occupied target without an exact declared
preimage defers safely. Roadmap intent is authored; current owning evidence
determines its execution state. Rendering is never a Test PASS verdict.
