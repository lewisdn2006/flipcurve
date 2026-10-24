"""FlipCurve API contract (FROZEN at schema_version 1.0).

Every backend module produces/consumes these types and the frontend reads the JSON they
serialise to. Change nothing here without telling the whole team; if you must add a field,
make it Optional with a default so old mocks still validate, and bump SCHEMA_VERSION minor.

Price conventions (read this once, it avoids most integration bugs):
  * `price_gbp` on the curve grid / slider = the STRIKE price: what seller and buyer agree
    for the item, excluding postage and buyer-protection fees.
  * `list_price_gbp` = what to type into the platform, i.e. strike / (1 - offer_buffer).
  * `total_buyer_price_gbp` on a ListingRow = item price + buyer fees + postage, used only
    inside the model to compare listings across platforms fairly.
  * `net_payout_gbp` = cash the seller actually keeps after seller fees and their own costs.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"
GRID_POINTS = 50
MAX_DAYS = 365.0  # durations are clipped here; the UI shows 365 as "1 year+"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


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
    fb_marketplace = "fb_marketplace"


class ObsType(str, Enum):
    """How a listing enters the survival likelihood.

    sold_exact        sold, time-to-sale known            -> ln f(t)
    active            still unsold after t days           -> ln S(t)
    sold_interval     vanished between two snapshots, so  -> ln[S(t) - S(t_upper)]
                      sold somewhere in (t, t_upper]
    sold_unknown_time sold, duration unknown (typical      -> price/condition anchors only,
                      eBay sold result)                       NOT in the hazard likelihood
    """
    sold_exact = "sold_exact"
    active = "active"
    sold_interval = "sold_interval"
    sold_unknown_time = "sold_unknown_time"


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
    condition_score: float = Field(..., ge=0, le=10, description="Vision score 0-10; c in [0.1,1] = score/10")
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
    """One normalised marketplace listing. Scrapers emit these; the engine consumes them."""
    listing_id: str
    platform: Platform
    title: str
    url: Optional[str] = None
    price_gbp: float = Field(..., gt=0, description="Item price as listed / sold")
    shipping_gbp: float = Field(0.0, ge=0)
    buyer_fee_gbp: float = Field(0.0, ge=0)
    total_buyer_price_gbp: float = Field(..., gt=0)
    condition_tier: Optional[ConditionTier] = None
    condition_raw: Optional[str] = Field(None, description="Platform's own condition text")
    obs_type: ObsType
    t_days: Optional[float] = Field(None, ge=0, description="Duration (sold_exact), age (active) or lower bound (sold_interval)")
    t_upper_days: Optional[float] = Field(None, ge=0, description="Upper bound, sold_interval only")
    listed_at: Optional[datetime] = None
    sold_at: Optional[datetime] = None
    observed_at: datetime
    favourite_count: Optional[int] = Field(None, ge=0)
    is_outlier: bool = False
    outlier_reason: Optional[str] = None

    @model_validator(mode="after")
    def _check_durations(self):
        if self.obs_type in ("sold_exact", "active") and self.t_days is None:
            raise ValueError(f"{self.obs_type} needs t_days")
        if self.obs_type == "sold_interval":
            if self.t_days is None or self.t_upper_days is None or self.t_upper_days < self.t_days:
                raise ValueError("sold_interval needs t_days <= t_upper_days")
        return self


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
    """Fitted Weibull AFT parameters, shown in a 'how this works' drawer for judges."""
    k: float = Field(..., gt=0)
    lambda0: float = Field(..., gt=0)
    beta1: float
    beta2: float
    sigma_p: float = Field(..., gt=0)
    anchor_gbp: dict[ConditionTier, float] = Field(..., description="M(c)")
    platform_multiplier: dict[Platform, float] = Field(..., description="lambda_platform = lambda * m")
    n_obs: int
    n_censored: int
    used_priors: bool
    log_likelihood: Optional[float] = None


class PlatformPoint(_Model):
    list_price_gbp: float
    net_payout_gbp: float
    expected_days: float = Field(..., ge=0, le=MAX_DAYS)
    prob_sell_7d: float = Field(..., ge=0, le=1)


class CurvePoint(_Model):
    price_gbp: float = Field(..., description="Strike price")
    prob_sell_24h: float = Field(..., ge=0, le=1)
    prob_sell_7d: float = Field(..., ge=0, le=1)
    prob_sell_30d: float = Field(..., ge=0, le=1)
    expected_days_to_sell: float = Field(..., ge=0, le=MAX_DAYS)
    median_days_to_sell: float = Field(..., ge=0, le=MAX_DAYS)
    ci_days_low: float = Field(..., ge=0, le=MAX_DAYS, description="5th percentile of time-to-sell (90% range)")
    ci_days_high: float = Field(..., ge=0, le=MAX_DAYS, description="95th percentile of time-to-sell")
    platforms: dict[Platform, PlatformPoint]
    best_platform: Platform = Field(..., description="Highest net payout, ties -> fewest expected days")


class PriceRange(_Model):
    min_gbp: float
    max_gbp: float
    step_gbp: float


class Presets(_Model):
    """Strike prices the quick-preset buttons snap the slider to (per condition tier)."""
    sell_24h_gbp: float = Field(..., description="Highest price with P(sell <= 24h) >= 0.5")
    best_ev_gbp: float = Field(..., description="p*(gamma) at default urgency")
    patient_gbp: float = Field(..., description="Highest price with P(sell <= 30d) >= 0.8")
    gamma_per_day: float = Field(..., description="Discount rate used for best_ev")


class PlatformRecommendation(_Model):
    recommended_list_gbp: float
    expected_strike_gbp: float
    net_payout_gbp: float
    expected_days: float
    offer_buffer_pct: float
    fit_score: int = Field(..., ge=0, le=100)
    flake_risk_pct: Optional[float] = Field(None, ge=0, le=100)
    notes: list[str] = Field(default_factory=list)


class MarkdownStep(_Model):
    day: int = Field(..., ge=0)
    list_price_gbp: float
    platform: Platform
    action: str


class MarketScanResponse(_Model):
    """Everything the frontend needs, in one payload. The slider never calls the backend."""
    schema_version: str = SCHEMA_VERSION
    item_metadata: ItemSpec
    cleaned_image_url: Optional[str] = None
    market_summary: MarketSummary
    model_fit: ModelFit
    price_range: PriceRange
    price_curve_grid: dict[ConditionTier, list[CurvePoint]] = Field(
        ..., description="GRID_POINTS points per tier, all tiers share the same price axis"
    )
    presets: dict[ConditionTier, Presets]
    platform_recommendations: dict[Platform, PlatformRecommendation] = Field(
        ..., description="At presets[item.condition_tier].best_ev_gbp; cards re-read the grid as the slider moves"
    )
    markdown_schedule: list[MarkdownStep]
    comparable_listings: list[ListingRow] = Field(default_factory=list, description="5-10 rows for the evidence drawer")
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_grid(self):
        _v = lambda k: getattr(k, "value", k)  # noqa: E731  (dict keys may arrive as enum or str)
        tiers = {_v(t) for t in self.price_curve_grid}
        if tiers != {t.value for t in ConditionTier}:
            raise ValueError("price_curve_grid needs all four condition tiers")
        if {_v(t) for t in self.presets} != tiers:
            raise ValueError("presets needs all four condition tiers")
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
        return self


class ErrorResponse(_Model):
    error: str
    detail: Optional[str] = None
    retryable: bool = False
