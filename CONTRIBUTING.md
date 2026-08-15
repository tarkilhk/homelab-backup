# Contributing

## Branch & Commit
- Use feature branches: `feat/<topic>`
- Conventional Commits: feat/fix/chore/docs/test

## Style
- Python: PEP8, type hints, ruff
- React/TS: Prettier, strict TS

## Tests
- Backend: pytest
- Frontend: vitest
- New features must include tests

## Releases

1. Choose the next [Semantic Version](https://semver.org/) and update `VERSION`,
   `backend/pyproject.toml`, `frontend/package.json`, and both root version fields
   in `frontend/package-lock.json`.
2. Move the release notes from `Unreleased` into a dated `## [X.Y.Z]` section in
   `CHANGELOG.md`.
3. Run `backend/.venv/bin/python backend/scripts/check_version.py`, then the
   backend and frontend checks documented in their READMEs.
4. Merge the release changes to `main`, create the `vX.Y.Z` tag from that exact
   commit, and push the tag.

CI rejects an invalid or unsynchronized version and any release tag that differs
from `VERSION`. Main builds publish `latest` and commit-SHA image tags; release
tags publish the versioned backend and frontend image tags.
