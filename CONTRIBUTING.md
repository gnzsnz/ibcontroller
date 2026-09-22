# Contributing to ibcontroller

Thanks for considering a contribution. This document covers the contribution workflow; for
getting a dev environment running, see
[docs/development.md](docs/development.md) — this document doesn't repeat that.

## How to contribute

1. **Open an issue first.** Before starting work, open an issue describing the bug or feature —
   this avoids duplicated work and gets feedback on approach before code is written. Use the
   [issue template](.github/ISSUE_TEMPLATE.md) (`Problem`/`Scope`).
2. **Branch.** Create a branch off `master` with a descriptive name, e.g. `fix/restart-race` or
   `feature/management-settings`.
3. **Set up your environment.** Follow [docs/development.md](docs/development.md) — Python/Java
   toolchains, `pre-commit install`.
4. **Make your change**, following the project's working method: validate each layer before
   building on it, PoC against a real TWS/Gateway before committing to a design, unit tests
   once a layer is validated.
5. **Run the checks** before committing:

   ```bash
   uv run pytest
   uv run pre-commit run --all-files
   ```

6. **Commit.** Use a conventional-commit-style prefix (`feat:`, `fix:`, `refactor:`, `doc:`/
   `docs:`, `test:`) matching this repo's existing history (`git log --oneline`). No trailing
   whitespace; every user-facing string belongs in `labels.json`, not a Python literal.
7. **Open a pull request** against `master`, with a summary of the change and a link to the issue
   it addresses.

## Reporting bugs / requesting features

Open an issue using the [issue template](.github/ISSUE_TEMPLATE.md): what's the problem, what's
the scope of the fix or feature. Include TWS/Gateway version and platform (macOS/Linux) for
bug reports where relevant — this project's supported target platforms.
