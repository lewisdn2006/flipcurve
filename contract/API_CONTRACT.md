# FlipCurve API contract — v1.0 (frozen)

Source of truth: `backend/models/schemas.py`. Generated from it: `contract/schema.json` and
`frontend/src/types/api.ts`. Example payloads: `backend/mock/`.

To change the contract: edit `schemas.py`, run `python scripts/make_mock.py`, regenerate the
TS types (command at the bottom), run `pytest`, and tell the team. Additive changes only
(new Optional fields) after Phase 0.

## Routes

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/v1/health` | – | `{ok, mode}` |
| POST | `/api/v1/identify` | multipart: `images` (1–2 files), optional `hint` | `IdentifyResponse` |
| POST | `/api/v1/identify-text` | JSON `IdentifyTextRequest {query, condition_tier?}` | `IdentifyResponse` |
| POST | `/api/v1/market-scan` | JSON `MarketScanRequest {item: ItemSpec, platforms?, allow_live?}` | `MarketScanResponse` |

Flow: identify (photo or text) → user edits `item` in the form → send the edited `item` to
market-scan → everything else happens in the browser.

## Price words (use these exactly)

* **strike** (`price_gbp` on the grid, the slider value) — the price the item actually sells
  for, excluding postage and buyer fees.
* **list** (`list_price_gbp`) — what to type into the platform: `strike / (1 - offer_buffer)`.
* **net payout** (`net_payout_gbp`) — cash the seller keeps.
* **total buyer price** (`ListingRow.total_buyer_price_gbp`) — item + postage + buyer fees;
  engine-only, for comparing listings across platforms.

## What the frontend reads

| UI element | Field |
|---|---|
| Slider bounds | `price_range.min_gbp`, `max_gbp`, `step_gbp` (identical for all tiers) |
| Condition toggle | switch which array of `price_curve_grid[tier]` you read; no refetch |
| Preset buttons | `presets[tier].sell_24h_gbp` / `best_ev_gbp` / `patient_gbp` |
| Big numbers | interpolate the current tier's grid at the slider price: `expected_days_to_sell`, `ci_days_low`–`ci_days_high`, `prob_sell_24h/7d/30d`, and `platforms[best_platform].net_payout_gbp` |
| Chart | the whole `price_curve_grid[tier]` array; crosshair at slider price |
| Bias banner | `market_summary.active_median_gbp`, `sold_median_gbp`, `survivorship_bias_pct` |
| Condition strip | `market_summary.condition_buckets[tier].p25/median/p75` |
| Platform cards | `price_curve_grid[tier][i].platforms[platform]` at the slider price (initial view: `platform_recommendations`) |
| Markdown timeline | `markdown_schedule` |
| Evidence drawer | `comparable_listings` (badge from `obs_type`: `active` → "Unsold", others → "Sold") |
| "How it works" drawer | `model_fit` |
| Data-source badge | `market_summary.data_source`: `live` / `db_cache` / `seed` / `mock` |

Durations are clipped at **365** days. Show 365 as "1 year+".

## Slider interpolation (reference)

```ts
import type { CurvePoint } from "./types/api";

export function atPrice(grid: CurvePoint[], price: number) {
  const i = Math.max(0, Math.min(grid.length - 2,
    grid.findIndex((p) => p.price_gbp > price) - 1));
  const a = grid[i], b = grid[i + 1];
  const w = Math.min(1, Math.max(0, (price - a.price_gbp) / (b.price_gbp - a.price_gbp)));
  const lerp = (x: number, y: number) => x + (y - x) * w;
  return {
    price,
    prob24h: lerp(a.prob_sell_24h, b.prob_sell_24h),
    prob7d: lerp(a.prob_sell_7d, b.prob_sell_7d),
    prob30d: lerp(a.prob_sell_30d, b.prob_sell_30d),
    days: lerp(a.expected_days_to_sell, b.expected_days_to_sell),
    daysLow: lerp(a.ci_days_low, b.ci_days_low),
    daysHigh: lerp(a.ci_days_high, b.ci_days_high),
    nearest: w < 0.5 ? a : b, // use for platform cards / best_platform
  };
}
```

Note `findIndex` returns -1 above the top of the grid, which the clamp handles.

## Regenerating the TS types

```bash
python scripts/make_mock.py   # rewrites mocks + contract/schema.json
npx json-schema-to-typescript@15 -i contract/schema.json -o frontend/src/types/api.ts --unreachableDefinitions
```
