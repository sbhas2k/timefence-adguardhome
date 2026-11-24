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
ruff check src tests scripts
ruff format --check src tests scripts
```

Tests never talk to a real AdGuard Home: `tests/conftest.py` and `tests/fakes.py`
ship an in-memory fake of the control API and the query log.

The runtime dependency set is pinned in `requirements.lock` (26 packages, with
hashes) and installed from that file by the image and `install.sh`. Regenerate it
with `uv pip compile` when you change `pyproject.toml` dependencies; CI runs
`pip-audit` against the requirements and fails on a known advisory.

`scripts/dev_server.py` runs the app against the fake AdGuard Home, which is the
quickest way to click through the UI locally.

## Ground rules

- Every user-facing string goes into `src/timefence/i18n.py` (English only for
  now, but the file is the single place a translation would touch).
- Never break the guarantees: a group is allowed only inside its allowed times
  (or while Free is on), the reconciler is idempotent, foreign AdGuard settings
  are preserved, and a clean stop blocks everything.
- New behaviour needs a test. Keep `ruff` clean (line length 100).
- No frontend build step, no CDNs, no inline scripts (the CSP forbids them).
- Database changes: add a new migration to `MIGRATIONS` in `db.py` and bump the
  schema version. Never edit an existing migration.
- Do not add secrets, internal notes, hostnames or personal paths to tracked
  files. `.gitignore` covers `.env` and common secret file names, and CI runs
  `gitleaks` over the full history on every push and pull request.

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
- Run the checks from [Development setup](#development-setup) before pushing;
  CI runs the same commands plus `shellcheck` on the DDNS helper.

## Releasing

1. Bump `version` in `pyproject.toml` and `src/timefence/__init__.py`.
2. Update `CHANGELOG.md` and, for anything owners must do, `docs/upgrading.md`.
3. `git tag v<version> && git push origin main --tags` — CI publishes the
   multi-arch image to `ghcr.io/sbhas2k/timefence-adguardhome`.
4. Point the deployment at the new tag; `:latest` moves with the release.
