# Decisions

These are built into the contract (v1.1). Anything marked **TEAM** still needs an answer.
The maths behind them is in the doc "FlipCurve: The Maths Behind Time-to-Sell".

## D1. Where time-to-sale comes from — item-number calibration

**Problem.** The survival model needs listings whose time-to-sale is known, at least roughly.
No platform publishes "listed on X, sold on Y".

**What we tested (24 Oct, in Chrome):**
* **eBay:** sorted newest-first, search results show each listing's exact start time. Item
  numbers come in ~100 parallel blocks (first three digits). Within a block they rise with
  time: interpolating between neighbouring known listings gave a median error of ~4 minutes,
  88–96% within 6 hours. Sold search gives the item number and the sold date (day only).
  With 4,531 calibration points from 5 queries, 75% of sold Air Force 1 listings got a
  duration: median 15 days, 10th–90th percentile 1.8 to 57 days, none negative.
* **Vinted:** item IDs are one global counter (~0.9 million an hour), perfectly ordered
  against "Uploaded … ago". Sold items stay viewable with a "Sold" badge; deleted ones 404.
  There is no sale date anywhere, and search only returns active items.

**Decision.** Observation types (`ListingRow.obs_type`) and where each comes from:

| obs_type | Source | Likelihood term |
|---|---|---|
| `active` | eBay: exact start from search. Vinted: age from ID calibration | ln S(t) |
| `sold_interval` | eBay sold: start from item-number calibration, sold date ± ½ day | ln[S(a) − S(b)] |
| `sold_interval` | Vinted: listed and active at a first scrape, sold by a later re-check | ln[S(a) − S(b)] |
| `sold_exact` | rare; both dates known | ln f(t) |
| `sold_unknown_time` | sold rows we can't date (outside the calibration) | price anchors only |

`ListingRow.start_source` records how the start date was found: `exact`,
`id_calibration`, `upload_text` or `unknown`.

**Vinted re-check (cheap, and the only source of Vinted speed data):** scrape each demo
item's Vinted results once (`scripts/vinted_smoke.py`), then later run it again with
`--recheck`. Any that now show "Sold" become interval sales. A single scrape only shows
listings that survived long enough to be seen (left truncation), so on its own it carries no
speed information: without a re-check, Vinted's badge reads "estimated" or "low".

**Sampling rules the model relies on (maths doc, section 5):** eBay rows are a cohort, meaning
only listings that started inside the window both searches fully cover, with entry age 0.
Vinted rows carry `t_entry_days` = their age when first seen, and the likelihood conditions on it.

Caveats: the sold date is day-only; relisted items may carry a misleading start date;
"Best Offer accepted" (`best_offer_accepted`) means the real sale price was below the shown one.

## D2. Condition toggle — precomputed grids per tier

`price_curve_grid`, `sale_surface` and `price_markers` hold all four tiers on one shared
price axis. Switching condition swaps arrays, with no refetch.

## D3. Per-platform speed — partial pooling with confidence

Each platform gets a speed term θ_j in the one Weibull fit, with a normal prior N(μ_j, τ²),
τ = 0.5 (a penalised / MAP fit). A platform with few observed sales is pulled toward the
shared average. Confidence comes from the Hessian (delta method on ln E[T]) and is shown as
a range on expected days plus a badge:

| Badge | Rule |
|---|---|
| high | 30+ observed sales, and the range's high end under 2× its low end |
| medium | 10–29 observed sales, or 30+ with a wide range |
| low | fewer than 10 observed sales |
| estimated | no data on that platform; prior only |

The badge depends on observed **sales**, not listings.

## D4. No price slider — charts instead

The app takes the photo, runs the search, and shows charts. The x-axis is the list price.
1. Sale-probability curves: P(sold within 1 / 7 / 30 days) against list price (the hero).
2. Price × time heatmap with 50 / 80 / 95% contours (the "3D" view; a rotating 3D surface is a stretch goal).
3. Evidence scatter: every listing, price against days on market, sold vs unsold.
4. Net cash against list price per platform, with confidence bands; peaks = recommended prices.

Markers on the charts (`price_markers`): fast sale (P(24h) ≥ 50%), best value (maximises
net × E[e^(−γT)], γ = 0.05/day), patient (P(30d) ≥ 80%).

## D5. "90% range" = 5th–95th percentile of time-to-sell

Closed-form Weibull quantiles: a predictive range for one item (`ci_days_low/high`). This is
different from the confidence range on a platform's average wait (`expected_days_low/high`).

## D6. Durations capped at 365 days

Shown as "1 year+".

## D7. Fees are placeholders until verified

`engine/platform_config.py`: every number marked `VERIFY` gets checked against the current UK
fee pages (step 11). If seller fees really are ~0 on both, platform choice comes down to
speed and haggling. Don't invent fee differences.

## D8. Text search route

`/api/v1/identify-text` (JSON) sits alongside `/api/v1/identify` (photos). Both return
`IdentifyResponse`.

## D9. FB Marketplace deferred

eBay and Vinted give real data; FB doesn't. FB's placeholder numbers are kept in
`DEFERRED_PLATFORMS` in `engine/platform_config.py` for adding back later.

## D10. How we collect listing data

FlipCurve uses public listings as price guidance for someone selling their own item.
We collect them politely, and we never try to get around a refusal.

* **eBay active listings:** the official Browse API when `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET`
  are set (it returns `itemOriginDate`, which survives relists). Otherwise the newest-first
  search page.
* **eBay sold listings and Vinted:** search pages loaded by a real browser
  (`backend/scrapers/fetcher.py`, Playwright), one page at a time with pauses.
* **Low volume:** a few pages per demo item, cached in `cache_seed/` and reused.
* **A refusal means stop.** On a 403, a bot check or a CAPTCHA we raise `Blocked` and use
  cached data. No switching disguises, no retry loops, no CAPTCHA solving.
* For the demo, build the cache ahead of time with `scripts/ebay_smoke.py` and
  `scripts/vinted_smoke.py` (then `--recheck` for Vinted later).
* **eBay, when automated loads are refused (24 Oct):** eBay refused the hidden browser's
  sold-search page but served the same page to a person browsing normally in Chrome. So eBay
  data for the demo comes from pages a team member opens and saves (Cmd+S) while browsing:
  `scripts/import_ebay_pages.py urls <query>` lists the pages, `import` turns them into the cache.
  Terapeak (Seller Hub > Research) is eBay's own sold-data tool; it was showing "unavailable"
  on a freshly enabled account, so worth retrying.

## TEAM — still to decide

- [ ] Demo items: fill in `backend/scrapers/cache_seed/demo_items.csv` with the things on the table.
- [ ] Frontend framework (Person 3's call; the types are in `frontend/src/types/api.ts`).
- [ ] Hosting for the demo: laptop on venue Wi-Fi vs a phone hotspot (recommended: hotspot + `FLIPCURVE_OFFLINE=1` as a fallback).
- [ ] Who owns the pitch deck and backup video.
