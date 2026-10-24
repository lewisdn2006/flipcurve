# Phase 0 decisions

These are recommended defaults, already built into the frozen contract (v1.0). Confirm or
change them as a team in the first half hour. Anything marked **TEAM** still needs an answer.

## D1. Where time-to-sale comes from — the snapshot trick

**Problem.** The survival model needs sold listings with a known duration (δ = 1). eBay sold
results show the sold date and price but usually not when the item was listed, and sold Vinted
items vanish. With only censored data, the hazard can't be estimated properly.

**Decision.** Use four observation types (`ListingRow.obs_type`):

| obs_type | Where it comes from | Likelihood term |
|---|---|---|
| `active` | any live listing; age = now − created_at | ln S(t) |
| `sold_interval` | a listing seen in snapshot A that has gone by snapshot B; it sold in (age at A, age at B] | ln[S(t) − S(t_upper)] |
| `sold_exact` | the rare case where we know both dates (e.g. an eBay item page that shows the start date) | ln f(t) |
| `sold_unknown_time` | eBay sold results | sets the price anchors M(c) and σ_p only, kept out of the hazard |

**Action tonight (Person 2, as soon as the Vinted client works):** start
`scrapers/snapshot_job.py` for every demo item, re-scraping every 2–3 hours. By Sunday
afternoon each item has 6–8 snapshots, and the listings that disappeared become interval-censored
sales. This is real time-to-sale data no other team will have, and it's a strong pitch point.

Caveat for the pitch: a vanished listing might have been deleted rather than sold. Be upfront
about it, and treat high-favourite vanished items as more likely to be sales.

## D2. Condition toggle — one grid per tier

`price_curve_grid` holds all four tiers (4 × 50 points), and **all tiers share one price axis**
(`price_range`). Switching tier swaps which array the frontend reads, with no refetch and no
jump in the slider. The payload is ~185 KB of JSON, which gzips to a fraction of that.

## D3. Per-platform expected days — multiplier on λ

λ_platform = λ · m_platform. Defaults in `engine/platform_config.py` (Vinted 0.85, eBay 1.0,
FB 0.70). When both Vinted and eBay have ≥ 12 usable rows, fit m as a platform term.
FB/Gumtree extras:
* a local strike p is evaluated as p / (1 − 0.12), because local buyers compare against shipped prices
* expected days are divided by (1 − flake_prob), since a flake restarts the clock

Each grid point carries `platforms{list, net, days, P7d}` and `best_platform`, so the platform
cards update as the slider moves.

## D4. One slider, three presets

A single strike-price slider. The backend computes three preset prices per tier:
* **Sell in 24h** — the highest price with P(24h) ≥ 50%
* **Best EV** — p*(γ) with γ = 0.05/day, meaning a sale in 2 weeks is worth half a sale today
* **Patient** — the highest price with P(30d) ≥ 80%

No separate γ control for the MVP. A γ slider is a stretch goal, and the maths for it already
exists (`survival_model.discounted_sale_value`).

## D5. "90% range" = 5th–95th percentile of time-to-sell

Use the closed-form Weibull quantiles T_q = λ(−ln(1−q))^(1/k). This is a predictive range for one
item, which is what the UI sentence means. It is not a confidence interval on the parameters,
and there is no bootstrap: that is slow and answers a different question.
`ci_days_low/high` keep the plan's names, but this is what they hold.

## D6. Durations capped at 365 days

On a shared price axis, the far end of a cheap tier gives absurd waits (hundreds of thousands of
days). Every duration is clipped to 365, and the UI shows it as "1 year+".

## D7. Fees are placeholders until verified

`engine/platform_config.py` currently has zero seller fees for Vinted, eBay private sellers
and FB local. Every number marked `VERIFY` gets checked against the platform's current UK fee
page in Phase 3. If seller fees really are ~0 everywhere, platform choice comes down to
**speed and buyer-side fees**. That's a fine story, so don't invent fee differences.

## D8. Text search route

`/api/v1/identify-text` (JSON) sits alongside `/api/v1/identify` (photos). Both return
`IdentifyResponse`, so the UI treats them the same.

## TEAM — still to decide

- [ ] Demo items: fill in `backend/scrapers/cache_seed/demo_items.csv` with the 8–10 things on the table.
- [ ] Frontend framework (Person 3's call; the types are in `frontend/src/types/api.ts`).
- [ ] Hosting for the demo: laptop on venue Wi-Fi vs a phone hotspot (recommended: hotspot + `FLIPCURVE_OFFLINE=1` as a fallback).
- [ ] Who owns the pitch deck and backup video (Phase 5).
