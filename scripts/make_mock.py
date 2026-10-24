"""Generate the mock payloads in backend/mock/ and the JSON Schema in contract/ (contract v1.1).

The mock is the REAL engine (backend/engine/pipeline.py) run on simulated listings from
engine/simulate.py, so the frontend builds against exactly what the live app will send.

Run from repo root:  python scripts/make_mock.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic.json_schema import models_json_schema  # noqa: E402

from backend.engine.pipeline import run  # noqa: E402
from backend.engine.simulate import TrueParams, simulate_listings  # noqa: E402
from backend.models.schemas import (  # noqa: E402
    ErrorResponse,
    IdentifyResponse,
    IdentifyTextRequest,
    MarketScanRequest,
    MarketScanResponse,
)

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
ITEM = {
    "title": "Nike Air Force 1 '07 Low White",
    "brand": "Nike",
    "model_name": "Air Force 1 '07 Low",
    "category": "Footwear / Trainers",
    "size_or_storage": "UK 9",
    "era_or_year": None,
    "condition_tier": "good",
    "condition_score": 7.4,
    "detected_flaws": ["Light creasing on toebox", "Minor heel sole wear"],
    "search_queries": {"ebay": "nike air force 1 07 white", "vinted": "air force 1 white"},
    "is_broken": False,
}
TITLES = {
    "ebay": ["Nike Air Force 1 '07 White UK 9", "Mens Nike Air Force 1 07 Triple White", "Nike Air Force 1 07 Low White Trainers",
             "Nike AF1 Air Force 1 '07 White Leather"],
    "vinted": ["Nike air force 1 white size 9", "Air Force 1 white", "white air force 1 trainers", "Nike Air Force 1 07 white"],
}


def build_rows():
    rows = simulate_listings(TrueParams(), n_per_platform=300, now=NOW, seed=7)
    rng = np.random.default_rng(11)
    for r in rows:
        r["title"] = TITLES[r["platform"]][rng.integers(4)]
        if r["platform"] == "ebay":
            r["url"] = f"https://www.ebay.co.uk/itm/{r['listing_id']}"
        else:
            r["url"] = f"https://www.vinted.co.uk/items/{r['listing_id']}"
    # a few junk rows so the evidence drawer and filter have something to show
    junk = dict(rows[0], listing_id="eb-junk1", title="Nike Air Force 1 box only", price_gbp=6.0)
    junk2 = dict(rows[1], listing_id="eb-junk2", title="Nike Air Force 1 07 white BUNDLE x3", price_gbp=95.0)
    return rows + [junk, junk2]


def build_market_scan():
    out = run(ITEM, build_rows(), now=NOW, data_source="mock")
    out["cleaned_image_url"] = "/static/mock/cleaned_item_01.png"
    out["warnings"] = ["MOCK DATA: real engine on simulated listings (scripts/make_mock.py)"] + out["warnings"]
    return out


def build_identify():
    return {"item": ITEM, "source": "vision", "cleaned_image_url": "/static/mock/cleaned_item_01.png",
            "confidence": 0.86, "warnings": []}


def main():
    mock_dir = ROOT / "backend" / "mock"
    contract_dir = ROOT / "contract"
    mock_dir.mkdir(exist_ok=True)
    contract_dir.mkdir(exist_ok=True)

    scan = MarketScanResponse.model_validate(build_market_scan())
    ident = IdentifyResponse.model_validate(build_identify())
    (mock_dir / "mock_market_scan.json").write_text(scan.model_dump_json(indent=2))
    (mock_dir / "mock_identify.json").write_text(ident.model_dump_json(indent=2))

    # one combined schema with shared $defs, so the TS generator emits each type once
    names = ["IdentifyResponse", "MarketScanResponse", "MarketScanRequest", "IdentifyTextRequest", "ErrorResponse"]
    models = [IdentifyResponse, MarketScanResponse, MarketScanRequest, IdentifyTextRequest, ErrorResponse]
    _, schema = models_json_schema([(m, "serialization") for m in models], ref_template="#/$defs/{model}")
    schema.update({"title": "FlipCurveApi", "type": "object",
                   "properties": {n: {"$ref": f"#/$defs/{n}"} for n in names}})
    (contract_dir / "schema.json").write_text(json.dumps(schema, indent=2))
    ms = scan.market_summary
    print(f"wrote mocks + schema; listings={len(scan.comparable_listings)} sold={ms.sample_size_sold} "
          f"active={ms.sample_size_active} excluded={ms.sample_size_excluded} bias={ms.survivorship_bias_pct}% "
          f"best={scan.best_platform}")
    for pl, rec in scan.platform_recommendations.items():
        print(f"  {pl}: list £{rec.recommended_list_gbp} -> net £{rec.net_payout_gbp}, "
              f"{rec.expected_days}d ({rec.expected_days_low}-{rec.expected_days_high}), {rec.confidence}")


if __name__ == "__main__":
    main()
