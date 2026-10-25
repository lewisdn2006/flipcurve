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
import gzip

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend.engine.pipeline import run as run_engine
from backend.models.schemas import (
    IdentifyResponse,
    IdentifyTextRequest,
    MarketScanRequest,
    MarketScanResponse,
    RecheckResultsRequest,
    ScanPagesRequest,
)
from backend.scrapers.orchestrator import gather
from backend.vision.gemini_identifier import GeminiIdentifier, IdentifyError

load_dotenv(Path(__file__).resolve().parents[1] / ".env")  # GEMINI_API_KEY, EBAY_*, FLIPCURVE_*
MOCK_DIR = Path(__file__).parent / "mock"
log = logging.getLogger("flipcurve")

app = FastAPI(title="FlipCurve API", version="1.1")
app.add_middleware(
    CORSMiddleware,
    # Only FlipCurve's own pages may call this from a browser: lewisdn.com (and subdomains), a
    # trycloudflare test address, and localhost / LAN addresses while developing.
    allow_origin_regex=r"https://([a-z0-9-]+\.)*lewisdn\.com|https://[a-z0-9-]+\.trycloudflare\.com"
                       r"|http://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)
WEB_DIR = Path(__file__).resolve().parents[1] / "web" / "flipcurve"


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/flipcurve/")


def _mock(name: str) -> dict:
    return json.loads((MOCK_DIR / name).read_text())


@app.get("/api/v1/health")
def health():
    from backend.scrapers import ebay_api
    return {"ok": True, "ebay_api": ebay_api.configured()}


def _identify_fallback(source: str, why: str) -> dict:
    """Gemini unavailable: return an editable placeholder so the user can type the item in."""
    # a blank item, not the sample trainers: a failed T-shirt photo showed "creasing on toebox" (25 Oct)
    item = {"title": "", "brand": None, "model_name": None, "category": None, "size_or_storage": None,
            "era_or_year": None, "condition_tier": "good", "condition_score": 6.0, "detected_flaws": [],
            "search_queries": {"ebay": "", "vinted": ""}, "is_broken": False, "must_have": [], "must_not_have": []}
    return {"item": item, "source": source, "cleaned_image_url": None, "confidence": 0.0,
            "warnings": [f"Couldn't identify the photo automatically ({why}). Type what it is and the "
                         "searches below, or try Identify again in a minute."]}


@app.post("/api/v1/identify", response_model=IdentifyResponse)
async def identify(
    images: list[UploadFile] = File(..., description="1-4 photos: overview, other angles, label close-up"),
    hint: Optional[str] = Form(None, description="Optional typed hint, e.g. 'size 9'"),
):
    blobs = [await f.read() for f in images[:4]]
    # in a worker thread: the Gemini call takes a while and mustn't freeze the whole server
    from backend.vision.gemini_identifier import DEFAULT_BUDGET_S
    return await run_in_threadpool(_identify_photos, blobs, hint, DEFAULT_BUDGET_S)


# ---- identify as a background job: the page starts it and polls, so a slow model can't run into
# Cloudflare's 100 s cut-off on the tunnel ("failed to fetch", 25 Oct)
import asyncio  # noqa: E402
import uuid  # noqa: E402
import time as _time  # noqa: E402

JOBS: dict[str, dict] = {}
JOB_KEEP_S = 900


def _start_job(fn) -> str:
    now = _time.time()
    for k in [k for k, v in JOBS.items() if now - v["started"] > JOB_KEEP_S]:
        JOBS.pop(k, None)
    jid = uuid.uuid4().hex[:16]
    JOBS[jid] = {"status": "running", "started": now, "result": None}

    async def runner():
        try:
            JOBS[jid]["result"] = await run_in_threadpool(fn)
        except Exception as e:  # noqa: BLE001 - always finish the job with something editable
            log.exception("identify job failed")
            JOBS[jid]["result"] = _identify_fallback("vision", f"something went wrong: {type(e).__name__}")
        JOBS[jid]["status"] = "done"
    asyncio.get_running_loop().create_task(runner())
    return jid


def _identify_photos(blobs: list[bytes], hint: str | None, budget_s: float) -> dict:
    from backend.vision.gemini_identifier import IdentifyError as _IE
    try:
        out = GeminiIdentifier(budget_s=budget_s).from_images(blobs, hint=hint)
    except _IE as e:
        log.warning("identify failed: %s", e)
        # say what actually happened to each model, e.g. "gemini-3.8-flash: no answer within 40 s; Claude: busy"
        return _identify_fallback("vision", e.reason() if hasattr(e, "reason") else str(e))
    from backend.vision.gemini_identifier import pretty_model
    return {"item": out["item"], "source": "vision", "confidence": out["confidence"],
            "warnings": [f"Identified by {pretty_model(out['model'] or '')} (the main model didn't answer)"]
            if out.get("fallback") else []}


@app.post("/api/v1/identify/start")
async def identify_start(images: list[UploadFile] = File(...), hint: Optional[str] = Form(None)):
    from backend.vision.gemini_identifier import JOB_BUDGET_S
    blobs = [await f.read() for f in images[:4]]
    return {"job": _start_job(lambda: _identify_photos(blobs, hint, JOB_BUDGET_S))}


@app.get("/api/v1/identify/job/{jid}")
def identify_job(jid: str):
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "unknown or expired job")
    out = {"status": job["status"], "seconds": round(_time.time() - job["started"])}
    if job["status"] == "done":
        out["result"] = IdentifyResponse.model_validate(job["result"]).model_dump(mode="json")
    return out


