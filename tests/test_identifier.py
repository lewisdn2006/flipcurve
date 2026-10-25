"""Gemini identifier against canned API responses (no network)."""
import json

import httpx
import pytest

from backend.models.schemas import ItemSpec
from backend.vision.gemini_identifier import GeminiIdentifier, IdentifyError

GOOD = {"title": "Nike Air Force 1 '07 Low White", "brand": "Nike", "model_name": "Air Force 1 '07",
        "category": "Trainers", "size_or_storage": "UK 9", "era_or_year": None, "condition_tier": "good",
        "condition_score": 7.5, "detected_flaws": ["toe box creasing"],
        "search_queries": {"ebay": "Nike Air Force 1 07 White", "vinted": "air force 1 white"},
        "is_broken": False, "confidence": 0.9}


def reply(obj):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}}]})


def make(handler):
    return GeminiIdentifier(api_key="k", model="m", client=httpx.Client(transport=httpx.MockTransport(handler)),
                            sleep=lambda s: None)


def test_text_identify_returns_valid_itemspec():
    out = make(lambda req: reply(GOOD)).from_text("air force 1 white size 9")
    ItemSpec.model_validate(out["item"])
    assert out["item"]["search_queries"]["ebay"] == "nike air force 1 07 white" and out["confidence"] == 0.9


def test_image_identify_sends_inline_image():
    seen = {}

    def handler(req):
        body = json.loads(req.content)
        seen["parts"] = body["contents"][0]["parts"]
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        return reply(GOOD)

    make(handler).from_images([b"not-really-a-jpeg"], hint="size 9")
    assert "inline_data" in seen["parts"][1] and "size 9" in seen["parts"][0]["text"]


def test_retries_on_busy_then_succeeds():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(503, text="high demand") if calls["n"] < 3 else reply(GOOD)

    make(handler).from_text("air force 1")
    assert calls["n"] == 3


def test_gives_up_with_clear_error():
    with pytest.raises(IdentifyError):
        make(lambda req: httpx.Response(503, text="high demand")).from_text("air force 1")


def test_messy_output_is_cleaned():
    messy = {"title": "", "condition_tier": "excellent", "condition_score": 14, "detected_flaws": None,
             "search_queries": {}, "confidence": 3}
    out = make(lambda req: reply(messy)).from_text("thing")
    ItemSpec.model_validate(out["item"])
    assert out["item"]["condition_tier"] == "good" and out["item"]["condition_score"] == 10 and out["confidence"] == 1


def test_api_falls_back_when_gemini_unset(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.main import app
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    r = TestClient(app).post("/api/v1/identify-text", json={"query": "Sony WH-1000XM4 Black"})
    assert r.status_code == 200 and r.json()["item"]["search_queries"]["ebay"] == "sony wh-1000xm4 black"
    assert r.json()["warnings"]


def test_switches_to_another_flash_model_when_busy(monkeypatch):
    monkeypatch.delenv("GEMINI_FALLBACK_MODELS", raising=False)
    tried = []

    def handler(req):
        if req.method == "GET":  # the models list
            return httpx.Response(200, json={"models": [
                {"name": "models/m", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-9-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-9-flash-image", "supportedGenerationMethods": ["generateContent"]}]})
        model = req.url.path.split("/models/")[1].split(":")[0]
        tried.append(model)
        return httpx.Response(503, text="high demand") if model == "m" else reply(GOOD)

    out = make(handler).from_text("air force 1")
    assert out["item"]["brand"] == "Nike"
    assert tried[-1] == "gemini-9-flash" and "gemini-9-flash-image" not in tried


def test_bad_key_does_not_try_other_models(monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "other")
    tried = []

    def handler(req):
        tried.append(req.url.path)
        return httpx.Response(403, text="API key not valid")

    with pytest.raises(IdentifyError):
        make(handler).from_text("x")
    assert all("/models/m:" in t for t in tried)


def test_heic_photos_are_converted_to_small_jpegs():
    import io
    import pillow_heif
    from PIL import Image
    from backend.vision.gemini_identifier import _shrink
    pillow_heif.register_heif_opener()
    buf = io.BytesIO()
    Image.new("RGB", (4032, 3024), (200, 200, 200)).save(buf, "HEIF")
    out, mime = _shrink(buf.getvalue())
    assert mime == "image/jpeg" and max(Image.open(io.BytesIO(out)).size) == 1600


def test_gives_up_inside_the_time_budget(monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    clock = {"t": 0.0}
    monkeypatch.setattr("backend.vision.gemini_identifier.time.monotonic", lambda: clock["t"])

    def handler(req):
        clock["t"] += 30  # every call is slow
        return httpx.Response(503, text="high demand")

    ident = GeminiIdentifier(api_key="k", model="m", client=httpx.Client(transport=httpx.MockTransport(handler)),
                             sleep=lambda s: clock.__setitem__("t", clock["t"] + s), budget_s=70)
    with pytest.raises(IdentifyError):
        ident.from_text("x")
    assert clock["t"] <= 75
