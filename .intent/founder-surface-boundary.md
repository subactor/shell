# Bounded intent: Founder surface boundary

Refactor the Shell REPL so help text, aliases, exit behavior and prompt
presentation are projected from one typed command registry. Preserve the
existing `subactor-shell` command and runtime behavior while creating a stable
surface contract that Platform can delegate to in a later, separately reviewed
migration.

Authorized paths:

- `src/subactor_shell/repl.py`
- `src/subactor_shell/surface.py`
- `tests/test_shell_integration.py`
- `tests/test_surface.py`
- `tests/test_auth.py`
- `.intent/founder-surface-boundary.md`

The test boundary also removes live Control availability from unit-test
outcomes. Authentication probes must use a deterministic HTTP transport; live
connectivity belongs to an explicitly selected integration or smoke suite.

Non-goals: no takeover of the `subactor` executable, no Control mutation
changes, no provider or Vault changes, and no cross-repository runtime
dependency in this step.
