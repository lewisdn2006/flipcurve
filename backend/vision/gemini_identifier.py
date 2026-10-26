"""Identify an item from 1-2 photos (or a typed description) with Gemini -> ItemSpec.

Gemini is asked for strict JSON (response schema) describing the item, its condition on our
four tiers, visible flaws, and two search queries written the way people search each site.
Google sometimes answers 503 "high demand"; we retry with backoff, then try the backups (Claude,
then other Gemini Flash models). An answer that can't be used (cut off, blocked, not JSON, no item
named) also moves on to the next model rather than giving up (25 Oct: photo identify failed often).

Owner: Person 2.
"""
from __future__ import annotations

import base64
import re
import io
import logging
import json
import os
import threading
import time
from datetime import datetime, timedelta

import httpx

API = "https://generativelanguage.googleapis.com/v1beta"
log = logging.getLogger("flipcurve")
RETRY_STATUS = {429, 500, 502, 503, 504}
BACKOFF_S = (1.0, 2.5, 5.0)     # per model; then the next model
DEFAULT_BUDGET_S = 70.0          # whole identify for the old one-request endpoint (Cloudflare cuts at 100 s)
JOB_BUDGET_S = 180.0             # identify run as a background job the page polls: no 100 s limit (3 min is fine)
REQUEST_TIMEOUT_S = 75.0
GEMMA_TIMEOUT_S = 160.0          # Gemma is slow on the free tier but has allowance left: let it take its time
FIRST_MODEL_CAP_S = 40.0         # longest wait for the first model, so the backups always get a turn
SMALL_RETRY_SIDE = 800           # one more go with smaller photos when the first round failed for non-busy reasons
GEMINI_MAX_SIDE = 1280           # photos sent to Gemini: plenty to read a label, fewer image tiles = faster
# models in the list that can't do this job (make images or speech, live audio, embeddings, agents)
SKIP_IN_FALLBACK = ("image", "tts", "audio", "live", "embedding", "thinking-exp", "native", "aqa", "computer-use",
                    "robotics", "research", "veo", "imagen", "lyria", "translate", "transcribe")
MIN_GEMMA_B = 4                  # smaller Gemmas can't see photos well (or at all)

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
    """Every model failed. .attempts = [(model, reason)] so the page can say what actually happened."""

    def __init__(self, msg: str, attempts: list[tuple[str, str]] | None = None):
        super().__init__(msg)
        self.attempts = attempts or []

    def reason(self) -> str:
        """Short, readable summary for the user, e.g. "gemini-3.8-flash: no answer within 40 s;
        Claude: busy (HTTP 529)"."""
        if not self.attempts:
            return str(self)
        bits = [f"{pretty_model(m)}: {r}" for m, r in self.attempts]
        if not claude_configured() and not any(m.startswith("gemma") for m, _ in self.attempts):
            bits.append("no backup AI set up (add ANTHROPIC_API_KEY to .env, or GEMMA_MODEL)")
        return "; ".join(bits)


def pretty_model(model: str) -> str:
    return "Claude" if model.startswith("claude:") else model


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
    for t in _strs(v)[:n]:
        t = " ".join(str(t).lower().split())
        if t and t not in out:
            out.append(t)
    return out


# ---- tolerant cleanup: models sometimes write "7/10", "85%", "high", a string instead of a list...
# (one of those used to raise a TypeError that ended the whole identify)

CONF_WORDS = {"very high": 0.95, "high": 0.85, "medium": 0.6, "moderate": 0.6, "low": 0.3, "very low": 0.15}
TIER_WORDS = {"very good": "very_good", "very-good": "very_good", "verygood": "very_good", "very_good": "very_good",
              "new": "mint", "brand new": "mint", "new with tags": "mint", "bnwt": "mint", "mint": "mint",
              "good": "good", "fair": "fair", "poor": "fair", "worn": "fair"}


