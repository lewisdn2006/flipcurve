"""Reference outputs from the Python backend, for checking the JavaScript Worker port
(NO_LAPTOP_PLAN.md, step 0). Every number the port produces is compared against these files.

Run:  .venv/bin/python scripts/make_golden.py

Writes tests/golden/:
  maths.json              scipy's gammaln, digamma and regularised gammainc on a grid
  engine/<case>.json.gz   pipeline.run on saved listings from cache_seed/: the input (item, rows,
                          now), every survival-model fit it made, and the full response
  pages/<name>.json.gz    for each capture in tests/fixtures/pages/ (FLIPCURVE_SAVE_PAGES on the
                          server): the rows ingest made from the pages, the fits and the response

Captures run in a throwaway copy of backend/ with empty caches and calibration, so the real
files aren't touched and the result depends only on the capture. The captures themselves stay
out of git (signed-in eBay pages can show your account name); the outputs here are listing data.
"""
from __future__ import annotations

import gzip
import json
import math
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "golden"
CAPTURES = ROOT / "tests" / "fixtures" / "pages"
CACHE = ROOT / "backend" / "scrapers" / "cache_seed"

# One case per item that has saved listings on both sites: (case, eBay cache, Vinted cache, title)
ENGINE_CASES = [
    ("jordan-1-low", "jordan-1-low-triple-black_ebay", "jordan-1-low-triple-black_vinted", "Nike Air Jordan 1 Low Triple Black"),
    ("iphone-15-pro", "apple-iphone-15-pro-256gb-black_ebay", "iphone-15-pro-256gb_vinted", "Apple iPhone 15 Pro 256GB Black"),
    ("carrera-bike", "carrera-crossfire-hybrid-bike_ebay", "carrera-crossfire-bike_vinted", "Carrera Crossfire hybrid bike"),
    ("ralph-lauren-tee", "polo-ralph-lauren-crew-neck-t-shirt-white_ebay", "ralph-lauren-white-tshirt_vinted", "Polo Ralph Lauren crew neck T-shirt white"),
    ("macbook-air", "apple-macbook-air-15-m5-black_ebay", "macbook-air-m5-15-inch_vinted", "Apple MacBook Air 15 M5"),
    ("galaxy-book2", "samsung-galaxy-book2-i5-1235u_ebay", "samsung-galaxy-book2_vinted", "Samsung Galaxy Book2 i5-1235U"),
]


def item_for(title: str, ebay_query: str | None, vinted_query: str | None) -> dict:
    return {"title": title, "brand": None, "model_name": None, "category": None, "size_or_storage": None,
            "era_or_year": None, "condition_tier": "good", "condition_score": 7.0, "detected_flaws": [],
            "search_queries": {"ebay": ebay_query, "vinted": vinted_query}, "is_broken": False}


def clean(x):
    """numpy -> plain JSON; NaN/inf -> None (JSON.parse can't read them)."""
    import numpy as np
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if isinstance(x, np.ndarray):
        return clean(x.tolist())
    if isinstance(x, (np.bool_,)):
        return bool(x)
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return float(x) if math.isfinite(x) else None
    return x


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(clean(data), allow_nan=False, separators=(",", ":"))
    if path.suffix == ".gz":
        # mtime=0 so re-running with the same inputs gives byte-identical files
        path.write_bytes(gzip.compress(text.encode(), mtime=0))
    else:
        path.write_text(text)
    print(f"  {path.relative_to(ROOT)}")


def recording_fits():
    """Wrap survival_model.fit so every fit pipeline.run makes is kept."""
    from backend.engine import pipeline
    fits, real = [], pipeline.sm.fit

    def fit(rows, platforms=("ebay", "vinted"), prior=None, anchors=None, sigma_p=None):
        f = real(rows, platforms=platforms, prior=prior, anchors=anchors, sigma_p=sigma_p)
        fits.append({"n_rows": len(rows), "platforms": list(platforms), "anchors_in": anchors,
                     **{k: getattr(f, k) for k in ("psi", "cov", "anchors", "sigma_p", "n_obs", "n_censored",
                                                   "n_interval", "n_exact", "n_sales", "log_likelihood",
                                                   "used_priors", "converged")}})
        return f

    pipeline.sm.fit = fit
    return fits


