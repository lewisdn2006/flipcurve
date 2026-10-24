# FlipCurve

Photograph a secondhand item and get a price-to-time-to-sell curve. The curve is corrected for
survivorship bias with a right-censored Weibull survival model and compared across eBay, Vinted and
FB Marketplace. Built at OxHack 2026.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # add GEMINI_API_KEY
python scripts/check_keys.py       # should print GEMINI: OK
uvicorn backend.main:app --reload --port 8000
# open http://localhost:8000/docs
pytest                             # contract tests, keep green
```

The API currently serves **mock data** (`backend/mock/`) so the frontend can build straight away.

## Read first

1. `DECISIONS.md` — the Phase 0 calls the contract is built on.
2. `contract/API_CONTRACT.md` — routes, price vocabulary, which field drives which UI element.
3. `backend/models/schemas.py` — the frozen contract itself.

## Who owns what

| Area | Owner | Files |
|---|---|---|
| Survival model, outliers, economics | Person 1 | `backend/engine/*`, `tests/test_survival_math.py` |
| Vision, scrapers, cache, API wiring | Person 2 | `backend/vision/*`, `backend/scrapers/*`, `backend/db/*`, `backend/main.py` |
| Frontend | Person 3 | `frontend/` (types already in `frontend/src/types/api.ts`) |

Shared, change only with the team: `backend/models/schemas.py`, `contract/`, `backend/mock/`.

## Regenerate mocks, schema and TS types

```bash
python scripts/make_mock.py
npx json-schema-to-typescript@15 -i contract/schema.json -o frontend/src/types/api.ts --unreachableDefinitions
```
