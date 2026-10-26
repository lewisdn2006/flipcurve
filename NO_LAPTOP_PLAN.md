# Plan: FlipCurve without the laptop

Goal: lewisdn.com/flipcurve works with no Python server, no tunnel and nothing to start by hand.
The helper extension stays: it is the only reliable way to read eBay sold listings and Vinted.

Written 26 Oct 2026 from the code as it stood at commit `7c619cb`.

## Where things stand now

```
Page (lewisdn.com/flipcurve, served from GitHub by the flipcurve-page Worker)
  │  photo ──────────────► api.lewisdn.com ──tunnel──► laptop: FastAPI (backend/main.py)
  │  "Scan" ─► extension fetches eBay/Vinted pages in your Chrome                ├─ Gemini / Gemma / Claude identify
  │            └─ HTML pages ─► api.lewisdn.com/api/v1/scan-pages ─tunnel─►      ├─ parse pages (BeautifulSoup)
  │                                                                              ├─ Weibull model (numpy/scipy)
  └─ extension, every 2 h: recheck-due / recheck-results ─tunnel─►               └─ files: cache_seed/*.json,
                                                                                     ebay_calibration.sqlite,
                                                                                     vinted_calibration.json,
                                                                                     model_cooldowns.json
```

Only the laptop box has to go. The page and the extension can stay almost as they are.

## Where things end up

```
Page ──► api.lewisdn.com = new Worker "flipcurve-api" (JavaScript) ──► Gemini / Claude APIs
Extension ──► same Worker                                          └──► D1 database "flipcurve-db"
```

- **Keep the API the same.** Same routes, same JSON shapes (`backend/models/schemas.py`, `contract/`).
  The page needs at most one small change (step 3), and the extension needs none: its
  default API is already `https://api.lewisdn.com`.
- **The cutover is a DNS switch.** Today `api.lewisdn.com` points at the tunnel. At the end
  it becomes the new Worker's custom domain. Until then, build and test the Worker on its
  workers.dev address using the page's `?api=` override, so the laptop setup keeps working
  throughout.

### Why rewrite in JavaScript instead of hosting the Python as it is

Cloudflare Containers could run the existing FastAPI app almost unchanged. But a container
has no lasting disk, so the files would have to move into a database anyway. It also can't
connect to D1 directly, it starts slowly after going idle, and it is billed separately. A
JavaScript Worker with D1 matches the other apps on lewisdn.com (td, lego), starts instantly,
and fits the Workers plan you already pay for. The Python stays in the repo as the reference,
and its tests and saved outputs are how the port gets checked.

### What does not get ported

- `backend/scrapers/fetcher.py`, Playwright and `backend/db/browser_profile/`: server-side fetching, which the extension replaces.
- The live branch of `/api/v1/market-scan` (`allow_live`): the page only calls it with `allow_live: false`.
- `backend/vision/opencv_pipeline.py` (a stub) and `backend/engine/simulate.py` (test data only).

## Steps

Each step ends with something you can test. Sizes are rough: S = an evening, M = a few
sessions, L = the main chunk of work.

### 0. Save reference outputs from the Python (S)

First, capture real pages to test against. `tests/fixtures/` only has
`vinted_upload_samples.json` and no page HTML. Have `/api/v1/scan-pages` save its request
body to `tests/fixtures/pages/` for a few scans (trainers, a phone, a bike, a T-shirt), plus
one `recheck-results` body.

Then write `scripts/make_golden.py`. It runs the current Python on fixed inputs and saves
the results to `tests/golden/`:
- the parser and `ingest` output for those saved pages
- `pipeline.run` on a handful of `cache_seed/` items (trainers, a phone, a bike, a T-shirt)
- `survival_model.fit` results (`psi`, `cov`, quantiles) for the same items

Every later step is checked against these files: numbers must match within a small tolerance.

### 1. Worker skeleton, database and secrets (S)

- New folder `web/api-worker/`: `wrangler.jsonc` and `src/`, plus `vitest` with
  `@cloudflare/vitest-pool-workers` for tests.
- `GET /api/v1/health` and CORS limited to `https://lewisdn.com`, `https://www.lewisdn.com`,
  `localhost` and the extension's origin.
- D1 database `flipcurve-db`. Secrets (`wrangler secret put`): `GEMINI_API_KEY`, and
  optionally `ANTHROPIC_API_KEY`, `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`.
- Set `limits.cpu_ms` (for example 60000), because the model fit plus parsing several MB of
  HTML can take more than the default CPU time.

### 2. Move the stored data into D1 (S–M)

| Now (file) | D1 table |
|---|---|
| `backend/db/ebay_calibration.sqlite` | `ebay_calibration` (copy the same columns) |
| `backend/db/vinted_calibration.json` | `vinted_calibration` |
| `backend/scrapers/cache_seed/<slug>_<platform>.json` (27 files, 12 MB) | `scan_cache(query_slug, platform, rows_json, updated_at)` |
| Vinted history / recheck state (inside `page_ingest.py`) | `vinted_listings(id, query, first_seen, last_checked, status, relevant, ...)` |
| `backend/db/model_cooldowns.json` | `model_cooldowns(model, until)` |

