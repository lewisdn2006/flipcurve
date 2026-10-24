"""Verify the Gemini key in .env works (and SerpAPI if set). Never prints the keys.

Run from the repo root:  python scripts/check_keys.py
"""
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def check_gemini() -> bool:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
    if not key:
        print("GEMINI: missing GEMINI_API_KEY in .env")
        return False
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": 'Reply with JSON {"ok": true} and nothing else.'}]}],
        "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 20},
    }
    try:
        r = httpx.post(url, json=body, headers={"x-goog-api-key": key}, timeout=15)
    except httpx.HTTPError as e:
        print(f"GEMINI: network error ({type(e).__name__}: {e})")
        return False
    if r.status_code == 200:
        print(f"GEMINI: OK ({model})")
        return True
    msg = r.json().get("error", {}).get("message", r.text[:200]) if r.headers.get("content-type", "").startswith("application/json") else r.text[:200]
    print(f"GEMINI: HTTP {r.status_code} - {msg}")
    return False


def check_serpapi() -> bool:
    key = os.getenv("SERPAPI_KEY", "").strip()
    if not key:
        print("SERPAPI: not set (optional, skipped)")
        return True
    r = httpx.get("https://serpapi.com/account.json", params={"api_key": key}, timeout=10)
    print("SERPAPI: OK" if r.status_code == 200 else f"SERPAPI: HTTP {r.status_code}")
    return r.status_code == 200


if __name__ == "__main__":
    ok = check_gemini() & check_serpapi()
    sys.exit(0 if ok else 1)