def _num(v) -> tuple[float | None, str]:
    """(number, unit) from 7.5, "7.5", "7/10", "7.5 out of 10", "85%"; unit is "", "%" or "/10"."""
    if isinstance(v, bool) or v is None:
        return None, ""
    if isinstance(v, (int, float)):
        return float(v), ""
    if isinstance(v, str):
        s = v.strip().lower()
        if s in CONF_WORDS:
            return CONF_WORDS[s], "word"
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        if not m:
            return None, ""
        unit = "%" if "%" in s else "/10" if re.search(r"/\s*10\b|out of 10", s) else ""
        return float(m.group()), unit
    return None, ""


def _score(v) -> float:
    x, unit = _num(v)
    if x is None or unit == "word":
        return 6.0
    if unit == "%":
        x = x / 10
    return float(min(max(x, 0.0), 10.0))


def _confidence(v) -> float | None:
    x, unit = _num(v)
    if x is None:
        return None
    if unit == "%":
        x /= 100
    elif unit == "/10":
        x /= 10
    return float(min(max(x, 0.0), 1.0))


def _str(v) -> str | None:
    if v is None or isinstance(v, (dict, bool)):
        return None
    if isinstance(v, (list, tuple)):
        v = ", ".join(str(x) for x in v if x is not None)
    s = str(v).strip()
    return None if s.lower() in ("", "null", "none", "n/a", "unknown") else s


def _strs(v) -> list[str]:
    if v is None or isinstance(v, (bool, int, float, dict)):
        return []
    if isinstance(v, str):
        return [t.strip() for t in re.split(r"[;,\n]", v) if t.strip()]
    return [str(x).strip() for x in v if x is not None and str(x).strip()]


def _tier(v) -> str:
    s = " ".join(str(v or "").lower().replace("_", " ").split())
    return TIER_WORDS.get(s, "good")


def _clean(raw: dict) -> dict:
    """Coerce the model's JSON into a valid ItemSpec dict (+ confidence). Never raises on odd values."""
    title = _str(raw.get("title")) or _str(" ".join(x for x in (_str(raw.get("brand")), _str(raw.get("model_name"))) if x)) \
        or "Unknown item"
    q = raw.get("search_queries")
    if isinstance(q, str):
        q = {"ebay": q, "vinted": q}
    q = q if isinstance(q, dict) else {}
    ebay_q = (_str(q.get("ebay")) or title).lower()
    vinted_q = (_str(q.get("vinted")) or ebay_q).lower()
    item = {
        "title": title, "brand": _str(raw.get("brand")), "model_name": _str(raw.get("model_name")),
        "category": _str(raw.get("category")), "size_or_storage": _str(raw.get("size_or_storage")),
        "era_or_year": _str(raw.get("era_or_year")), "condition_tier": _tier(raw.get("condition_tier")),
        "condition_score": _score(raw.get("condition_score")),
        "detected_flaws": _strs(raw.get("detected_flaws"))[:6],
        "search_queries": {"ebay": ebay_q, "vinted": vinted_q},
        "is_broken": raw.get("is_broken") is True or str(raw.get("is_broken")).lower() == "true",
        "must_have": _terms(raw.get("must_have"), 4),
        "must_not_have": _terms(raw.get("must_not_have"), 15),
    }
    return {"item": item, "confidence": _confidence(raw.get("confidence"))}


class Unusable(Exception):
    """The model answered, but not with something we can use."""

    def __init__(self, reason: str, partial: dict | None = None):
        super().__init__(reason)
        self.partial = partial


