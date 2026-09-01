# Bounded intent: command-handler registry

Bind every executable Shell command to a domain handler declared in the same
registry that renders help and resolves shortcuts. Fail closed when a command
has no handler or a handler is not installed in the REPL dispatcher.

Authorized paths:

- `src/subactor_shell/surface.py`
- `src/subactor_shell/repl.py`
- `tests/test_surface.py`
- `tests/test_shell_integration.py`
- `.intent/command-handler-registry.md`

Acceptance:

- `/login` and `/auth` are visible in the registry and generated help;
- all executable registry entries declare a handler id;
- aliases resolve to the same handler as their canonical command;
- the REPL validates registry/dispatcher completeness before prompting;
- command behavior and mutation confirmations remain unchanged;
- no Platform dependency or Control/Planfile API change.