def _identify_text(req: IdentifyTextRequest, budget_s: float) -> dict:
    tier = getattr(req.condition_tier, "value", req.condition_tier)
    try:
        out = GeminiIdentifier(budget_s=budget_s).from_text(req.query, condition_tier=tier)
    except IdentifyError as e:
        log.warning("identify-text failed: %s", e)
        data = _identify_fallback("text", "the model is busy or unavailable")
        q = req.query.strip().lower()
        data["item"].update(title=req.query.strip(), search_queries={"ebay": q, "vinted": q},
                            condition_tier=tier or "good")
        why = e.reason() if hasattr(e, "reason") else "the AI models are busy"
        data["warnings"] = [f"Couldn't look it up automatically ({why}). Your description is used as "
                            "the search; check it below."]
        return data
    return {"item": out["item"], "source": "text", "confidence": out["confidence"], "warnings": []}


@app.post("/api/v1/identify-text", response_model=IdentifyResponse)
async def identify_text(req: IdentifyTextRequest):
    from backend.vision.gemini_identifier import DEFAULT_BUDGET_S
    return await run_in_threadpool(_identify_text, req, DEFAULT_BUDGET_S)


@app.post("/api/v1/identify-text/start")
async def identify_text_start(req: IdentifyTextRequest):
    from backend.vision.gemini_identifier import JOB_BUDGET_S
    return {"job": _start_job(lambda: _identify_text(req, JOB_BUDGET_S))}


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


STOP_MESSAGES = {
    "sign_in_required": "eBay needs you signed in to show sold listings. Sign in to eBay in this browser, then scan again.",
    "refused": "{site} declined the request for now, so these results use saved data for it.",
    "slow": "{site} slowed right down part-way, so this uses the pages that arrived. Scanning again in a few minutes may get more.",
    "network_error": "{site} couldn't be reached, so these results use saved data for it.",
}


@app.get("/api/v1/recheck-ids")
def recheck_ids(vinted_query: str):
    """Vinted listings from earlier scans that the extension should re-check during this scan."""
    from backend.scrapers.page_ingest import recheck_ids as _ids
    return {"ids": _ids(vinted_query)}


