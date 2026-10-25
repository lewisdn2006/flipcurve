"""Try the Gemini identifier live.

    python scripts/identify_smoke.py "sony wh-1000xm4 black headphones"
    python scripts/identify_smoke.py --image photo.jpg [--image label.jpg]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from backend.vision.gemini_identifier import GeminiIdentifier, IdentifyError  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?")
    ap.add_argument("--image", action="append", default=[])
    a = ap.parse_args()
    t = time.time()
    try:
        g = GeminiIdentifier()
        out = g.from_images([Path(p).read_bytes() for p in a.image], hint=a.text) if a.image else g.from_text(a.text or "")
    except IdentifyError as e:
        print("FAILED:", e.reason() if hasattr(e, "reason") else e)
        sys.exit(1)
    print(json.dumps(out, indent=2))
    print(f"({time.time() - t:.1f}s, answered by {g.used_model or g.model})")
