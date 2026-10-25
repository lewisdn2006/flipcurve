"""FlipCurve API contract, schema_version 1.1.

Every backend module produces/consumes these types and the frontend reads the JSON they
serialise to. Change nothing here without telling the whole team; if you must add a field,
make it Optional with a default so old mocks still validate, and bump SCHEMA_VERSION minor.

v1.1 changes: eBay + Vinted only (FB Marketplace deferred); no price slider, the frontend
draws charts instead; x-axis is the LIST price; per-platform confidence; sale-probability
surface for the price x time heatmap; up to 500 evidence listings for the scatter.

Price conventions (read this once, it avoids most integration bugs):
  * `price_gbp` on the curve grid = the LIST price: what is shown on the listing. The model
    is fitted on listed prices, so this is the natural x-axis.
  * `expected_sale_price_gbp` = what the item is likely to actually sell for after offers,
    list * (1 - offer_buffer) for that platform.
  * `net_payout_gbp` = cash the seller keeps after seller fees.
  * `total_buyer_price_gbp` on a ListingRow = item price + buyer fees + postage; engine-only.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.1"
GRID_POINTS = 50
MAX_DAYS = 365.0  # durations are clipped here; the UI shows 365 as "1 year+"
MAX_EVIDENCE_ROWS = 500


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


def _v(k):
    """Dict keys may arrive as an enum member or its string value."""
    return getattr(k, "value", k)


# ---------------------------------------------------------------- enums


class ConditionTier(str, Enum):
    fair = "fair"
    good = "good"
    very_good = "very_good"
    mint = "mint"

    @property
    def index(self) -> int:
        """1..4, the c used in the survival model."""
        return list(ConditionTier).index(self) + 1


CONDITION_LABELS = {"fair": "Fair", "good": "Good", "very_good": "Very Good", "mint": "Mint/New"}


class Platform(str, Enum):
    ebay = "ebay"
    vinted = "vinted"
    # fb_marketplace: deferred, see DECISIONS.md D9


class ObsType(str, Enum):
    """How a listing enters the survival likelihood (maths doc, section 5).

    sold_exact        sold, time-to-sale known                  -> ln f(t)
    active            still unsold after t days                 -> ln S(t)
    sold_interval     sold somewhere in (t, t_upper] days       -> ln[S(t) - S(t_upper)]
                      e.g. eBay sold item whose start date came from item-number
                      calibration (sold date is day-only), or a Vinted item seen sold
                      with t = 0 and t_upper = its age when we checked
    sold_unknown_time sold, no duration at all                  -> price anchors M(c) only
    """
    sold_exact = "sold_exact"
    active = "active"
    sold_interval = "sold_interval"
    sold_unknown_time = "sold_unknown_time"


StartSource = Literal["exact", "id_calibration", "upload_text", "unknown"]
Confidence = Literal["high", "medium", "low", "estimated"]
DataSource = Literal["live", "db_cache", "seed", "mock"]


# ---------------------------------------------------------------- identify


class SearchQueries(_Model):
    ebay: str
    vinted: str


class ItemSpec(_Model):
    """Output of /identify and input to /market-scan. Every field is user-editable in the UI."""
    title: str = Field(..., description="Human display title, e.g. \"Nike Air Force 1 '07 Low White\"")
    brand: Optional[str] = None
    model_name: Optional[str] = None
    category: Optional[str] = None
    size_or_storage: Optional[str] = None
    era_or_year: Optional[str] = None
    condition_tier: ConditionTier
    condition_score: float = Field(..., ge=0, le=10, description="Vision score 0-10")
    detected_flaws: list[str] = Field(default_factory=list)
    search_queries: SearchQueries
    is_broken: bool = Field(False, description="If true, the outlier filter keeps 'spares/repairs' listings")


class IdentifyResponse(_Model):
    schema_version: str = SCHEMA_VERSION
    item: ItemSpec
    source: Literal["vision", "text"]
    cleaned_image_url: Optional[str] = None
    confidence: Optional[float] = Field(None, ge=0, le=1)
    warnings: list[str] = Field(default_factory=list)


class IdentifyTextRequest(_Model):
    """JSON body for the text-search fallback (photos go as multipart/form-data instead)."""
    query: str = Field(..., min_length=2)
    condition_tier: Optional[ConditionTier] = None


# ---------------------------------------------------------------- market data


class ListingRow(_Model):
    """One normalised marketplace listing. Scrapers emit these; the engine consumes them;
    the evidence scatter draws them."""
    listing_id: str
    platform: Platform
    title: str
    url: Optional[str] = None
    price_gbp: float = Field(..., gt=0, description="Price shown on the listing (sold price for sold rows)")
    shipping_gbp: float = Field(0.0, ge=0)
    buyer_fee_gbp: float = Field(0.0, ge=0)
    total_buyer_price_gbp: float = Field(..., gt=0)
    condition_tier: Optional[ConditionTier] = None
    condition_raw: Optional[str] = Field(None, description="Platform's own condition text")
    obs_type: ObsType
    t_days: Optional[float] = Field(None, ge=0, description="Duration (sold_exact), age (active) or lower bound (sold_interval)")
    t_upper_days: Optional[float] = Field(None, ge=0, description="Upper bound, sold_interval only")
    t_entry_days: Optional[float] = Field(
        None, ge=0, description="Age when we first saw it (left truncation); None/0 = seen from the start. "
                                "The likelihood conditions on surviving to this age")
    stock_window_days: Optional[float] = Field(
        None, gt=0, description="Active rows from a newest-first catalogue read back to this age (days): "
                                "the model uses the age spread of what's still listed (stock sampling)")
    related: Optional[str] = Field(None, description="Set when this is a similar but different item "
                                   "(e.g. 'mid' for a Jordan 1 Mid when we searched Low): used for selling "
                                   "speed with its price rescaled, never for the price level")
    start_source: StartSource = Field("unknown", description="How we know when it was listed")
    listed_at: Optional[datetime] = None
    sold_at: Optional[datetime] = None
    observed_at: datetime
    favourite_count: Optional[int] = Field(None, ge=0)
    best_offer_accepted: Optional[bool] = Field(None, description="eBay: true means the real sale price was below price_gbp")
    is_outlier: bool = False
    outlier_reason: Optional[str] = None

    @model_validator(mode="after")
    def _check_durations(self):
        if self.obs_type in ("sold_exact", "active") and self.t_days is None:
            raise ValueError(f"{self.obs_type} needs t_days")
        if self.obs_type == "sold_interval":
            if self.t_days is None or self.t_upper_days is None or self.t_upper_days < self.t_days:
                raise ValueError("sold_interval needs t_days <= t_upper_days")
        if self.t_entry_days and self.t_days is not None and self.t_entry_days > self.t_days + 1e-9:
            raise ValueError("t_entry_days cannot be after t_days")
        return self


class CollectedPage(_Model):
    """One page the helper extension loaded in the user's browser (extension/README.md)."""
    platform: Platform
    kind: Literal["active", "sold", "catalog", "item", "recheck"]
    page: Optional[int] = None
    url: str
    final_url: Optional[str] = None
    status: int = 200
    html: str
    fetched_at: datetime