def latest(rows: list[dict]) -> datetime:
    ts = [r.get(k) for r in rows for k in ("observed_at", "sold_at") if r.get(k)]
    return max(datetime.fromisoformat(str(t).replace("Z", "+00:00")) for t in ts)


def maths() -> None:
    import numpy as np
    from scipy.special import digamma, gammainc, gammaln
    xs = np.round(np.concatenate([np.linspace(0.05, 5, 100), np.linspace(5.5, 60, 40)]), 6)
    # the model uses a = 1/k with k in [0.3, 3], and x = (t/lambda)^k over a wide range
    a_s = np.round(1 / np.linspace(0.3, 3.0, 28), 6)
    x_s = np.round(np.logspace(-6, 3, 46), 9)
    write(OUT / "maths.json", {
        "gammaln": [[x, gammaln(x)] for x in xs],
        "digamma": [[x, digamma(x)] for x in xs],
        "gammainc": [[a, x, gammainc(a, x)] for a in a_s for x in x_s],
    })


def engine_cases() -> None:
    from backend.engine.pipeline import run
    for case, ebay, vinted, title in ENGINE_CASES:
        files = {"ebay": CACHE / f"{ebay}.json", "vinted": CACHE / f"{vinted}.json"}
        if not all(f.exists() for f in files.values()):
            print(f"  skipped {case}: no saved listings")
            continue
        caches = {site: json.loads(f.read_text()) for site, f in files.items()}
        rows = caches["ebay"]["rows"] + caches["vinted"]["rows"]
        item = item_for(title, caches["ebay"]["query"], caches["vinted"]["query"])
        now = latest(rows)
        fits = recording_fits()
        out = run(item, rows, now=now, data_source="seed")
        write(OUT / "engine" / f"{case}.json.gz",
              {"item": item, "now": now.isoformat(), "rows": rows, "fits": fits, "response": out})


def one_capture(path: Path) -> None:
    """Runs inside the throwaway copy of backend/ (see captures())."""
    from backend.engine.pipeline import run
    from backend.scrapers.page_ingest import ingest
    req = json.loads(gzip.decompress(path.read_bytes()))
    if "pages" not in req:
        return  # a recheck-results capture: kept for the step 6 tests, nothing to run here yet
    item, pages = req["item"], req["pages"]
    rows, stats = ingest(pages, item["search_queries"])
    now = max((datetime.fromisoformat(p["fetched_at"].replace("Z", "+00:00")) for p in pages if p.get("fetched_at")),
              default=datetime.now(timezone.utc))
    fits = recording_fits()
    out = run(item, rows, now=now, data_source="live") if rows else None
    write(OUT / "pages" / path.name, {"capture": path.name, "item": item, "now": now.isoformat(), "rows": rows,
                                      "stats": stats, "fits": fits, "response": out})


def captures() -> None:
    files = sorted(CAPTURES.glob("scan-*.json.gz")) if CAPTURES.exists() else []
    if not files:
        print("  no captures yet: run the server with FLIPCURVE_SAVE_PAGES=tests/fixtures/pages and scan a few items")
        return
    for f in files:
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(ROOT / "backend", Path(tmp) / "backend",
                            ignore=shutil.ignore_patterns("__pycache__", "browser_profile", "cache_seed", "*.sqlite",
                                                          "*.json", "server.log"))
            (Path(tmp) / "backend" / "scrapers" / "cache_seed").mkdir()
            shutil.copytree(ROOT / "backend" / "mock", Path(tmp) / "backend" / "mock", dirs_exist_ok=True)
            env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": tmp, "HOME": tmp}  # no .env: no eBay API calls
            subprocess.run([sys.executable, __file__, "--capture", str(f)], cwd=tmp, env=env, check=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--capture"]:
        sys.path.insert(0, ".")
        one_capture(Path(sys.argv[2]))
        sys.exit()
    sys.path.insert(0, str(ROOT))
    print("maths")
    maths()
    print("engine")
    engine_cases()
    print("captures")
    captures()
