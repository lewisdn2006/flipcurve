// Responses shaped the way FastAPI's response_model writes them (backend/models/schemas.py): every
// field present, in schema order, defaults filled in, and pydantic's dates (UTC as "Z").

export const SCHEMA_VERSION = '1.1';

/** pydantic's datetime text: the ISO form with a UTC offset written as "Z". */
const pydanticTime = (v) => (typeof v === 'string' ? v.replace(/\+00:00$/, 'Z') : v ?? null);

const LISTING_DEFAULTS = {
  listing_id: null, platform: null, title: null, url: null, price_gbp: null, shipping_gbp: 0.0, buyer_fee_gbp: 0.0,
  total_buyer_price_gbp: null, condition_tier: null, condition_raw: null, obs_type: null, t_days: null, t_upper_days: null,
  t_entry_days: null, stock_window_days: null, related: null, start_source: 'unknown', listed_at: null, sold_at: null,
  observed_at: null, favourite_count: null, best_offer_accepted: null, is_outlier: false, outlier_reason: null,
};
const TIMES = ['listed_at', 'sold_at', 'observed_at'];

export function listingRow(r) {
  const out = {};
  for (const [k, d] of Object.entries(LISTING_DEFAULTS)) out[k] = r[k] === undefined ? d : r[k];
  for (const k of TIMES) out[k] = pydanticTime(out[k]);
  return out;
}

/** A MarketScanResponse in the order FastAPI writes it. */
export function marketScanResponse(out) {
  return {
    schema_version: SCHEMA_VERSION,
    item_metadata: out.item_metadata,
    cleaned_image_url: out.cleaned_image_url ?? null,
    market_summary: { ...out.market_summary, fetched_at: pydanticTime(out.market_summary.fetched_at),
      condition_buckets: Object.fromEntries(Object.entries(out.market_summary.condition_buckets || {})
        .map(([t, b]) => [t, { p25: b.p25, median: b.median, p75: b.p75, avg_days: b.avg_days ?? null, n: b.n }])) },
    model_fit: out.model_fit,
    price_range: out.price_range,
    price_curve_grid: out.price_curve_grid,
    sale_surface: out.sale_surface,
    price_markers: out.price_markers,
    best_platform: out.best_platform,
    platform_recommendations: out.platform_recommendations,
    markdown_schedule: out.markdown_schedule,
    comparable_listings: (out.comparable_listings || []).map(listingRow),
    warnings: out.warnings || [],
  };
}
