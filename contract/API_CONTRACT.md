# FlipCurve API contract — v1.1

Source of truth: `backend/models/schemas.py`. Generated from it: `contract/schema.json` and
`frontend/src/types/api.ts`. Example payloads: `backend/mock/`. The maths behind every number
is in the doc "FlipCurve: The Maths Behind Time-to-Sell".

To change the contract: edit `schemas.py`, run `python scripts/make_mock.py`, regenerate the
TS types (command at the bottom), run `pytest`, and tell the team. Additive changes only
(new Optional fields) from here on.

**v1.1:** eBay and Vinted only (FB Marketplace deferred). No price slider: the frontend
draws charts. The x-axis everywhere is the **list price**.

## Routes

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/v1/health` | – | `{ok, mode}` |
| POST | `/api/v1/identify` | multipart: `images` (1–2 files), optional `hint` | `IdentifyResponse` |
| POST | `/api/v1/identify-text` | JSON `IdentifyTextRequest {query, condition_tier?}` | `IdentifyResponse` |
| POST | `/api/v1/market-scan` | JSON `MarketScanRequest {item: ItemSpec, platforms?, allow_live?}` | `MarketScanResponse` |

Flow: identify (photo or text) → user edits `item` in the form → send the edited `item` to
market-scan → the results page draws everything from that one payload.

## Price words (use these exactly)

* **list price** (`price_gbp` on the grid, `recommended_list_gbp`, `list_price_gbp`) — the
  price shown on a listing. Every chart's x-axis.
* **expected sale price** (`expected_sale_price_gbp`) — what it likely sells for after offers:
  list × (1 − that platform's offer buffer).
* **net payout** (`net_payout_gbp`) — cash the seller keeps after seller fees.
* **total buyer price** (`ListingRow.total_buyer_price_gbp`) — item + postage + buyer fees;
  engine-only.

## What each chart reads

`tier` = the condition currently selected (starts at `item_metadata.condition_tier`).
Switching condition swaps which tier's arrays are drawn; there is no refetch.

| UI element | Fields |
|---|---|
| **Sale-probability curves** (hero) | `price_curve_grid[tier][i]`: x = `price_gbp`, lines = `prob_sell_24h`, `prob_sell_7d`, `prob_sell_30d`. Mark `price_markers[tier].*` and `market_summary.sold_median_gbp` |
| **Price × time heatmap** | `sale_surface[tier]`: x = the shared price axis (`price_curve_grid[tier][i].price_gbp`), y = `days`, colour = `prob[i][j]`. Contours at 0.5 / 0.8 / 0.95 |
| **Evidence scatter** | `comparable_listings`: x = `price_gbp`, y = `t_days` (for `sold_interval` draw a bar from `t_days` to `t_upper_days`), filled = sold, hollow with a right arrow = `active` (unsold, "at least this long"). Colour by `platform` |
| **Cash vs list price, per platform** | `price_curve_grid[tier][i].platforms[platform].net_payout_gbp` and `expected_days` (with `expected_days_low`–`expected_days_high` as a band); peak = `platform_recommendations[platform].recommended_list_gbp` |
| **Platform cards** | `platform_recommendations[platform]`: list price, expected sale price, net payout, expected days with range, P(7 days), `confidence` badge (`high` / `medium` / `low` / `estimated`), `n_sales`, `notes`. Highlight `best_platform` |
| **Survivorship-bias banner** | `market_summary.active_median_gbp`, `sold_median_gbp`, `survivorship_bias_pct` |
| **Condition strip** | `market_summary.condition_buckets[tier].p25 / median / p75` |
| **Markdown timeline** | `markdown_schedule` |
| **"How it works" drawer** | `model_fit` |
| **Data-source badge** | `market_summary.data_source`: `live` / `db_cache` / `seed` / `mock` |

Durations are clipped at **365** days. Show 365 as "1 year+".

`price_curve_grid[tier][i]` top-level probabilities are pooled across platforms; the
per-platform numbers are in its `platforms` field. Hover read-outs can interpolate between
grid points; `best_platform` on each point says which platform wins at that list price.

## Regenerating the TS types

```bash
python scripts/make_mock.py   # rewrites mocks + contract/schema.json
npx json-schema-to-typescript@15 -i contract/schema.json -o frontend/src/types/api.ts --unreachableDefinitions
```
