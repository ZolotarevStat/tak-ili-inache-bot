# «Так или иначе»

Private Telegram bot MVP for a football prediction game. Participants build a
five-bet coupon with a fixed 5,000-unit bank; the server validates the coupon,
deadline and current line before saving it. The administrator imports fixtures,
publishes predictions, records results and runs scoring.

The current source snapshot corresponds to local candidate
`0.1.0-cjm-v1-2-close-integrity-20260825-local`. Deployment and a live Telegram
smoke are separate release gates; cloning this repository does not deploy or
start a bot.

## Local setup

Python 3.12 or 3.13 is recommended.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
```

Create local configuration from `.env.example`. Keep the real `.env` outside
Git and never paste bot tokens, Telegram user IDs or group chat IDs into source,
tests, issues or logs.

## Verification

The test suite is local and does not call Telegram or require credentials:

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=src .venv/bin/python -m compileall -q src tests scripts
bash -n deploy/tak-ili-inache-admin \
  deploy/tak-ili-inache-upgrade-wrapper \
  deploy/tak-ili-inache-backup \
  deploy/tak-ili-inache-prune-releases
```

The audited candidate passed 145 unit/integration tests. See `LOCAL_SMOKE.md`
for the bounded tester workflow and `RUNBOOK.md` for deployment and recovery
procedures. All addresses and identifiers in committed documentation are
placeholders; secrets must be supplied only through the deployment environment.

## Repository contents

- `src/tak_ili_inache/` — bot, validation, persistence, scoring and reporting;
- `tests/` — unit and integration coverage with fake Telegram transport;
- `data/fixtures_sample.csv` and `data/fixtures_smoke_20260907.csv` — synthetic
  fixtures required by tests and smoke preparation;
- `deploy/` and `scripts/` — explicit operational helpers;
- `CJM_SPEC.md`, `LOCAL_SMOKE.md`, `RUNBOOK.md` and `RELEASE_MANIFEST.md` —
  product, smoke and release contracts.

Runtime CSV files, drafts, participant exports, results, backups, logs, generated
charts, private pilot fixtures and credential files are intentionally excluded.
