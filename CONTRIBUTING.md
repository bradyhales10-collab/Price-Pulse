# Contributing to Price-Pulse

## Setup

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

For reproducible installs (recommended before a production collection run),
use the pinned lockfile instead:

```
.venv/bin/pip install -r requirements-lock.txt
```

## Running tests

```
.venv/bin/python -m pytest
```

The suite (327 tests as of this writing) does not require Playwright browsers
to be installed — it runs against fixtures, not live pages. Every push and
pull request to `main` also runs this suite automatically via
`.github/workflows/tests.yml`.

## Linting

```
.venv/bin/pip install ruff
.venv/bin/ruff check .
```

Config lives in `pyproject.toml`. Feel free to run `ruff check . --fix` for
mechanical fixes (import sorting, unused imports, modern syntax); anything
flagged that isn't auto-fixable (unused variables, default-argument calls,
etc.) is worth a second look rather than a blind fix, since it may be
intentional (e.g. FastAPI's `Depends()` pattern) or point at a real bug.

## Working with multiple agents/collaborators on this repo

Since this project may be worked on by more than one person or agent at a
time, a few conventions help avoid stepped-on toes:

- **One branch per task/feature.** Don't commit directly to `main`.
- **Use `git worktree` for parallel work** so two agents/sessions can have
  live working directories without colliding:
  ```
  git worktree add ../price-pulse-feature-x feature/x
  ```
- **Small, focused PRs.** CI must pass (pytest) before merging.
- **Anything touching `app/competitors/` or price-forensics logic** should
  include or update tests in `tests/` — this is the part of the codebase
  most sensitive to silent regressions.
- Never commit anything under `data/private/`, `.env`, or `*.db` files —
  these are gitignored for a reason (auth state and local databases).
