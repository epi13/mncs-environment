# Implementation Landing Zone

Runtime implementation will live under `src/` once the canonical contracts and representation choices are established.

Do not use this directory as permission to pick a stack by habit. The first implementation should be chosen to integrate cleanly with MNCS's existing compiler/runtime/service architecture and to satisfy the contracts in `docs/` and `rfcs/0001-environment-session-model.md`.

Initial implementation work should prioritize a narrow vertical proof:

1. resolve a real workspace,
2. construct a durable EnvironmentSession,
3. attach a WorkIntent,
4. bind at least one real MNCS capability,
5. enforce an explicit protected scope,
6. observe a typed result/event,
7. checkpoint and resume in a fresh consumer.

Broaden the implementation only after that path is real.
