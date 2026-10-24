"""FastAPI entrypoint.

Phase 0: both routes return the frozen mock payloads so the frontend can build immediately.
Person 2 swaps the mock bodies for the real pipeline route by route; the response models
stay exactly as they are.

Run:  uvicorn backend.main:app --reload --port 8000
Docs: http://localhost:8000/docs
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from backend.models.schemas import (
    IdentifyResponse,
    IdentifyTextRequest,
    MarketScanRequest,
    MarketScanResponse,
)

MOCK_DIR = Path(__file__).parent / "mock"

app = FastAPI(title="FlipCurve API", version="1.0")
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
    return {"ok": True, "mode": "mock"}


@app.post("/api/v1/identify", response_model=IdentifyResponse)
async def identify(
    images: list[UploadFile] = File(..., description="1-2 photos: overview + optional label close-up"),
    hint: Optional[str] = Form(None, description="Optional typed hint, e.g. 'size 9'"),
):
    # TODO(Person 2): opencv_pipeline -> gemini_identifier
    return _mock("mock_identify.json")


@app.post("/api/v1/identify-text", response_model=IdentifyResponse)
def identify_text(req: IdentifyTextRequest):
    # TODO(Person 2): Gemini text-only extraction for the typed-search fallback
    data = _mock("mock_identify.json")
    data["source"] = "text"
    data["cleaned_image_url"] = None
    return data


@app.post("/api/v1/market-scan", response_model=MarketScanResponse)
def market_scan(req: MarketScanRequest):
    # TODO: orchestrator -> outlier_filter -> survival_model -> platform_economics
    data = _mock("mock_market_scan.json")
    data["item_metadata"] = req.item.model_dump(mode="json")
    return data
