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

## Run it on a laptop (local development; the live site uses the Worker above)

1. **Helper extension:** Chrome → `chrome://extensions` → Developer mode → Load unpacked →
   `extension/`. The ID must be `fdibajhmjjbjcdfafdofkoedbkhbkfcb`. Stay signed in to eBay.
2. **Local test:** `uvicorn backend.main:app --port 8000`, then open
   http://localhost:8000/flipcurve/ in the same Chrome.
3. **Online:** `scripts/tunnel.sh quick` (downloads cloudflared the first time if needed) for a throwaway
   address, or `scripts/tunnel.sh setup` once and `scripts/tunnel.sh run` each time to put the
   API at https://api.lewisdn.com, which the live page uses.
4. **The page is live at https://lewisdn.com/flipcurve/**: a small Cloudflare Worker
   (`web/worker/worker.js`, "flipcurve-page") serves `web/flipcurve/index.html` from GitHub, so a
   push to main updates it within a minute. The rest of lewisdn.com is untouched.

## Without the laptop: the Cloudflare Worker

`web/api-worker` is the same API in JavaScript on Cloudflare (`NO_LAPTOP_PLAN.md`): identify,
scans, saved data and the extension's re-checks, with data in the D1 database `flipcurve-db`.
Since 26 Oct 2026 it answers at https://api.lewisdn.com (a route in its `wrangler.jsonc`), which
the live page and the helper extension use, so nothing needs to run on a laptop. The laptop
setup below still works for local development (http://localhost:8000/flipcurve/).

```bash
cd web/api-worker
npm install
npm test              # Workers runtime tests, then checks against the Python's results (tests/golden)
npx wrangler deploy
```

The Python server stays as the reference: after changing the maths or the page readers there,
rerun `scripts/make_golden.py`, port the change, and `npm test` says whether the two still agree.

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
