"""Verify the Gemini key in .env works (and the eBay API / SerpAPI keys if set). Never prints the keys.

Run from the repo root:  python scripts/check_keys.py
If the configured model is unavailable, it lists the Flash models your key can use.
"""
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
API = "https://generativelanguage.googleapis.com/v1beta"


def list_models(key: str) -> list[str]:
    r = httpx.get(f"{API}/models", headers={"x-goog-api-key": key}, params={"pageSize": 200}, timeout=15)
    if r.status_code != 200:
        return []
    return [m["name"].split("/", 1)[-1] for m in r.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])]


def check_gemini() -> bool:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
    if not key:
        print("GEMINI: missing GEMINI_API_KEY in .env")
        return False
    body = {
        "contents": [{"parts": [{"text": 'Reply with JSON {"ok": true} and nothing else.'}]}],
        "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 20},
    }
    try:
        r = httpx.post(f"{API}/models/{model}:generateContent", json=body,
                       headers={"x-goog-api-key": key}, timeout=90)
    except httpx.HTTPError as e:
        print(f"GEMINI: network error ({type(e).__name__}: {e})")
        return False
    if r.status_code == 200:
        print(f"GEMINI: OK ({model})")
        return True
    try:
        msg = r.json().get("error", {}).get("message", r.text[:200])
    except ValueError:
        msg = r.text[:200]
    print(f"GEMINI: HTTP {r.status_code} with model '{model}' - {msg}")
    if r.status_code in (429, 503):
        flash = sorted([m for m in list_models(key) if "flash" in m and m != model
                        and not any(k in m for k in ("image", "tts", "audio", "live", "embedding"))], reverse=True)[:2]
        print("  (busy, not broken) when it's busy the app tries these instead:", ", ".join(flash) or "none found")
    if r.status_code in (400, 404):
        flash = [m for m in list_models(key) if "flash" in m and "image" not in m and "tts" not in m]
        if flash:
            print("  Flash models this key can use:", ", ".join(sorted(flash)))
            print("  -> put one in .env as GEMINI_MODEL=<name> and run this again")
    return False


def check_serpapi() -> bool:
    key = os.getenv("SERPAPI_KEY", "").strip()
    if not key:
        print("SERPAPI: not set (optional, skipped)")
        return True
    r = httpx.get("https://serpapi.com/account.json", params={"api_key": key}, timeout=10)
    print("SERPAPI: OK" if r.status_code == 200 else f"SERPAPI: HTTP {r.status_code}")
    return r.status_code == 200


def check_ebay() -> bool:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from backend.scrapers import ebay_api
    if not ebay_api.configured():
        print("EBAY API: not set (optional; add EBAY_CLIENT_ID and EBAY_CLIENT_SECRET to .env)")
        return True
    try:
        api = ebay_api.BrowseAPI()
        api.token()
        j = api.search("nike air force 1", limit=5)
    except httpx.HTTPStatusError as e:
        body = e.response.text[:200].replace(os.getenv("EBAY_CLIENT_SECRET", "") or "\0", "***")
        print(f"EBAY API: HTTP {e.response.status_code} - {body}")
        if e.response.status_code == 401:
            print("  -> check EBAY_CLIENT_ID is the App ID and EBAY_CLIENT_SECRET the Cert ID of the Production keyset")
        return False
    except httpx.HTTPError as e:
        print(f"EBAY API: network error ({type(e).__name__})")
        return False
    items = j.get("itemSummaries", [])
    dated = sum(1 for it in items if it.get("itemCreationDate"))
    print(f"EBAY API: OK ({j.get('total', len(items))} UK listings for a test search; {dated}/{len(items)} with listing dates)")
    return True


if __name__ == "__main__":
    ok = check_gemini() & check_ebay() & check_serpapi()
    sys.exit(0 if ok else 1)
