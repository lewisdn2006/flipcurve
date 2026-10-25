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

Markers on the charts (`price_markers`), always quick ≤ best ≤ patient (revised 25 Oct):
* quick sale: highest price with P(sold within 7 days) ≥ 50%;
* best value: maximises net × E[e^(−γT)] with γ = 0.02/day (waiting a month costs ~45%), but
  never above 1.35× the median price exact matches sold for (or their 90th percentile);
* patient: the same with γ = 0.005/day, never above 1.6× that median (or 1.15× the 90th pct).
The ceilings use same-condition sales when there are 3+. Without them, a fit where price looked
barely linked to speed sent every "best" price to the top of the chart (£121, 25 Oct).
If no price reaches the quick-sale target (a slow market), quick sale = 85% of the typical sold
price for the condition, with a `note`, and the markdown floor uses it too. Before this, a slow
market made every marker fall to the bottom of the chart: a user's £30 Jordans showed £10.
(γ was 0.05/day, which made any slow market recommend the cheapest price.)

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

## D11. Architecture: website + helper extension + laptop backend (25 Oct)

* **Website** (`web/flipcurve/index.html`, one static page): text or photo in → confirm the
  item → Scan → results. Live at `lewisdn.com/flipcurve/` via the "flipcurve-page" Worker
  (route `lewisdn.com/flipcurve*`, runs before the site's own Worker), which serves the file from
  GitHub main; it talks to `api.lewisdn.com`.
* **Helper extension** (`extension/`, ID `fdibajhmjjbjcdfafdofkoedbkhbkfcb`): no screens. When
  the page asks, it loads ~2 eBay newest + ~2 eBay sold + 1 Vinted catalogue + a few Vinted item
  pages **in the user's own Chrome, signed in as them**, 1.5 s apart, and hands the HTML back.
  It stops on any sign-in or bot-check page (D10). Only lewisdn.com and localhost can call it.
* **Backend** on the laptop (FastAPI), exposed with a Cloudflare named tunnel
  (`scripts/tunnel.sh`). `POST /api/v1/scan-pages` turns the pages into rows, fits, and returns
  the usual MarketScanResponse; `GET /api/v1/recheck-ids` lists Vinted items due a re-check.
  Browser calls are only accepted from lewisdn.com, trycloudflare.com and localhost.
* Why: eBay's sold search now needs a signed-in person, and a signed-in person's own browser is
  the honest way to see it; the user never saves pages by hand.
* If the laptop is off, the site shows "can't reach the server"; "Use saved data" works whenever
  the server is up even without the helper.

## D12. More data per scan: similar versions, Vinted stock sampling, more pages (25 Oct)

First live scan (Jordan 1 Low Triple Black): eBay's search was ~75% Jordan 1 **Mid** and ~25%
kids' sizes, only 4 exact sales, and Vinted had no sales at all, so speed came almost entirely
from priors. Three changes:
* **Similar versions** (Mid when we searched Low, Pro/Max/Mini the search didn't name, kids'
  sizes) are tagged `related`. They join the speed fit with prices rescaled to this item's level
  (ratio of the two "Good" anchors), so "a Mid at a Mid's usual price sells about as fast as a
  Low at a Low's usual price". They never set the price level; anchors come from exact matches
  (≥ 3 sales pooled over tiers). Groups under 5 listings are dropped. Shown greyed as "similar".
* **Vinted stock sampling.** A newest-first catalogue read back to age A, with listings arriving
  steadily, has visible ages distributed as S(t) / ∫₀ᴬ S(u) du: young-heavy = fast, even spread =
  slow. Each catalogue row now carries `stock_window_days` (A = 90th percentile of ages, so bumped
  old listings don't stretch it). Vinted ages use an ID-interpolated point estimate instead of the
  ~36 h bounds. Caveat: deleted listings also vanish, so this leans fast; re-checks still add
  real sales over time. Vinted's badge is "low" (not "estimated") when this is all it has.
* **More pages:** up to 4 eBay new + 4 eBay sold (240 each, stops at the last page), 4 Vinted
  catalogue pages, 8 Vinted item pages. Pages are stripped of scripts/styles in the extension and
  gzipped by the site (~10× smaller) before upload.
* Relevance now needs 70% of the query's words (50% let "Air Force 1 Triple Black" through).
* Survivorship gap is measured within one platform (mixing Vinted asks with eBay sales made it
  negative).

## D13. Matching the exact item (25 Oct, second Jordan scan)

The eBay search "nike air jordan 1 low triple black" returned every Jordan 1 Low colourway and
collab (Panda, Game Royal, Travis Scott at £300-780) and an Air Force 1, all passing the 70%
word-overlap test, so prices came out ~4x too high. Now:
* The identifier (Gemini) also returns `must_have` (words every correct title contains, e.g.
  jordan, low) and `must_not_have` (other colourways, collabs, replicas). Both are editable on
  the confirm step. Version words (mid, kids, pro...) in must_not_have are left to D12.
* Without those, two generic checks still apply: a colour in the title the item doesn't have
  ("different colour: white"), and a standalone "x" ("Travis Scott x Nike": a collaboration).
  OG / SP count as versions.
* Price fences are robust (median / MAD of condition-adjusted log prices); anchors pool all
  sales and keep each condition's own median only within 0.7-1.5x of the pooled level; the
  price spread σ_p uses MAD too; β1 is bounded ≥ 0.75 (price must matter somewhat).
* Up to 4 photos go to the identifier; photos can be removed one by one; "Start again" resets.
* Third Jordan scan (4 pages): exact matches priced over 2.5× (or under 0.3×) the median
  condition-adjusted sold price are dropped (a £228 "Black Lace-Up", a Cactus Jack pair); for
  footwear, UK 6-and-under listings count as kids' sizing for an adult pair (related, D12).
* Vinted's stock window A is now the typical age of the last 5% of cards in catalogue order: the
  4 pages for a rarely listed item reached back ~300 days and the 90th-percentile rule cut it at 191.
* Platform cards show "half sell within" (median); the average is pulled up by the long tail.
  Parameters on a bound (β1 at its floor) are held fixed for the ranges, which were 0-365 days.
* The survivorship gap needs 5+ sold and 5+ unsold exact matches on one platform and compares
  condition-adjusted prices (brand-new asks next to used sales showed +154%).

## D14. Vinted sales without waiting for the next scan (25 Oct)

Checked first: Vinted search has no "sold" filter, and sellers' public profiles don't show sold
items (a profile with 2 reviews listed only its 3 items for sale). A sold listing's own page
stays up and says "Sold", so following listings we've already seen is the only source.
* **Background re-checks** (the one exception to "only when you press Scan"; user-approved,
  with an off switch in the extension popup): every 2 hours while Chrome is open the helper
  takes up to 20 due listings from `GET /api/v1/recheck-due`, opens them 1.5 s apart, and posts
  them to `POST /api/v1/recheck-results`. Due = matched the item in its scan, first seen 3 h+ ago,
  not checked in the last 6 h, followed for 30 days. A refusal pauses it for 12 hours.
* The history lives in each query's Vinted cache under `seen`; every scan (and "Use saved data")
  turns it into rows: a sale between the last time it was still for sale and the check that found
  it sold, or survival to the last check. Ages use the ID-interpolated start.
* Vinted catalogue depth: up to 8 pages per scan (stops at the last page) for the stock estimate.

## D15. Scans that stalled, and price sensitivity (25 Oct, T-shirt test)

* Two scans with 8 Vinted pages both died on the 15th Vinted request in about a minute: Vinted
  slows down when asked too often, and Chrome stops an extension worker whose request stalls.
  Now: Vinted requests are 4.5 s apart (background re-checks 6 s), eBay 1.5 s, and the two sites
  run side by side; any request is abandoned after 20 s ("slow": that site's part ends with the
  pages it got, and the result says so); 4 Vinted listing pages per scan instead of 8 (the ID
  calibration builds up across scans); the worker is kept awake during long runs; the last scan's
  progress is logged and shown in the popup and in the site's warning if it ends early. Pages are
  sent to the site one at a time, and a partial scan is kept rather than thrown away.
* Price sensitivity floor is now in elasticity terms: a 10% dearer listing waits at least ~10%
  longer (β1 ≥ σ_p / anchor, and ≥ 0.3). The fixed 0.75 was far stronger for tightly priced items
  (σ_p £1.60 on a £7 T-shirt) and made the tail heavy (k 0.40 → 0.68 after the change).
* MKI tee test: the query "mki miyuki zoku studio t-shirt white" threw out 71 of 74 timed eBay
  sales (titles lack "studio" or say "tee"). Titles and queries now share synonyms (tee / t-shirt /
  t shirt -> tshirt; sneakers -> trainers; hoodie, jumper), and with must_have words present a
  title only needs half the query's words. Vinted's stock window is capped at 120 days: listings
  still up after a year (seen to 680 days) are mostly forgotten, not slow.
* Quick sale is never below the 25th percentile of same-condition sales (it went to £4 for a
  T-shirt that sells around £7). "XL" no longer marks a different model (it's a clothing size).

## D16. eBay Browse API alongside the helper (25 Oct, keys approved)

The API adds to the helper; it doesn't replace it. The Browse API only has **live** eBay
listings: no sold listings (those need a signed-in browser) and nothing from Vinted, so the
helper still does eBay sold pages and all of Vinted, and keeps loading eBay's newest pages too.
* With `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` in .env, every scan also pulls up to 1,000 newest
  UK listings (`itemLocationCountry:GB`, 5 calls) on the server, with exact listing dates
  (`itemOriginDate`, kept across relists). They're merged with the helper's pages (API dates win).
* Each API listing's (item number, `itemCreationDate`) is a calibration point, which makes the
  dating of sold listings (from their item numbers) more precise.
* If the API fails, the scan carries on with the helper's pages and says so.
  `python scripts/check_keys.py` tests the keys; the page shows "eBay API on".

## TEAM — still to decide

- [ ] Demo items: fill in `backend/scrapers/cache_seed/demo_items.csv` with the things on the table.
- [ ] Frontend framework (Person 3's call; the types are in `frontend/src/types/api.ts`).
- [ ] Demo network: laptop on venue Wi-Fi vs a phone hotspot (the tunnel needs internet either way).
- [ ] Who owns the pitch deck and backup video.
