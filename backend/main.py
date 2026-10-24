"""FastAPI entrypoint.

/market-scan runs the real pipeline: orchestrator (live -> cached listings) -> engine.
If there are no listings at all it serves the mock payload, clearly labelled, so the UI
never breaks during a demo. /identify is still mock until the Gemini identifier lands.

Run:  uvicorn backend.main:app --reload --port 8000
Docs: http://localhost:8000/docs
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

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


@app.post("/api/v1/identify", response_model=IdentifyResponse)
async def identify(
    images: list[UploadFile] = File(..., description="1-2 photos: overview + optional label close-up"),
    hint: Optional[str] = Form(None, description="Optional typed hint, e.g. 'size 9'"),
):
    # TODO(step 14): opencv_pipeline -> gemini_identifier
    return _mock("mock_identify.json")


@app.post("/api/v1/identify-text", response_model=IdentifyResponse)
def identify_text(req: IdentifyTextRequest):
    # TODO(step 14): Gemini text-only extraction for the typed-search fallback
    data = _mock("mock_identify.json")
    data["source"] = "text"
    data["cleaned_image_url"] = None
    return data


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