Write a one-off `scripts/export_to_d1.py` that produces SQL inserts, and load them with
`wrangler d1 execute flipcurve-db --remote --file=...`.

### 3. Identify (M)

- Port `backend/vision/gemini_identifier.py` (about 800 lines): prompt, model choice and
  fallbacks (Gemini → Gemma → Claude), JSON extraction and repair, cooldowns (now in D1).
- Shrink photos **in the page** before upload: a canvas resize to 1600 px JPEG replaces Pillow.
  HEIC photos that Chrome can't draw are sent unchanged, since Gemini accepts `image/heic`.
- The page currently starts a job and polls it (`/identify/start`, `/identify/job/{id}`).
  A Worker can wait as long as the request stays open, so the simplest change is for the page
  to call the plain `/api/v1/identify` and `/api/v1/identify-text` routes and wait. If you
  want to keep polling, store jobs in D1 and run them with `ctx.waitUntil` (but that stops
  30 s after the response, which is too short for the 160 s Gemma wait).
- Test: same photos as `tests/test_identifier.py`. Answers should be the same kind of item
  with the same search terms.

### 4. Parsers and page ingest (M)

- Port `ebay_parse.py`, `vinted_parse.py`, `page_ingest.py`, `outlier_filter.py`,
  `ebay_calibration.py`, `vinted_calibration.py`, and the eBay Browse API client
  (`ebay_api.py`, if you use it).
- Use `linkedom` (a pure-JS DOM that runs in Workers) in place of BeautifulSoup, so
  selectors carry over almost line for line.
- Accept gzip request bodies (`DecompressionStream`), as `/scan-pages` does now.
- Test: parser output equals `tests/golden/` for every fixture.

### 5. The model and the pipeline (L)

- Port `survival_model.py`, `pipeline.py`, `platform_economics.py`, `platform_config.py`.
- Maths functions scipy gave for free: `gammaln`, `digamma`, regularised lower `gammainc`
  (a series plus continued fraction, as in Numerical Recipes). Test them against scipy
  values saved in step 0.
- Optimiser: about 7–8 parameters, two of them bounded (`beta1 in [b_min, 8]`,
  `ln k in [ln 0.3, ln 3]`). Either write a small projected L-BFGS / BFGS with numerical
  gradients, or map the bounded parameters through a sigmoid and use plain BFGS. Keep the
  "parameter on a bound counts as fixed" rule when inverting the Hessian. A small matrix
  inverse with a pseudo-inverse fallback is enough at 8×8.
- Test: `psi` and the curve quantiles match `tests/golden/` within about 1%, and
  `tests/test_survival_math.py` cases pass when rewritten in vitest.

### 6. Scan routes and background rechecks (S)

- `/api/v1/scan-pages`, `/api/v1/market-scan` (saved data only), `/api/v1/recheck-ids`,
  `/api/v1/recheck-due`, `/api/v1/recheck-results`, all on D1.
- Test: run `tests/test_contract.py` and `tests/test_scan_pages.py` against the Worker's
  address (point the test client at a URL), or rewrite them in vitest.

### 7. Cutover (S)

1. Full scan from the live page with `?api=https://flipcurve-api.<account>.workers.dev`.
2. In Cloudflare, remove the tunnel's `api.lewisdn.com` record. Add `api.lewisdn.com` as the
   Worker's custom domain in `wrangler.jsonc` and deploy.
3. Turn the laptop off, then scan from another computer's Chrome with the extension, and
   identify a photo from your phone.
4. Update `README.md` ("Run the whole thing"), and retire `scripts/tunnel.sh`.

### 8. Optional, if other people will use it

- Publish the extension on the Chrome Web Store (one-off developer fee) so people don't need
  Developer mode. Its permissions and description are already written for that.
- Limit identify calls per visitor with Workers rate limiting, since they all use your
  Gemini quota.
- On a phone, live scanning needs a browser with extensions. Firefox for Android can run
  MV3 extensions, and Safari on iPhone would need an Xcode app wrapper. Without either, a
  phone can still identify items and use saved data, plus active eBay listings via the
  official Browse API, which works from the server. Sold eBay data and Vinted still need
  the extension.

## What still needs a computer afterwards

Nothing has to be run. A live scan needs a desktop Chrome with the extension, signed in to
eBay (eBay only shows sold listings to signed-in users). The 2-hourly Vinted re-checks only
happen while Chrome is open somewhere with the extension.

## Order and risk

Steps 0 → 1 → 2 first. After that, 3 is independent of 4–6, and 3 alone already takes
identification off the laptop: point only `/identify*` at the Worker by routing that path,
or switch the page's identify calls to it. The riskiest step is 5, because a port of the
model can drift slightly from the Python. Step 0's saved outputs are there for that.
