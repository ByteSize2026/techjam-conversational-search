# Add interactive CLI for shopping agent

## Goal

Add a small, dependency-free REPL so a developer can demonstrate the existing
shopping Agent one turn at a time from a terminal.  The CLI must exercise the
same `Agent.reset`/`Agent.respond` contract used by the evaluators and expose
both the official offline path and the optional natural-language model path.

## Requirements

- Add a runnable module with a stable command such as
  `python3 -m starter.cli`.
- Support `--protocol-profile official|natural_language` and the existing
  catalog configuration path used by `Agent`.
- Start a new session, accept user messages from stdin until EOF or an
  explicit exit command, and increment turns from 1 through 10.
- Print the Agent response in a readable form: message, ask_attribute,
  ordered recommendations, and reported usage when present.
- In natural-language mode, make it possible to see the accepted intent/model
  diagnostics without exposing secrets or evaluator-only target fields.
- Keep the CLI offline-first: no new runtime dependency and no implicit model
  or network activation.  DeepSeek remains opt-in through existing environment
  variables.
- Add focused tests for argument parsing, turn/session behavior, EOF/exit, and
  response formatting/error fallback.
- Document offline and online invocation in the project README and the
  Devpost submission draft.

## Acceptance Criteria

- [x] `python3 -m starter.cli --help` exits successfully and documents both
      profiles.
- [x] An offline scripted stdin session produces one response per turn and
      never requires an API key or network.
- [x] A configured online profile is passed to `Agent` without the CLI
      handling or printing the API key.
- [x] EOF and `exit`/`quit` terminate cleanly; turns beyond 10 are rejected or
      end the session with a clear message.
- [x] Tests pass using only the repository's existing standard-library test
      setup.
- [x] README and Devpost docs contain copy-pasteable commands for both modes.

## Notes

- Keep `prd.md` focused on requirements, constraints, and acceptance criteria.
- Lightweight tasks can remain PRD-only.
- For complex tasks, add `design.md` for technical design and `implement.md` for execution planning before `task.py start`.
