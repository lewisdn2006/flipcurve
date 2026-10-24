"""Identify an item from 1-2 photos (or a typed description) with Gemini -> ItemSpec.

Gemini is asked for strict JSON (response schema) describing the item, its condition on our
four tiers, visible flaws, and two search queries written the way people search each site.
Google sometimes answers 503 "high demand"; we retry with backoff before giving up.

Owner: Person 2.
"""
from __future__ import annotations

import base64
import json
import os
import time

import httpx

API = "https://generativelanguage.googleapis.com/v1beta"
RETRY_STATUS = {429, 500, 502, 503, 504}
BACKOFF_S = (1.0, 2.5, 5.0, 10.0)

PROMPT = """You are helping someone in the UK price a secondhand item they own, to sell on eBay or Vinted.
Look at the photo(s) (or read the description) and identify the exact product.

Return JSON only, matching the schema. Rules:
- title: how a seller would title it, e.g. "Nike Air Force 1 '07 Low White"
- brand, model_name, category, size_or_storage, era_or_year: null if you can't tell
- condition_tier: one of fair, good, very_good, mint (mint = new / new with tags)
- condition_score: 0-10, where 10 is brand new
- detected_flaws: short phrases for visible wear, e.g. "light creasing on toe box"; [] if none
- search_queries.ebay: 3-7 words, brand + model + key attribute, no size, e.g. "nike air force 1 07 white"
- search_queries.vinted: 2-5 casual words, e.g. "air force 1 white"
- is_broken: true only if it looks broken or for parts
- confidence: 0-1, how sure you are of the exact product
"""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "brand": {"type": "STRING", "nullable": True},
        "model_name": {"type": "STRING", "nullable": True},
        "category": {"type": "STRING", "nullable": True},
        "size_or_storage": {"type": "STRING", "nullable": True},
        "era_or_year": {"type": "STRING", "nullable": True},
        "condition_tier": {"type": "STRING", "enum": ["fair", "good", "very_good", "mint"]},
        "condition_score": {"type": "NUMBER"},
        "detected_flaws": {"type": "ARRAY", "items": {"type": "STRING"}},
        "search_queries": {"type": "OBJECT", "properties": {"ebay": {"type": "STRING"}, "vinted": {"type": "STRING"}},
                           "required": ["ebay", "vinted"]},
        "is_broken": {"type": "BOOLEAN"},
        "confidence": {"type": "NUMBER"},
    },
    "required": ["title", "condition_tier", "condition_score", "detected_flaws", "search_queries", "confidence"],
}


class IdentifyError(RuntimeError):
    pass


def _shrink(data: bytes, max_side: int = 1600) -> tuple[bytes, str]:
    """Downscale big phone photos before upload (faster, cheaper). Falls back to the original."""
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return data, "image/jpeg"
        h, w = img.shape[:2]
        s = max_side / max(h, w)
        if s < 1:
            img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
        return (buf.tobytes(), "image/jpeg") if ok else (data, "image/jpeg")
    except ImportError:
        return data, "image/jpeg"


def _clean(raw: dict) -> dict:
    """Coerce Gemini's JSON into a valid ItemSpec dict (+ confidence)."""
    tier = raw.get("condition_tier") if raw.get("condition_tier") in ("fair", "good", "very_good", "mint") else "good"
    title = (raw.get("title") or "").strip() or "Unknown item"
    q = raw.get("search_queries") or {}
    ebay_q = (q.get("ebay") or title).strip().lower()
    vinted_q = (q.get("vinted") or ebay_q).strip().lower()
    nz = lambda v: (str(v).strip() or None) if v not in (None, "", "null") else None  # noqa: E731
    item = {
        "title": title, "brand": nz(raw.get("brand")), "model_name": nz(raw.get("model_name")),
        "category": nz(raw.get("category")), "size_or_storage": nz(raw.get("size_or_storage")),
        "era_or_year": nz(raw.get("era_or_year")), "condition_tier": tier,
        "condition_score": float(min(max(float(raw.get("condition_score") or 6.0), 0.0), 10.0)),
        "detected_flaws": [str(f) for f in (raw.get("detected_flaws") or [])][:6],
        "search_queries": {"ebay": ebay_q, "vinted": vinted_q},
        "is_broken": bool(raw.get("is_broken", False)),
    }
    conf = raw.get("confidence")
    return {"item": item, "confidence": float(min(max(conf, 0.0), 1.0)) if isinstance(conf, (int, float)) else None}


class GeminiIdentifier:
    def __init__(self, api_key: str | None = None, model: str | None = None, client: httpx.Client | None = None,
                 sleep=time.sleep):
        self.key = api_key or os.getenv("GEMINI_API_KEY", "")
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
        self.http = client or httpx.Client(timeout=90)
        self.sleep = sleep
        if not self.key:
            raise IdentifyError("GEMINI_API_KEY is not set")

    def _call(self, parts: list[dict], use_schema: bool = True) -> dict:
        cfg = {"responseMimeType": "application/json", "temperature": 0.2}
        if use_schema:
            cfg["responseSchema"] = SCHEMA
        body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": cfg}
        url = f"{API}/models/{self.model}:generateContent"
        last = None
        for attempt in range(len(BACKOFF_S) + 1):
            try:
                r = self.http.post(url, json=body, headers={"x-goog-api-key": self.key})
            except httpx.HTTPError as e:
                last = f"network error: {type(e).__name__}"
            else:
                if r.status_code == 200:
                    return r.json()
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code == 400 and use_schema and "schema" in r.text.lower():
                    return self._call(parts, use_schema=False)  # older/newer model without schema support
                if r.status_code not in RETRY_STATUS:
                    break
            if attempt < len(BACKOFF_S):
                self.sleep(BACKOFF_S[attempt])
        raise IdentifyError(f"Gemini failed ({self.model}): {last}")

    def _parse(self, resp: dict) -> dict:
        try:
            text = "".join(p.get("text", "") for p in resp["candidates"][0]["content"]["parts"])
            text = text.strip().removeprefix("```json").removesuffix("```").strip()
            return _clean(json.loads(text))
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise IdentifyError(f"Gemini returned something unexpected: {type(e).__name__}") from e

    def from_images(self, images: list[bytes], hint: str | None = None) -> dict:
        parts = [{"text": PROMPT + (f"\nThe seller adds: {hint}" if hint else "")}]
        for data in images[:2]:
            small, mime = _shrink(data)
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(small).decode()}})
        return self._parse(self._call(parts))

    def from_text(self, query: str, condition_tier: str | None = None) -> dict:
        extra = f"\nThe seller says the condition is: {condition_tier}." if condition_tier else ""
        out = self._parse(self._call([{"text": PROMPT + f"\nDescription: {query}{extra}"}]))
        if condition_tier:
            out["item"]["condition_tier"] = condition_tier
        return out
