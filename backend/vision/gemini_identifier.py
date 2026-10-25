"""Identify an item from 1-2 photos (or a typed description) with Gemini -> ItemSpec.

Gemini is asked for strict JSON (response schema) describing the item, its condition on our
four tiers, visible flaws, and two search queries written the way people search each site.
Google sometimes answers 503 "high demand"; we retry with backoff before giving up.

Owner: Person 2.
"""
from __future__ import annotations

import base64
import re
import io
import logging
import json
import os
import time

import httpx

API = "https://generativelanguage.googleapis.com/v1beta"
log = logging.getLogger("flipcurve")
RETRY_STATUS = {429, 500, 502, 503, 504}
BACKOFF_S = (1.0, 2.5, 5.0)     # per model; then the next model
DEFAULT_BUDGET_S = 70.0          # whole identify for the old one-request endpoint (Cloudflare cuts at 100 s)
JOB_BUDGET_S = 170.0             # identify run as a background job the page polls: no 100 s limit
REQUEST_TIMEOUT_S = 75.0
GEMINI_MAX_SIDE = 1280           # photos sent to Gemini: plenty to read a label, fewer image tiles = faster
SKIP_IN_FALLBACK = ("image", "tts", "audio", "live", "embedding", "thinking-exp", "native")

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
- must_have: 1-4 lowercase words that nearly every correct listing title includes, naming the model
  (not the brand alone, not the colour unless it's part of the name). Give alternatives with "|".
  e.g. Jordan 1 Low Triple Black -> ["jordan", "low"]; iPhone 13 Pro 128GB -> ["iphone 13", "pro"]
- must_not_have: lowercase words that would mean a different item: other colourways or
  collaborations of the same model, a different version, replicas. e.g. Jordan 1 Low Triple Black
  -> ["white", "red", "blue", "grey", "panda", "travis", "psg", "toe", "replica"]
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
        "must_have": {"type": "ARRAY", "items": {"type": "STRING"}},
        "must_not_have": {"type": "ARRAY", "items": {"type": "STRING"}},
        "is_broken": {"type": "BOOLEAN"},
        "confidence": {"type": "NUMBER"},
    },
    "required": ["title", "condition_tier", "condition_score", "detected_flaws", "search_queries", "confidence"],
}


class IdentifyError(RuntimeError):
    pass


CLAUDE_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_DEFAULT_MODEL = "claude-haiku-5-5"   # fast, reads images; override with CLAUDE_MODEL
CLAUDE_JSON_NOTE = ("\nReply with only one JSON object (no other text) with these keys: title, brand, model_name, "
                    "category, size_or_storage, era_or_year, condition_tier, condition_score, detected_flaws, "
                    "search_queries {ebay, vinted}, must_have, must_not_have, is_broken, confidence.")


def claude_configured() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def _thinking_config(model: str) -> dict | None:
    """Least thinking each model family allows (ai.google.dev/gemini-api/docs/thinking):
    Gemini 3+ takes thinkingLevel; 2.5 Flash takes a thinkingBudget (0 = off)."""
    m = model.lower()
    if "2.5" in m and "flash" in m:
        return {"thinkingBudget": 0}
    if re.search(r"gemini-(\d+)", m) and int(re.search(r"gemini-(\d+)", m).group(1)) >= 3:
        return {"thinkingLevel": "low"}
    return None


def _shrink(data: bytes, max_side: int = 1600) -> tuple[bytes, str]:
    """Downscale big phone photos to a JPEG before sending (faster, cheaper). Handles iPhone HEIC
    photos too (via pillow-heif), which OpenCV can't read: they used to go up full size with the
    wrong type. Falls back to the original bytes."""
    try:
        from PIL import Image, ImageOps
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=88)
        return buf.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 - try OpenCV, then send as-is
        pass
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is not None:
            h, w = img.shape[:2]
            s = max_side / max(h, w)
            if s < 1:
                img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
            if ok:
                return buf.tobytes(), "image/jpeg"
    except ImportError:
        pass
    return data, _sniff_mime(data)


