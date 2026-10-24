"""FastAPI entrypoint.

/market-scan runs the real pipeline: orchestrator (live -> cached listings) -> engine.
If there are no listings at all it serves the mock payload, clearly labelled, so the UI
never breaks during a demo. /identify uses Gemini; if Gemini is busy or unset it returns an
editable placeholder with a warning.

Run:  uvicorn backend.main:app --reload --port 8000
Docs: http://localhost:8000/docs
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from backend.engine.pipeline import run as run_engine
from backend.models.schemas import (
    IdentifyResponse,
    IdentifyTextRequest,
    MarketScanRequest,
    MarketScanResponse,
)
from backend.scrapers.orchestrator import gather
from backend.vision.gemini_identifier import GeminiIdentifier, IdentifyError

load_dotenv(Path(__file__).resolve().parents[1] / ".env")  # GEMINI_API_KEY, EBAY_*, FLIPCURVE_*
MOCK_DIR = Path(__file__).parent / "mock"
log = logging.getLogger("flipcurve")

app = FastAPI(title="FlipCurve API", version="1.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # hackathon: open CORS so any dev server / phone on the LAN can call us
    allow_methods=["*"],
    allow_headers=["*"],
)


def _mock(name: str) -> dict:
    return json.loads((MOCK_DIR / name).read_text())


@app.get("/api/v1/health")
def health():
    return {"ok": True}


def _identify_fallback(source: str, why: str) -> dict:
    """Gemini unavailable: return an editable placeholder so the user can type the item in."""
    data = _mock("mock_identify.json")
    data.update(source=source, cleaned_image_url=None, confidence=0.0,
                warnings=[f"Couldn't identify automatically ({why}). Please check and edit the details."])
    return data


@app.post("/api/v1/identify", response_model=IdentifyResponse)
async def identify(
    images: list[UploadFile] = File(..., description="1-2 photos: overview + optional label close-up"),
    hint: Optional[str] = Form(None, description="Optional typed hint, e.g. 'size 9'"),
):
    blobs = [await f.read() for f in images[:2]]
    try:
        out = GeminiIdentifier().from_images(blobs, hint=hint)
    except IdentifyError as e:
        log.warning("identify failed: %s", e)
        return _identify_fallback("vision", "the image model is busy or unavailable")
    return {"item": out["item"], "source": "vision", "confidence": out["confidence"], "warnings": []}


@app.post("/api/v1/identify-text", response_model=IdentifyResponse)
def identify_text(req: IdentifyTextRequest):
    tier = getattr(req.condition_tier, "value", req.condition_tier)
    try:
        out = GeminiIdentifier().from_text(req.query, condition_tier=tier)
    except IdentifyError as e:
        log.warning("identify-text failed: %s", e)
        data = _identify_fallback("text", "the model is busy or unavailable")
        q = req.query.strip().lower()
        data["item"].update(title=req.query.strip(), brand=None, model_name=None, size_or_storage=None,
                            detected_flaws=[], search_queries={"ebay": q, "vinted": q},
                            condition_tier=tier or "good")
        return data
    return {"item": out["item"], "source": "text", "confidence": out["confidence"], "warnings": []}


@app.post("/api/v1/market-scan", response_model=MarketScanResponse)
def market_scan(req: MarketScanRequest):
    item = req.item.model_dump(mode="json")
    platforms = [getattr(p, "value", p) for p in req.platforms]
    queries = item["search_queries"]
    rows, source, warnings = gather(queries, platforms, allow_live=req.allow_live)
    if rows:
        try:
            out = run_engine(item, rows, data_source=source, platforms=platforms)
            out["warnings"] = warnings + out["warnings"]
            return out
        except Exception:  # noqa: BLE001 - never break the demo; log and fall through to mock
            log.exception("engine failed")
            warnings.append("engine error on these listings; showing example data")
    data = _mock("mock_market_scan.json")
    data["item_metadata"] = item
    data["warnings"] = warnings + ["Showing EXAMPLE data: no listings found for this item"] + data["warnings"]
    return data
