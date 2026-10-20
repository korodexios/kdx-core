# Skill: cli-wizard-ux

Make every prompt crash-proof, per the chassis rulebook.

## When to use
Adding or editing a command, prompt, menu, or the wizard.

## Rules
- Commands are declared, not coded: `CliCommand` / `CliArgument` feed both argparse
  (`cli/surface.py`) and the rich wizard (`cli/wizard.py`).
- Numbered menus: `[1]`, `[2]`; ENTER accepts the bracketed default `[8080]`.
- Every free-text input runs in a `while True` validation loop; invalid input prints a
  polite `[!]` message with the expected shape, then re-prompts. Never a stack trace.
- Mandatory `--yes` plus non-TTY auto-detection: CI must never block on a question.
- Tags: `[✓]` success, `[?]` question, `[!]` problem, `[i]` note; the ASCII fallback
  is applied once, in `cli/render.write_line`.
- Diagnostics go to stderr; the operator contract owns stdout.

## Verification
`echo | korodex wizard` and `korodex --yes status` both finish without prompting.