@app.get("/api/v1/recheck-due")
def recheck_due(limit: int = 20):
    """For the extension's background run: Vinted listings (from any earlier scan) to re-open now."""
    from backend.scrapers.page_ingest import due_rechecks
    return {"items": due_rechecks(limit=max(1, min(limit, 30)))}


async def _json_body(request: Request, model):
    body = await request.body()
    if request.headers.get("content-encoding", "").lower() == "gzip":
        body = gzip.decompress(body)
    try:
        return model.model_validate_json(body)
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False, include_context=False)) from None


@app.post("/api/v1/recheck-results", openapi_extra={"requestBody": {
    "required": True, "content": {"application/json": {"schema": RecheckResultsRequest.model_json_schema()}}}})
async def recheck_results(request: Request):
    """The extension's background re-checks: which listings sold, are still up, or were deleted."""
    from backend.scrapers.page_ingest import apply_background_rechecks
    req = await _json_body(request, RecheckResultsRequest)
    out = await run_in_threadpool(apply_background_rechecks, [r.model_dump(mode="json") for r in req.results])
    log.info("background re-checks: %s", out)
    return out


@app.post("/api/v1/scan-pages", response_model=MarketScanResponse, openapi_extra={"requestBody": {
    "required": True, "content": {"application/json": {"schema": ScanPagesRequest.model_json_schema()}}}})
async def scan_pages(request: Request):
    """Scan using pages the helper extension collected in the user's own browser.
    The body is a ScanPagesRequest; send it gzipped with Content-Encoding: gzip (pages are big)."""
    req = await _json_body(request, ScanPagesRequest)
    return await run_in_threadpool(_scan_pages, req)


def _scan_pages(req: ScanPagesRequest):
    from backend.scrapers.orchestrator import load_cache
    from backend.scrapers.page_ingest import ingest
    item = req.item.model_dump(mode="json")
    queries = item["search_queries"]
    pages = [p.model_dump(mode="json") for p in req.pages]
    warnings = []
    rows, stats = ingest(pages, queries)
    have = {r["platform"] for r in rows}
    if (stats.get("ebay") or {}).get("api_error"):
        warnings.append("The eBay API didn't answer this time, so eBay listings come from the helper's pages only.")
    for site, info in (req.stopped or {}).items():
        msg = STOP_MESSAGES.get((info or {}).get("reason"), "{site} stopped early.")
        warnings.append(msg.format(site="eBay" if site == "ebay" else "Vinted"))
    for site in ("ebay", "vinted"):
        if site not in have:  # nothing fresh from this site: fall back to its saved data
            cached = load_cache(queries[site], site) or []
            rows += cached
            if cached:
                warnings.append(f"Using saved {'eBay' if site == 'ebay' else 'Vinted'} data from an earlier scan.")
    if not rows:
        data = _mock("mock_market_scan.json")
        data["item_metadata"] = item
        data["warnings"] = warnings + ["Showing EXAMPLE data: no listings found for this item"] + data["warnings"]
        return data
    try:
        out = run_engine(item, rows, data_source="live" if pages else "seed")
    except Exception:  # noqa: BLE001
        log.exception("engine failed on extension pages")
        data = _mock("mock_market_scan.json")
        data["item_metadata"] = item
        data["warnings"] = warnings + ["Engine error on these listings; showing example data"]
        return data
    out["warnings"] = warnings + out["warnings"]
    log.info("scan-pages stats: %s", stats)
    if queries.get("vinted"):  # background re-checks follow only the listings that matched this item
        from backend.scrapers.page_ingest import mark_relevant
        mark_relevant(queries["vinted"], [r["listing_id"] for r in out["comparable_listings"]
                                          if r["platform"] == "vinted" and not r.get("is_outlier")])
    return out


# The FlipCurve page itself, so http://localhost:8000/flipcurve/ works for testing.
# (On lewisdn.com the same file is served by Cloudflare and talks to this server via the tunnel.)
app.mount("/flipcurve", StaticFiles(directory=WEB_DIR, html=True), name="web")