def _sniff_mime(data: bytes) -> str:
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypmsf1", b"ftyphevc"):
        return "image/heic"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _terms(v, n: int) -> list[str]:
    out = []
    for t in (v or [])[:n]:
        t = " ".join(str(t).lower().split())
        if t and t not in out:
            out.append(t)
    return out


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
        "must_have": _terms(raw.get("must_have"), 4),
        "must_not_have": _terms(raw.get("must_not_have"), 15),
    }
    conf = raw.get("confidence")
    return {"item": item, "confidence": float(min(max(conf, 0.0), 1.0)) if isinstance(conf, (int, float)) else None}


class GeminiIdentifier:
    def __init__(self, api_key: str | None = None, model: str | None = None, client: httpx.Client | None = None,
                 sleep=time.sleep, budget_s: float = DEFAULT_BUDGET_S):
        self.key = api_key or os.getenv("GEMINI_API_KEY", "")
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
        self.http = client or httpx.Client(timeout=REQUEST_TIMEOUT_S)
        self.sleep = sleep
        # The whole identify, retries and fallbacks included, must finish well inside Cloudflare's
        # 100 s limit on the tunnel (25 Oct: retries while Gemini was busy ran past it -> "failed to fetch").
        self.budget_s = budget_s
        self._deadline = None
        self.light_thinking = True
        if not self.key:
            raise IdentifyError("GEMINI_API_KEY is not set")

    def fallback_models(self) -> list[str]:
        """Other Flash models to try when the main one is overloaded (503 "high demand", 25 Oct).
        GEMINI_FALLBACK_MODELS (comma-separated) if set; otherwise the newest Flash models this key
        can use, from the models list. Never raises."""
        env = os.getenv("GEMINI_FALLBACK_MODELS")
        if env is not None:
            return [m.strip() for m in env.split(",") if m.strip() and m.strip() != self.model][:3]
        try:
            r = self.http.get(f"{API}/models", headers={"x-goog-api-key": self.key}, params={"pageSize": 200},
                              timeout=10)
            if r.status_code != 200:
                return []
            names = [m["name"].split("/", 1)[-1] for m in r.json().get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", [])]
        except Exception:  # noqa: BLE001
            return []
        flash = [n for n in names if "flash" in n and n != self.model and not any(k in n for k in SKIP_IN_FALLBACK)]
        return sorted(flash, reverse=True)[:2]  # newest versions sort last alphabetically

    def _left(self) -> float:
        return self._deadline - time.monotonic() if self._deadline else self.budget_s

    def _call(self, parts: list[dict], use_schema: bool = True) -> dict:
        errors, fallbacks = [], None
        model = self.model
        self.used_model = None
        self._deadline = time.monotonic() + self.budget_s
        while model:
            if self._left() < 5:
                errors.append("out of time")
                break
            out, err, busy = self._call_model(model, parts, use_schema)
            if out is not None:
                self.used_model = model
                return out
            errors.append(f"{model}: {err}")
            if not busy and not err.startswith("HTTP 404"):
                break  # a real error (bad key, bad request): another model won't help
            if fallbacks is None:
                # a different provider first: when Gemini is overloaded, other Geminis often are too
                fallbacks = ([f"claude:{os.getenv('CLAUDE_MODEL', CLAUDE_DEFAULT_MODEL)}"] if claude_configured() else [])
                fallbacks += self.fallback_models()
            model = fallbacks.pop(0) if fallbacks else None
        raise IdentifyError("Gemini failed (" + "; ".join(errors) + ")")

    def _call_claude(self, model: str, parts: list[dict]) -> tuple[dict | None, str, bool]:
        """Same request to Anthropic's Messages API; the answer comes back in Gemini's response
        shape so the rest of the code doesn't care which AI answered."""
        content = []
        for p in parts:
            if "inline_data" in p:
                mime = p["inline_data"]["mime_type"]
                if mime not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                    continue  # (photos are converted to JPEG before this point)
                content.append({"type": "image", "source": {"type": "base64", "media_type": mime,
                                                             "data": p["inline_data"]["data"]}})
        text = " ".join(p["text"] for p in parts if "text" in p) + CLAUDE_JSON_NOTE
        content.append({"type": "text", "text": text})
        left = self._left()
        if left < 5:
            return None, "out of time", True
        try:
            r = self.http.post(CLAUDE_URL, json={"model": model, "max_tokens": 1500,
                                                 "messages": [{"role": "user", "content": content}]},
                               headers={"x-api-key": os.getenv("ANTHROPIC_API_KEY", ""),
                                        "anthropic-version": os.getenv("ANTHROPIC_VERSION", "2023-06-01"),
                                        "content-type": "application/json"},
                               timeout=min(REQUEST_TIMEOUT_S, left - 2))
        except httpx.TimeoutException as e:
            return None, f"no reply in time ({type(e).__name__})", True
        except httpx.HTTPError as e:
            return None, f"network error: {type(e).__name__}", True
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:200]}", r.status_code in RETRY_STATUS | {529}
        out = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        log.warning("Gemini %s was busy; used Claude %s", self.model, model)
        return {"candidates": [{"content": {"parts": [{"text": out}]}}]}, "", False

    def _call_model(self, model: str, parts: list[dict], use_schema: bool) -> tuple[dict | None, str, bool]:
        """(response, last error, was it 'busy') for one model, with retries and backoff."""
        if model.startswith("claude:"):
            return self._call_claude(model.split(":", 1)[1], parts)
        cfg = {"responseMimeType": "application/json", "temperature": 0.2}
        if use_schema:
            cfg["responseSchema"] = SCHEMA
        think = _thinking_config(model) if self.light_thinking else None
        if think:
            # Gemini 3 Flash thinks at "high" by default, which can take a long time before it
            # answers (identify ran past 45 s, 25 Oct); naming a product doesn't need it.
            cfg["thinkingConfig"] = think
        body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": cfg}
        url = f"{API}/models/{model}:generateContent"
        last, busy = None, False
        for attempt in range(len(BACKOFF_S) + 1):
            left = self._left()
            if left < 5:
                return None, (last or "") + " (out of time)", busy
            try:
                r = self.http.post(url, json=body, headers={"x-goog-api-key": self.key},
                                   timeout=min(REQUEST_TIMEOUT_S, left - 2))
            except httpx.TimeoutException as e:
                return None, f"no reply in time ({type(e).__name__})", True  # slow: try another model
            except httpx.HTTPError as e:
                last, busy = f"network error: {type(e).__name__}", True
            else:
                if r.status_code == 200:
                    if model != self.model:
                        log.warning("Gemini %s was busy; used %s", self.model, model)
                    return r.json(), "", False
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code == 400 and self.light_thinking and "think" in r.text.lower():
                    self.light_thinking = False  # model without that setting: ask again without it
                    return self._call_model(model, parts, use_schema)
                if r.status_code == 400 and use_schema and "schema" in r.text.lower():
                    return self._call_model(model, parts, use_schema=False)  # model without schema support
                busy = r.status_code in RETRY_STATUS
                if not busy:
                    break
            if attempt < len(BACKOFF_S):
                if self._left() - BACKOFF_S[attempt] < 8:
                    break  # not enough time for another go: try the next model instead
                self.sleep(BACKOFF_S[attempt])
        return None, last, busy

    def _parse(self, resp: dict) -> dict:
        try:
            text = "".join(p.get("text", "") for p in resp["candidates"][0]["content"]["parts"])
            text = text.strip().removeprefix("```json").removesuffix("```").strip()
            if not text.startswith("{") and "{" in text:  # tolerate a sentence around the JSON
                text = text[text.index("{"):text.rindex("}") + 1]
            return _clean(json.loads(text))
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise IdentifyError(f"Gemini returned something unexpected: {type(e).__name__}") from e

    def from_images(self, images: list[bytes], hint: str | None = None) -> dict:
        parts = [{"text": PROMPT + (f"\nThe seller adds: {hint}" if hint else "")}]
        for data in images[:4]:
            small, mime = _shrink(data, max_side=GEMINI_MAX_SIDE)
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(small).decode()}})
        out = self._parse(self._call(parts))
        out.update(model=self.used_model, fallback=self.used_model != self.model)
        return out

    def from_text(self, query: str, condition_tier: str | None = None) -> dict:
        extra = f"\nThe seller says the condition is: {condition_tier}." if condition_tier else ""
        out = self._parse(self._call([{"text": PROMPT + f"\nDescription: {query}{extra}"}]))
        out.update(model=self.used_model, fallback=self.used_model != self.model)
        if condition_tier:
            out["item"]["condition_tier"] = condition_tier
        return out