class ScanPagesRequest(_Model):
    item: "ItemSpec"
    pages: list[CollectedPage] = Field(default_factory=list, max_length=40)
    stopped: dict = Field(default_factory=dict, description="Sites the extension stopped on, and why")


class MarketScanRequest(_Model):
    item: ItemSpec
    platforms: list[Platform] = Field(default_factory=lambda: [p for p in Platform])
    allow_live: bool = Field(True, description="False = cache/seed only")


# ---------------------------------------------------------------- engine output


class ConditionBucket(_Model):
    p25: float
    median: float
    p75: float
    avg_days: Optional[float] = None
    n: int = 0


class MarketSummary(_Model):
    sample_size_sold: int
    sample_size_active: int
    sample_size_excluded: int = 0
    active_median_gbp: Optional[float] = None
    sold_median_gbp: Optional[float] = None
    survivorship_bias_pct: Optional[float] = Field(None, description="(active_median - sold_median) / sold_median * 100")
    condition_buckets: dict[ConditionTier, ConditionBucket]
    data_source: DataSource
    platforms_ok: list[Platform]
    fetched_at: datetime


class ModelFit(_Model):
    """Fitted Weibull AFT parameters (maths doc, sections 4-6), for the 'how it works' drawer."""
    k: float = Field(..., gt=0)
    lambda0: float = Field(..., gt=0, description="Days, Good condition, typical price, average platform")
    beta1: float = Field(..., description="Price effect per spread of markup")
    beta2: float = Field(..., description="Condition effect")
    sigma_p: float = Field(..., gt=0)
    anchor_gbp: dict[ConditionTier, float] = Field(..., description="M(c), median sold price per condition")
    platform_theta: dict[Platform, float] = Field(..., description="Speed term; lambda multiplied by exp(theta)")
    platform_n_sales: dict[Platform, int] = Field(..., description="Observed sales (exact + interval) per platform")
    n_obs: int
    n_censored: int
    n_interval: int = 0
    used_priors: bool = Field(..., description="True when thin data left the fit close to the priors")
    log_likelihood: Optional[float] = None


class PlatformPoint(_Model):
    """One platform at one list price."""
    expected_sale_price_gbp: float
    net_payout_gbp: float
    prob_sell_7d: float = Field(..., ge=0, le=1)
    expected_days: float = Field(..., ge=0, le=MAX_DAYS)
    expected_days_low: float = Field(..., ge=0, le=MAX_DAYS, description="90% confidence range on the average wait")
    expected_days_high: float = Field(..., ge=0, le=MAX_DAYS)


