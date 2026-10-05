# Compiler/VM environment composition

This integration campaign made the canonical `mncs-compiler → mncs.vm.artifact/1 → mncs-vm` path selectable and observable through Environment, Test, Forge, Debug, and Doctor. The selection and evidence boundaries remain provider-owned: Environment binds provider capabilities and records their identities; it does not lower source or interpret VM artifacts.

The [integration matrix](integration-matrix.json) records the before/after edges for 14 consumers. The [cache inventory](cache-inventory.json) records exact duplicate immutable payloads without deleting caches.

## Bound roles and proof

The working composition keeps three independent roles:

| Role | Selected identity | Evidence |
| --- | --- | --- |
| Stage-0/reference | `mncs-language` revision `174c4b427dc40e49b93076b266a922d21c0b9946`, executable SHA-256 `5a913164b39cdb256a34c706915634552691d995c9469a915d41a20e42eb560f` | Exact selected checkout and executable observed; build origin remains unknown. |
| Next-generation producer | `mncs-compiler` revision `1f0b94a9f84f493f8ac5a88cf8ea269a7018909f`, executable SHA-256 `2c3165819cb669b763c097e2e20bb9362671c72072f27f80704bb9c60bc289c0` | Producer identity `sha256:23c58d850751e76575d8551ed4cb31b54520e0d7eac905282edb794ee53ff48e`; local build-input receipt names Stage-0 pin `a3ac17df69e68f6373cbff336db0a572667d73da`. The receipt is a local observation, not an independent attestation or self-hosting claim. |
| Canonical runtime | `mncs-vm` revision `f4cf251bc218626e4de1135424c38d9a93403654`, executable SHA-256 `096452244f4413d46da5ed55f9faec8650a6c27c13b83fb8c86b6cfaa1304517` | Runtime contract `mncs.vm.runtime-provider/1`; executable build origin remains unknown. |

The normal Environment entry `ses_fc0c87a5da16be2f` selected those exact paths and passed the required Doctor service `mncs-doctor:compiler-vm-coherence`. Its provider-owned `/status == pass` observation is tied to execution-composition identity `0ccf0b1adae7bab3`, so Environment reports `execution_stack.compatibility.state = verified`. Replacing a selected executable changes the role identity and makes old compatibility evidence unproven until the selected service runs again. The full workspace readiness remains `degraded` with no blocking requirements because 51 optional capabilities are unavailable; the selected compiler/VM compatibility check is ready.

The resumed claim-bound integration proof completed in session `ses_1f494c27a2c47b0b`, checkpoint `chk_f7b4ee1ffeae33dd`. It used normal Environment capability invocation for producer inspection, artifact emission, warm-cache reuse, VM admission/call, Test, Forge, Debug, and Doctor. It took 160.742 seconds end to end. The direct call's 1,000-step request was observed as a 1,000-step VM limit. The producer and runtime identities, sealed artifact identity, result, and invocation records are under `/tmp/mncs-coherence-proof/final/` on the campaign host.

The small direct call artifact was 70,642 bytes (`sha256:b1ced378dbddc85a9ada3fdc2e423c0beb76bc7e69aa2ec017f547c3c8d856f5`). The real Test self-suite artifact was 1,173,516 bytes (`sha256:6c9723ac4e80e0b7f49ccd43d61d5270475c5ab90bbfb7d768d6f27ce6b0070b`). Repeating emission reused the content-addressed product. The normal Test result retained six passes and one skip; normalized Stage-0/reference and canonical-VM result rows were identical, digest `796350dcbbf34b6d74376f5d52e83a54e97d4d403ac00a5ceb6552d59a5e957d`. The skipped case remains a skip.

The same campaign's segment and decl comparisons retained three-way reference/VM/Cranelift semantic equality:

- Segment, 63 cases: `cc235fb51a63ed685105a83c2f04966a77d6af29e84535408874d5b4a791dcc5`.
- Decl, four cases: `8bc4c512e31f90c747fe948169bc88c90e830f33de717a1b3cdea7a7be32bbf7`.

## Consumer changes and retained lanes

`mncs-compiler` exposes the direct compiler artifact producer through provider-owned contracts and a content-addressed product receipt. It still labels its producer as a Stage-0 bootstrap/direct emitter over verified SSA; no self-hosting claim was added. `mncs-vm` exposes runtime description, sealed-artifact admission, bounded call, and retained session contracts, with exact executable identity in execution provenance.

`mncs-test vm` routes VM-suitable native suites through direct producer output and one bounded VM session. The regular `mncs test` reference/bootstrap lane and explicit `bin/mncs-test-compat` adapter remain separate. The compatibility adapter's research-bytecode path is deliberate and cannot silently become the canonical VM path.

Forge now selects compiler and runtime independently and retains a VM process for pure supported native calls. Effectful host-grant work stays on the explicit Stage-0/embed lane until VM host capability providers are selected; incomplete canonical selection fails closed. Debug consumes direct compiler products, checks receipt and source-map bytes, and correlates selected SSA operation identities with the live VM debug stream. Imported/generic operations without compiler source correspondence remain unmapped.

Doctor reports reference, producer, runtime, and their compatibility separately. Its bounded smoke checks standalone admission and known-answer execution. Cranelift and portable-WASM readiness remain optional and are not converted into global blockers. Automation, Store, Language Service, Harness, RAVEL, and Fabric were audited but not changed: their current helper/effect, storage, active-work, differential, or placement roles do not justify a blanket runtime migration. Store can retain VM bytes through existing opaque content-addressed APIs.

Research-bytecode remains in explicit reference, compatibility, native-effect, and backend-conformance lanes in Language, Test, Forge, Doctor, compiler policy, Automation, Harness/RAVEL/Fabric. It is absent from the direct canonical compiler→VM call and from the new canonical Test, Forge, Debug, and Doctor proof paths. The compiler migration module remains deleted; there are zero migration callers.

## Caches, tests, and limits

The exact-file scan found 22 duplicate payload groups (over 100 KB each) across Debug, Forge, Automation, Test, and the compiler probe cache, totaling 3,111,476,021 redundant bytes by byte identity. Those provider-local hot caches were left intact because age, liveness, and cache ownership are not interchangeable. New immutable producer products use content identities inside caller-selected caches; Store remains opaque storage authority. At final inspection, the campaign host had 17 GiB free disk. The shared Store state pre-existed this run and was not vacuumed. An accidentally generated 420.9 MiB duplicate Cargo target was removed; the selected `.bootstrap/target` remains the only compiler probe build target used by Environment.

Validation included: compiler provider tests (3 passed); VM offline Cargo tests (passed); Test transport proof (passed); Forge canonical proof plus 87-test regression set (passed); Debug direct-product test (passed); Doctor coherence test and selected-stack smoke (passed); Environment composition/toolchain/entry tests (45 passed), composition tests (7 passed), and readiness-bound tests (2 passed). The wider Environment test run reported 148 passed and one failure in the existing `ReconcilerTests.test_restart_resumes_cursor_without_replay` event-replay case, also present in the pre-campaign baseline.

Language Service stayed on `campaign/continuous-hardening-cursor-safety-20260922`; its dirty `modules.rs`, `render.rs`, fixture manifest, and `.worktrees/` were not touched. CP-0024 and CP-0025 remain open and unchanged. Compiler executable provenance is stronger but still locally observed; VM and selected reference executable build origins remain unknown. No Stage-1 feature campaign was run. The next compiler frontier remains the record literal `{` at `mncs-compiler/src/compiler/parser.mncs`, bytes 710–711.
