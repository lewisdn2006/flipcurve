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
