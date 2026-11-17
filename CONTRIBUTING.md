# Contributing

Thanks for helping! This is a small project, so the process is light.

## Development setup

```bash
git clone https://github.com/sbhas2k/timefence-adguardhome.git
cd timefence-adguardhome
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check src tests
ruff format --check src tests
```

Tests never talk to a real AdGuard Home: `tests/conftest.py` ships an
in-memory fake of the control API.

## Ground rules

- Every user-facing string goes into `src/timefence/i18n.py` (English only for
  now, but the file is the single place a translation would touch).
- Never break the guarantees: groups are blocked by default, sessions are
  capped, the reconciler is idempotent, foreign AdGuard settings are preserved.
- New behaviour needs a test. Keep `ruff` clean.
- No frontend build step, no CDNs, no inline scripts (the CSP forbids them).

## Pre-push hook

The repository ships a hook that runs `ruff` and `pytest` before anything is
pushed, so a broken commit cannot reach the remote (or CI):

```bash
git config core.hooksPath .githooks
```

Emergency bypass: `SKIP_HOOKS=1 git push`.

## Commits and PRs

- Conventional-ish commit subjects (`fix:`, `feat:`, `docs:`, `test:`...).
- One logical change per commit.
- PRs: describe the user-visible change, mention migrations if the schema
  changed (add a new migration, never edit an old one).

## Releasing

1. Bump `version` in `pyproject.toml` and `src/timefence/__init__.py`.
2. Update `CHANGELOG.md`.
3. `git tag v0.1.0 && git push origin main --tags` — CI publishes the
   multi-arch image to `ghcr.io/sbhas2k/timefence-adguardhome`.
