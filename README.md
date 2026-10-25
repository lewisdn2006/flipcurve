# FlipCurve

Photograph a secondhand item and get a price-to-time-to-sell curve. The curve is corrected for
survivorship bias with a censored Weibull survival model and compared across eBay and Vinted. Built at OxHack 2026.

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

The API currently serves **mock data** (`backend/mock/`, contract v1.1) so the frontend can build straight away.

## Run the whole thing (website + helper + this laptop)

1. **Helper extension:** Chrome → `chrome://extensions` → Developer mode → Load unpacked →
   `extension/`. The ID must be `fdibajhmjjbjcdfafdofkoedbkhbkfcb`. Stay signed in to eBay.
2. **Local test:** `uvicorn backend.main:app --port 8000`, then open
   http://localhost:8000/flipcurve/ in the same Chrome.
3. **Online:** `brew install cloudflared`, then `scripts/tunnel.sh quick` for a throwaway
   address, or `scripts/tunnel.sh setup` once and `scripts/tunnel.sh run` each time for
   https://flipcurve.lewisdn.com/flipcurve/ (API at https://api.lewisdn.com).

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