class CurvePoint(_Model):
    """Pooled across platforms (theta = 0) unless read from `platforms`."""
    price_gbp: float = Field(..., description="List price")
    prob_sell_24h: float = Field(..., ge=0, le=1)
    prob_sell_7d: float = Field(..., ge=0, le=1)
    prob_sell_30d: float = Field(..., ge=0, le=1)
    expected_days_to_sell: float = Field(..., ge=0, le=MAX_DAYS)
    median_days_to_sell: float = Field(..., ge=0, le=MAX_DAYS)
    ci_days_low: float = Field(..., ge=0, le=MAX_DAYS, description="5th percentile of one item's time-to-sell")
    ci_days_high: float = Field(..., ge=0, le=MAX_DAYS, description="95th percentile of one item's time-to-sell")
    platforms: dict[Platform, PlatformPoint]
    best_platform: Platform = Field(..., description="Highest net payout x E[exp(-gamma T)]")


class SaleSurface(_Model):
    """P(sold within days[j] | price_axis[i]) for the price x time heatmap, pooled across platforms."""
    days: list[float] = Field(..., min_length=2)
    prob: list[list[float]] = Field(..., description="prob[i][j]: i over the shared price axis, j over days")


class PriceRange(_Model):
    min_gbp: float
    max_gbp: float
    step_gbp: float


class PriceMarkers(_Model):
    """List prices to mark on the charts (per condition tier)."""
    fast_sale_gbp: float = Field(..., description="Highest list price with P(sell <= 7d) >= 0.5")
    best_value_gbp: float = Field(..., description="Best list price on the best platform at default urgency")
    patient_gbp: float = Field(..., description="Highest list price with P(sell <= 30d) >= 0.5")
    gamma_per_day: float = Field(..., description="Urgency used for best_value")
    note: Optional[str] = Field(None, description="Set when a marker fell back to recent sold prices")


class PlatformRecommendation(_Model):
    recommended_list_gbp: float = Field(..., description="Best list price on this platform for the item's condition")
    expected_sale_price_gbp: float
    net_payout_gbp: float
    prob_sell_7d: float = Field(..., ge=0, le=1)
    expected_days: float = Field(..., ge=0, le=MAX_DAYS)
    expected_days_low: float = Field(..., ge=0, le=MAX_DAYS)
    expected_days_high: float = Field(..., ge=0, le=MAX_DAYS)
    offer_buffer_pct: float
    n_listings: int = Field(..., ge=0)
    n_sales: int = Field(..., ge=0, description="Observed sales behind this platform's speed estimate")
    confidence: Confidence
    notes: list[str] = Field(default_factory=list)


class MarkdownStep(_Model):
    day: int = Field(..., ge=0)
    list_price_gbp: float
    platform: Platform
    action: str


class MarketScanResponse(_Model):
    """Everything the frontend needs, in one payload; charts are drawn from it with no refetch."""
    schema_version: str = SCHEMA_VERSION
    item_metadata: ItemSpec
    cleaned_image_url: Optional[str] = None
    market_summary: MarketSummary
    model_fit: ModelFit
    price_range: PriceRange
    price_curve_grid: dict[ConditionTier, list[CurvePoint]] = Field(
        ..., description="GRID_POINTS points per tier, all tiers share the same price axis"
    )
    sale_surface: dict[ConditionTier, SaleSurface]
    price_markers: dict[ConditionTier, PriceMarkers]
    best_platform: Platform = Field(..., description="Platform recommended for the item's condition")
    platform_recommendations: dict[Platform, PlatformRecommendation] = Field(
        ..., description="For the item's condition tier"
    )
    markdown_schedule: list[MarkdownStep]
    comparable_listings: list[ListingRow] = Field(
        default_factory=list, max_length=MAX_EVIDENCE_ROWS, description="Cleaned listings for the evidence scatter"
    )
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_grid(self):
        tiers = {_v(t) for t in self.price_curve_grid}
        if tiers != {t.value for t in ConditionTier}:
            raise ValueError("price_curve_grid needs all four condition tiers")
        for name in ("sale_surface", "price_markers"):
            if {_v(t) for t in getattr(self, name)} != tiers:
                raise ValueError(f"{name} needs all four condition tiers")
        axes = []
        for tier, pts in self.price_curve_grid.items():
            if len(pts) != GRID_POINTS:
                raise ValueError(f"{tier}: expected {GRID_POINTS} grid points, got {len(pts)}")
            prices = [p.price_gbp for p in pts]
            if any(b <= a for a, b in zip(prices, prices[1:])):
                raise ValueError(f"{tier}: grid prices must be strictly increasing")
            axes.append(tuple(round(p, 2) for p in prices))
        if len(set(axes)) != 1:
            raise ValueError("all tiers must share the same price axis")
        for tier, s in self.sale_surface.items():
            if len(s.prob) != GRID_POINTS or any(len(row) != len(s.days) for row in s.prob):
                raise ValueError(f"{tier}: sale_surface.prob must be {GRID_POINTS} x len(days)")
        return self


class ErrorResponse(_Model):
    error: str
    detail: Optional[str] = None
    retryable: bool = False