BLOCKED = {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY", "OTHER"}


def parse_answer(resp: dict) -> dict:
    """Model reply (Gemini's response shape) -> cleaned item, or Unusable with a readable reason."""
    if not isinstance(resp, dict):
        raise Unusable("empty answer")
    block = (resp.get("promptFeedback") or {}).get("blockReason")
    if block:
        raise Unusable(f"answer blocked ({block})")
    cands = resp.get("candidates") or []
    if not cands:
        raise Unusable("empty answer")
    cand = cands[0] or {}
    finish = cand.get("finishReason") or ""
    # thinking parts (thought: true) are the model's notes, not its answer
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts") or []
                   if isinstance(p, dict) and not p.get("thought"))
    if not text.strip():
        if finish in BLOCKED:
            raise Unusable(f"answer blocked ({finish})")
        if finish == "MAX_TOKENS":
            raise Unusable("ran out of room before answering")
        raise Unusable("empty answer")
    raw = _find_json(text)
    if raw is None:
        shown = " ".join(text.split())
        shown = shown[:400] + ("…" if len(shown) > 400 else "")
        why = "answer was cut off" if finish == "MAX_TOKENS" else "answer wasn't valid JSON"
        raise Unusable(f"{why} (it said: «{shown}»)")
    out = _clean(raw)
    named = _str(raw.get("title")) or _str(raw.get("model_name")) or _str((raw.get("search_queries") or {}).get("ebay")
                                                                          if isinstance(raw.get("search_queries"), dict) else raw.get("search_queries"))
    if not named:
        raise Unusable("couldn't name the item", partial=out)  # another model may do better; kept as a last resort
    return out


ITEM_KEYS = {"title", "search_queries", "condition_tier", "brand", "model_name"}


def _json_candidates(text: str):
    """Places the JSON might be: the whole reply, ```fenced``` blocks, then every balanced {...}
    (last first: models tend to put notes first and the answer last)."""
    t = text.strip()
    yield t
    for m in re.finditer(r"```(?:json|JSON)?\s*(.*?)```", t, re.S):
        yield m.group(1).strip()
    objs, depth, start, in_str, esc = [], 0, None, False, False
    for i, ch in enumerate(t):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                objs.append(t[start:i + 1])
    yield from reversed(objs)
    if start is not None and depth:  # an object that never closed (cut off): try closing it
        yield t[start:] + "}" * depth


def _repair(t: str) -> str:
    """Common slips: comments, trailing commas, curly quotes, Python's True/False/None."""
    t = re.sub(r"//[^\n\"]*$", "", t, flags=re.M)
    t = re.sub(r"/\*.*?\*/", "", t, flags=re.S)
    t = t.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    t = re.sub(r",\s*([}\]])", r"\1", t)
    t = re.sub(r"\bTrue\b", "true", re.sub(r"\bFalse\b", "false", re.sub(r"\bNone\b", "null", t)))
    return t


def _find_json(text: str) -> dict | None:
    """The item object in a model's reply, however it was wrapped; None if there isn't one."""
    import ast
    first = None
    for c in _json_candidates(text):
        for attempt in (c, _repair(c)):
            try:
                v = json.loads(attempt)
            except (json.JSONDecodeError, ValueError):
                try:
                    v = ast.literal_eval(attempt)  # single-quoted keys and strings
                except (ValueError, SyntaxError, MemoryError, RecursionError):
                    continue
            if isinstance(v, list) and v and isinstance(v[0], dict):
                v = v[0]
            if isinstance(v, dict):
                if ITEM_KEYS & set(v):
                    return v
                first = first or v
    return first


def _google_error(r: httpx.Response) -> str:
    """Google's own error message, in full (it's what tells you which limit or which problem)."""
    try:
        return " ".join(str(r.json()["error"]["message"]).split())
    except Exception:  # noqa: BLE001
        return " ".join(r.text.split())[:600]


# ---- models that are out of allowance (HTTP 429) are skipped until their limit resets, instead of
# being asked again on every identify (25 Oct: each identify spent requests on three Flash models
# that could only say 429). Shared by all identifies in this server process.

COOLDOWN: dict[str, dict] = {}     # model -> {"until": epoch seconds, "why": text, "since": epoch seconds}
_COOLDOWN_LOCK = threading.Lock()
_COOLDOWN_LOADED = False


