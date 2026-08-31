# Bounded intent: terminal interaction engine

Extract generic input interpretation from `ShellRepl` into a typed terminal
interaction engine. The engine owns normalization, exit/back navigation,
single-letter shortcut resolution and command-versus-message classification.
The REPL remains responsible for invoking application handlers.

Authorized paths:

- `src/subactor_shell/interaction.py`
- `src/subactor_shell/repl.py`
- `tests/test_interaction.py`
- `.intent/terminal-interaction-engine.md`

Acceptance:

- every input produces one explicit immutable event;
- rendered shortcuts and submitted shortcuts use the same command registry;
- empty, exit, back, command and message paths retain existing behavior;
- no Control, Planfile, provider, Vault or executable ownership changes;
- no runtime dependency on `subactor/platform`.

This change is stacked on the merge-clean Founder surface registry PR and must
remain independently reviewable for retargeting to `main` after that PR merges.