def _cooldown_file():
    """Where cooldowns are kept between restarts (backend/db/ is gitignored). FLIPCURVE_COOLDOWN_FILE=""
    turns that off (tests)."""
    from pathlib import Path
    env = os.getenv("FLIPCURVE_COOLDOWN_FILE")
    if env is not None:
        return Path(env) if env.strip() else None
    return Path(__file__).resolve().parents[1] / "db" / "model_cooldowns.json"


def _load_cooldowns():
    global _COOLDOWN_LOADED
    if _COOLDOWN_LOADED:
        return
    _COOLDOWN_LOADED = True
    f = _cooldown_file()
    try:
        if f and f.exists():
            now = time.time()
            COOLDOWN.update({m: c for m, c in json.loads(f.read_text()).items() if c.get("until", 0) > now})
    except Exception:  # noqa: BLE001 - a broken file just means starting fresh
        pass


def _save_cooldowns():
    f = _cooldown_file()
    if not f:
        return
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(COOLDOWN, indent=1))
    except Exception:  # noqa: BLE001
        pass


def _next_pacific_midnight(now: float) -> float:
    """Gemini API daily limits reset at midnight Pacific time."""
    try:
        from zoneinfo import ZoneInfo
        pt = datetime.fromtimestamp(now, ZoneInfo("America/Los_Angeles"))
        nxt = (pt + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        return nxt.timestamp()
    except Exception:  # noqa: BLE001 - no tz data: assume a day
        return now + 24 * 3600


def quota_details(r: httpx.Response) -> dict:
    """From a 429 body: which limit (quotaId / metric), the per-day flag, and Google's retry delay."""
    out = {"ids": [], "daily": False, "retry_s": None}
    try:
        for d in r.json()["error"].get("details", []) or []:
            t = d.get("@type", "")
            if t.endswith("QuotaFailure"):
                for v in d.get("violations", []) or []:
                    qid = v.get("quotaId") or v.get("quotaMetric") or ""
                    if qid:
                        out["ids"].append(qid + (f" (limit {v['quotaValue']})" if v.get("quotaValue") else ""))
                    if re.search(r"per ?day", qid, re.I):
                        out["daily"] = True
            elif t.endswith("RetryInfo"):
                m = re.match(r"([\d.]+)s", str(d.get("retryDelay", "")))
                if m:
                    out["retry_s"] = float(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    if not out["ids"] and re.search(r"per ?day|daily", r.text, re.I):
        out["daily"] = True
    return out


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M") + (" tomorrow" if datetime.fromtimestamp(ts).date() > datetime.now().date() else "")


def cool_down(model: str, r: httpx.Response, now: float | None = None) -> str:
    """Remember that `model` is out of allowance; returns a description of when it's tried again."""
    now = time.time() if now is None else now
    q = quota_details(r)
    until = _next_pacific_midnight(now) if q["daily"] else now + max(q["retry_s"] or 60.0, 10.0)
    why = ("daily limit" if q["daily"] else "per-minute limit") + (f": {', '.join(q['ids'])}" if q["ids"] else "")
    with _COOLDOWN_LOCK:
        _load_cooldowns()
        COOLDOWN[model] = {"until": until, "why": why, "since": now}
        _save_cooldowns()
    return f"{why}; skipped until {_clock(until)}"


def cooling(model: str, now: float | None = None) -> dict | None:
    now = time.time() if now is None else now
    with _COOLDOWN_LOCK:
        _load_cooldowns()
        c = COOLDOWN.get(model)
        if c and c["until"] <= now:
            COOLDOWN.pop(model, None)
            _save_cooldowns()
            c = None
    return c


class GeminiIdentifier:
    def __init__(self, api_key: str | None = None, model: str | None = None, client: httpx.Client | None = None,
                 sleep=time.sleep, budget_s: float = DEFAULT_BUDGET_S):
        self.key = api_key if api_key is not None else os.getenv("GEMINI_API_KEY", "")
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
        self.http = client or httpx.Client(timeout=REQUEST_TIMEOUT_S)
        self.sleep = sleep
        # The whole identify, retries, backups and the smaller-photo retry included, has to fit the
        # budget (the old one-request endpoint sits behind Cloudflare's 100 s limit).
        self.budget_s = budget_s
        self._deadline = None
        self.light_thinking = True
        self.used_model = None
        self.lead = None               # the model asked first on purpose (PHOTO_AI_FIRST), not a backup
        self._names = None             # models list, fetched at most once
        self._no_json_mode = set()     # models that refused the JSON response setting
        if not self.key and not claude_configured():
            raise IdentifyError("GEMINI_API_KEY is not set")

    def _list_models(self) -> list[str]:
        """Models this key can call generateContent on (asked once per identify). Never raises."""
        if self._names is not None:
            return self._names
        self._names = []
        if not self.key:
            return self._names
        try:
            r = self.http.get(f"{API}/models", headers={"x-goog-api-key": self.key}, params={"pageSize": 200},
                              timeout=min(10, max(1, self._left() - 2)))
            if r.status_code == 200:
                self._names = [m["name"].split("/", 1)[-1] for m in r.json().get("models", [])
                               if "generateContent" in m.get("supportedGenerationMethods", [])]
        except Exception:  # noqa: BLE001
            pass
        return self._names

    @staticmethod
    def _rank(n: str) -> tuple:
        """Order to try models in: Flash (newest first), Flash-Lite, Pro, then Gemma (newest, largest;
        slow but with the biggest free allowance), then anything else."""
        v = re.search(r"(?:gemini|gemma)-(\d+(?:\.\d+)?)", n)
        ver = float(v.group(1)) if v else 0.0
        size = re.search(r"(\d+)b\b", n.replace("-", " "))
        b = int(size.group(1)) if size else 0
        fam = (3 if n.startswith("gemma") else 1 if "lite" in n else 0 if "flash" in n else 2 if "pro" in n else 4)
        return (fam, -ver, -b, n)

    def _gemma_ok(self, n: str) -> bool:
        size = re.search(r"(\d+)b\b", n.replace("-", " "))
        return n.startswith("gemma") and bool(size) and int(size.group(1)) >= MIN_GEMMA_B

    def gemma_model(self) -> str | None:
        """Best Gemma (Google's open models: far bigger free allowance than Flash, 25 Oct).
        GEMMA_MODEL pins one ("" turns Gemma off); otherwise the newest, largest this key can use."""
        env = os.getenv("GEMMA_MODEL")
        if env is not None:
            return env.strip() or None
        if os.getenv("GEMINI_FALLBACK_MODELS") is not None:
            return None
        gs = sorted((n for n in self._list_models() if self._gemma_ok(n) and not any(k in n for k in SKIP_IN_FALLBACK)),
                    key=self._rank)
        return gs[0] if gs else None

    def fallback_models(self) -> list[str]:
        """Every other model this key can use for the job, best first (25 Oct: "try every model that
        has allowance left"). Ones out of allowance are skipped later by their cooldown.
        GEMINI_FALLBACK_MODELS (comma-separated) replaces the list; GEMMA_MODEL pins or turns off Gemma."""
        if not self.key:
            return []
        env = os.getenv("GEMINI_FALLBACK_MODELS")
        if env is not None:
            out = [m.strip() for m in env.split(",") if m.strip() and m.strip() != self.model]
        else:
            out = [n for n in self._list_models() if n != self.model and not any(k in n for k in SKIP_IN_FALLBACK)
                   and (n.startswith("gemini") or self._gemma_ok(n))]
            pinned = os.getenv("GEMMA_MODEL")
            if pinned is not None:  # a pinned Gemma replaces the discovered ones; "" means none
                out = [n for n in out if not n.startswith("gemma")]
            out = sorted(dict.fromkeys(out), key=self._rank)
        g = os.getenv("GEMMA_MODEL", "").strip() or (self.gemma_model() if env is not None else None)
        return out + ([g] if g and g not in out and g != self.model else [])

    def _left(self) -> float:
        return self._deadline - time.monotonic() if self._deadline else self.budget_s

    def _start(self):
        self._deadline = time.monotonic() + self.budget_s

    def _call(self, parts: list[dict], use_schema: bool = True, first: str = "gemini") -> dict:
        """Try models in turn until one gives a usable answer -> cleaned item dict.
        Order: the main Gemini model (or Claude / Gemma first if asked), then Claude, then other
        Flash models, then Gemma."""
        if self._deadline is None:
            self._start()
        claude = f"claude:{os.getenv('CLAUDE_MODEL', CLAUDE_DEFAULT_MODEL)}" if claude_configured() else None
        main = self.model if self.key else None
        lead = claude if first == "claude" else (self.gemma_model() if first == "gemma" and self.key else None)
        self.lead = lead
        queue = [m for m in dict.fromkeys([lead, main]) if m]
        attempts, tried, partial, gemini_ok, extended = [], [], None, bool(self.key), False
        while True:
            if not queue:
                if extended:
                    break
                extended = True
                queue = [m for m in [claude] + (self.fallback_models() if gemini_ok else []) if m and m not in tried]
                if not queue:
                    break
                continue
            model = queue.pop(0)
            if self._left() < 5:
                attempts.append((model, "out of time"))
                break
            c = cooling(model) if not model.startswith("claude:") else None
            if c:
                attempts.append((model, f"skipped: usage limit reached at {_clock(c['since'])} ({c['why']}); "
                                        f"tried again after {_clock(c['until'])}"))
                continue
            tried.append(model)
            # the first model can't use up the whole budget, so the backups always get a turn.
            # Not Gemma: it's slow but often the only one with allowance left, so it gets the time.
            cap = FIRST_MODEL_CAP_S if len(tried) == 1 and not model.startswith("gemma") else None
            resp, err, kind = self._call_model(model, parts, use_schema, cap)
            if resp is not None:
                try:
                    out = parse_answer(resp)
                    self.used_model = model
                    return out
                except Unusable as u:
                    attempts.append((model, str(u)))
                    partial = partial or u.partial
                    continue
            attempts.append((model, err))
            if kind == "auth" and not model.startswith("claude:"):
                gemini_ok = False  # a refused Gemini key won't work for other Gemini models either
                queue = [m for m in queue if m.startswith("claude:")]
        if partial:  # nobody named the item: keep the best half-answer rather than nothing
            self.used_model = tried[0] if tried else None
            return partial
        log.warning("identify failed: %s", attempts)
        raise IdentifyError("All models failed (" + "; ".join(f"{m}: {r}" for m, r in attempts) + ")", attempts)

    def _call_claude(self, model: str, parts: list[dict], cap: float | None = None) -> tuple[dict | None, str, str]:
        """Same request to Anthropic's Messages API; the answer comes back in Gemini's response
        shape so the rest of the code doesn't care which AI answered."""
        content, wanted = [], 0
        for p in parts:
            if "inline_data" in p:
                wanted += 1
                mime = p["inline_data"]["mime_type"]
                if mime not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                    continue  # (photos are converted to JPEG before this point)
                content.append({"type": "image", "source": {"type": "base64", "media_type": mime,
                                                             "data": p["inline_data"]["data"]}})
        if wanted and not content:
            return None, "couldn't read the photo format", "error"  # don't let it guess from the text alone
        text = " ".join(p["text"] for p in parts if "text" in p) + CLAUDE_JSON_NOTE
        content.append({"type": "text", "text": text})
        left = self._left()
        if left < 5:
            return None, "out of time", "busy"
        timeout = min(REQUEST_TIMEOUT_S, left - 2, cap or REQUEST_TIMEOUT_S)
        try:
            r = self.http.post(CLAUDE_URL, json={"model": model, "max_tokens": 1500,
                                                 "messages": [{"role": "user", "content": content}]},
                               headers={"x-api-key": os.getenv("ANTHROPIC_API_KEY", ""),
                                        "anthropic-version": os.getenv("ANTHROPIC_VERSION", "2023-06-01"),
                                        "content-type": "application/json"},
                               timeout=timeout)
        except httpx.TimeoutException:
            return None, f"no answer within {round(timeout)} s", "busy"
        except httpx.HTTPError as e:
            return None, f"network error ({type(e).__name__})", "busy"
        if r.status_code != 200:
            kind = "busy" if r.status_code in RETRY_STATUS | {529} else "auth" if r.status_code in (401, 403) else "error"
            label = {"busy": "usage limit reached" if r.status_code == 429 else "busy", "auth": "API key refused"}.get(kind, "rejected the request")
            return None, f"{label} (HTTP {r.status_code}: {_google_error(r)})" if kind == "error" else f"{label} (HTTP {r.status_code})", kind
        body = r.json()
        out = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
        if model != self.model:
            log.warning("Gemini %s didn't answer; used Claude %s", self.model, model)
        finish = "MAX_TOKENS" if body.get("stop_reason") == "max_tokens" else "STOP"
        return {"candidates": [{"content": {"parts": [{"text": out}]}, "finishReason": finish}]}, "", "ok"

    def _call_model(self, model: str, parts: list[dict], use_schema: bool,
                    cap: float | None = None) -> tuple[dict | None, str, str]:
        """(response, last error, kind) for one model, with retries and backoff.
        kind: ok | busy (429/5xx/slow/network) | auth (key refused) | error (anything else)."""
        if model.startswith("claude:"):
            return self._call_claude(model.split(":", 1)[1], parts, cap)
        # Gemma (and any model that refused it before) doesn't take the JSON response setting: ask
        # for JSON in the prompt instead, as with Claude.
        json_mode = not model.startswith("gemma") and model not in self._no_json_mode
        cfg = {"temperature": 0.2}
        if json_mode:
            cfg["responseMimeType"] = "application/json"
            if use_schema:
                cfg["responseSchema"] = SCHEMA
        else:
            parts = [dict(p, text=p["text"] + CLAUDE_JSON_NOTE) if i == 0 and "text" in p else p for i, p in enumerate(parts)]
        think = _thinking_config(model) if self.light_thinking else None
        if think:
            # Gemini 3 Flash thinks at "high" by default, which can take a long time before it
            # answers (identify ran past 45 s, 25 Oct); naming a product doesn't need it.
            cfg["thinkingConfig"] = think
        body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": cfg}
        url = f"{API}/models/{model}:generateContent"
        last, kind = "", "error"
        t_end = time.monotonic() + cap if cap else None  # the cap covers retries too
        for attempt in range(len(BACKOFF_S) + 1):
            left = self._left() if not t_end else min(self._left(), t_end - time.monotonic() + 2)
            if left < 5:
                return None, (last + " " if last else "") + ("(out of time)" if not t_end else "(gave up to try the backups)"), "busy"
            timeout = min(GEMMA_TIMEOUT_S if model.startswith("gemma") else REQUEST_TIMEOUT_S, left - 2)
            try:
                r = self.http.post(url, json=body, headers={"x-goog-api-key": self.key}, timeout=timeout)
            except httpx.TimeoutException:
                return None, f"no answer within {round(timeout)} s", "busy"  # slow: try another model
            except httpx.HTTPError as e:
                last, kind = f"network error ({type(e).__name__})", "busy"
            else:
                if r.status_code == 200:
                    if model != self.model:
                        log.warning("Gemini %s didn't answer; used %s", self.model, model)
                    return r.json(), "", "ok"
                if r.status_code == 400 and self.light_thinking and "think" in r.text.lower():
                    self.light_thinking = False  # model without that setting: ask again without it
                    return self._call_model(model, parts, use_schema, cap)
                if r.status_code == 400 and json_mode and re.search(r"json mode|response_?mime_?type", r.text.lower()):
                    self._no_json_mode.add(model)  # model without the JSON setting: ask in the prompt instead
                    return self._call_model(model, parts, use_schema, cap)
                if r.status_code == 400 and json_mode and use_schema and "schema" in r.text.lower():
                    return self._call_model(model, parts, False, cap)  # model without schema support
                if r.status_code == 429:
                    # not "busy": this key has used up its allowance (requests per minute or per day).
                    # Skip this model until the limit resets; retrying now only spends more of it.
                    when = cool_down(model, r)
                    last, kind = f"usage limit reached (HTTP 429: {_google_error(r)}) [{when}]", "busy"
                    break
                elif r.status_code in RETRY_STATUS:
                    last, kind = f"busy (HTTP {r.status_code})", "busy"
                elif r.status_code in (401, 403):
                    return None, f"API key refused (HTTP {r.status_code})", "auth"
                else:
                    return None, f"rejected the request (HTTP {r.status_code}: {_google_error(r)})", "error"
            if attempt < len(BACKOFF_S):
                left = self._left() if not t_end else min(self._left(), t_end - time.monotonic())
                if left - BACKOFF_S[attempt] < 8:
                    break  # not enough time for another go: try the next model instead
                self.sleep(BACKOFF_S[attempt])
        return None, last, kind

    def _parse(self, resp: dict) -> dict:
        """Kept for older callers: one reply -> item, or IdentifyError."""
        try:
            return parse_answer(resp)
        except Unusable as u:
            if u.partial:
                return u.partial
            raise IdentifyError(f"Gemini returned something unexpected: {u}") from u

    def _photo_parts(self, images: list[bytes], hint: str | None, max_side: int) -> list[dict]:
        parts = [{"text": PROMPT + (f"\nThe seller adds: {hint}" if hint else "")}]
        for data in images[:4]:
            small, mime = _shrink(data, max_side=max_side)
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(small).decode()}})
        return parts

    def from_images(self, images: list[bytes], hint: str | None = None) -> dict:
        self._start()
        first = os.getenv("PHOTO_AI_FIRST", "gemini").strip().lower()  # gemini | claude | gemma
        try:
            out = self._call(self._photo_parts(images, hint, GEMINI_MAX_SIDE), first=first)
        except IdentifyError as e:
            # One more round with smaller photos, unless every model was simply busy (smaller photos
            # won't help that) or there isn't time left for it.
            not_busy = any(not r.startswith(("busy", "usage limit", "skipped")) for _, r in e.attempts)
            if not (not_busy and self._left() >= 25 and images):
                raise
            log.warning("identify: retrying with %d px photos after: %s", SMALL_RETRY_SIDE, e.attempts)
            try:
                out = self._call(self._photo_parts(images, hint, SMALL_RETRY_SIDE), first=first)
            except IdentifyError as e2:
                raise IdentifyError(str(e2), e.attempts + [(m, r + " (smaller photos)") for m, r in e2.attempts]) from e2
        out.update(model=self.used_model, fallback=self.used_model not in (self.model, self.lead))
        return out

    def from_text(self, query: str, condition_tier: str | None = None) -> dict:
        self._start()
        extra = f"\nThe seller says the condition is: {condition_tier}." if condition_tier else ""
        out = self._call([{"text": PROMPT + f"\nDescription: {query}{extra}"}])
        out.update(model=self.used_model, fallback=self.used_model != self.model)
        if condition_tier:
            out["item"]["condition_tier"] = condition_tier
        return out
