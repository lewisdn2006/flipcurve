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


def test_claude_steps_in_when_gemini_is_busy(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "gemini-other-flash")
    order = []

    def handler(req):
        if req.url.host == "api.anthropic.com":
            order.append("claude")
            body = json.loads(req.content)
            kinds = [b["type"] for b in body["messages"][0]["content"]]
            assert kinds == ["image", "text"] and body["model"] == "claude-haiku-5-5"
            assert req.headers["x-api-key"] == "test-key"
            return httpx.Response(200, json={"type": "message", "content": [
                {"type": "text", "text": "Here it is: " + json.dumps(GOOD)}]})
        order.append(req.url.path.split("/models/")[1].split(":")[0])
        return httpx.Response(503, text="high demand")

    out = make(handler).from_images([b"not-really-a-jpeg"])
    assert out["item"]["brand"] == "Nike" and out["model"] == "claude:claude-haiku-5-5" and out["fallback"]
    assert order[0] == "m" and order[-1] == "claude" and "gemini-other-flash" not in order


def test_slow_gemini_moves_on_instead_of_waiting_again(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "gemini-other-flash")
    tried = []

    def handler(req):
        model = req.url.path.split("/models/")[1].split(":")[0]
        tried.append(model)
        if model == "m":
            raise httpx.ReadTimeout("slow", request=req)
        return reply(GOOD)

    out = make(handler).from_text("air force 1")
    assert tried == ["m", "gemini-other-flash"] and out["item"]["brand"] == "Nike"


def test_asks_for_low_thinking_and_drops_it_if_refused():
    from backend.vision.gemini_identifier import _thinking_config
    assert _thinking_config("gemini-3.8-flash") == {"thinkingLevel": "low"}
    assert _thinking_config("gemini-2.5-flash") == {"thinkingBudget": 0}
    seen = []

    def handler(req):
        cfg = json.loads(req.content)["generationConfig"]
        seen.append("thinkingConfig" in cfg)
        if "thinkingConfig" in cfg:
            return httpx.Response(400, text="Unknown field thinkingConfig")
        return reply(GOOD)

    GeminiIdentifier(api_key="k", model="gemini-3.8-flash", client=httpx.Client(transport=httpx.MockTransport(handler)),
                     sleep=lambda s: None).from_text("x")
    assert seen == [True, False]


def test_identify_jobs_start_and_finish():
    import time as t
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as c:
        jid = c.post("/api/v1/identify/start", files=[("images", ("a.jpg", b"xx", "image/jpeg"))]).json()["job"]
        for _ in range(50):
            j = c.get(f"/api/v1/identify/job/{jid}").json()
            if j["status"] == "done":
                break
            t.sleep(0.1)
        assert j["status"] == "done" and j["result"]["item"]["title"] == ""   # no key in tests -> blank form
        tj = c.post("/api/v1/identify-text/start", json={"query": "nike air force 1"}).json()["job"]
        for _ in range(50):
            j = c.get(f"/api/v1/identify/job/{tj}").json()
            if j["status"] == "done":
                break
            t.sleep(0.1)
        assert j["result"]["item"]["search_queries"]["ebay"] == "nike air force 1"
        assert c.get("/api/v1/identify/job/nope").status_code == 404


# ---- 25 Oct: photo identify failed often. Each way it used to give up early, and what it does now.

def _model_of(req):
    return "claude" if req.url.host == "api.anthropic.com" else req.url.path.split("/models/")[1].split(":")[0]


def _claude_ok(obj=GOOD):
    return httpx.Response(200, json={"type": "message", "stop_reason": "end_turn",
                                     "content": [{"type": "text", "text": json.dumps(obj)}]})


def _jpeg(side=2000):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (side, side * 3 // 4), (120, 90, 60)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.mark.parametrize("bad", [
    httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"title": "Nike Air'}]}, "finishReason": "MAX_TOKENS"}]}),
    httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY"}]}),
    httpx.Response(200, json={"promptFeedback": {"blockReason": "OTHER"}}),
    httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "I can't tell what this is."}]}}]}),
    httpx.Response(200, json={"candidates": []}),
])
def test_unusable_answer_moves_on_to_the_backup(monkeypatch, bad):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    order = []

    def handler(req):
        order.append(_model_of(req))
        return _claude_ok() if order[-1] == "claude" else bad

    out = make(handler).from_images([_jpeg(400)])
    assert order == ["m", "claude"] and out["item"]["brand"] == "Nike" and out["fallback"]


def test_odd_values_are_read_instead_of_crashing():
    odd = {"title": "Levi's 501 Jeans", "condition_tier": "Very Good", "condition_score": "7/10",
           "detected_flaws": "faded knees; small mark on pocket", "search_queries": "levis 501",
           "must_have": "501", "is_broken": "false", "confidence": "85%", "brand": ["Levi's"]}
    out = make(lambda req: reply(odd)).from_text("levis")
    ItemSpec.model_validate(out["item"])
    it = out["item"]
    assert it["condition_tier"] == "very_good" and it["condition_score"] == 7 and out["confidence"] == 0.85
    assert it["detected_flaws"] == ["faded knees", "small mark on pocket"] and it["search_queries"]["vinted"] == "levis 501"
    assert it["must_have"] == ["501"] and it["is_broken"] is False and it["brand"] == "Levi's"
    for conf, want in [("high", 0.85), ("8/10", 0.8), (None, None)]:
        assert make(lambda req, c=conf: reply({**GOOD, "confidence": c})).from_text("x")["confidence"] == want


def test_first_model_cannot_use_the_whole_budget(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    timeouts = {}

    def handler(req):
        m = _model_of(req)
        timeouts[m] = req.extensions["timeout"]["read"]
        if m == "m":
            raise httpx.ReadTimeout("slow", request=req)
        return _claude_ok()

    ident = GeminiIdentifier(api_key="k", model="m", client=httpx.Client(transport=httpx.MockTransport(handler)),
                             sleep=lambda s: None, budget_s=170)
    out = ident.from_images([_jpeg(400)])
    assert timeouts["m"] <= 40 and timeouts["claude"] > 40 and out["item"]["brand"] == "Nike"


def test_other_request_errors_still_try_the_backup(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    order = []

    def handler(req):
        order.append(_model_of(req))
        if order[-1] == "claude":
            return _claude_ok()
        return httpx.Response(400, json={"error": {"message": "Unable to process input image."}})

    assert make(handler).from_images([_jpeg(400)])["item"]["brand"] == "Nike" and order == ["m", "claude"]


def test_refused_gemini_key_skips_other_geminis_but_tries_claude(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "other")
    order = []

    def handler(req):
        order.append(_model_of(req))
        return _claude_ok() if order[-1] == "claude" else httpx.Response(403, text="API key not valid")

    make(handler).from_text("x")
    assert order == ["m", "claude"]


def test_retries_with_smaller_photos_when_the_first_round_failed(monkeypatch):
    import base64
    import io
    from PIL import Image
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    sides = []

    def handler(req):
        part = json.loads(req.content)["contents"][0]["parts"][1]["inline_data"]["data"]
        sides.append(max(Image.open(io.BytesIO(base64.b64decode(part))).size))
        return httpx.Response(400, json={"error": {"message": "Request payload size exceeds the limit"}}) \
            if len(sides) == 1 else reply(GOOD)

    out = GeminiIdentifier(api_key="k", model="m", client=httpx.Client(transport=httpx.MockTransport(handler)),
                           sleep=lambda s: None, budget_s=170).from_images([_jpeg(2000)])
    assert sides == [1280, 800] and out["item"]["brand"] == "Nike"


def test_no_smaller_photo_retry_when_everything_was_just_busy(monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503, text="high demand")

    with pytest.raises(IdentifyError) as e:
        GeminiIdentifier(api_key="k", model="m", client=httpx.Client(transport=httpx.MockTransport(handler)),
                         sleep=lambda s: None, budget_s=170).from_images([_jpeg(400)])
    assert len(calls) == 4  # one model, its retries, no second round
    assert "m: busy (HTTP 503)" in e.value.reason() and "ANTHROPIC_API_KEY" in e.value.reason()


def test_claude_alone_works_without_a_gemini_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    seen = []
    ident = GeminiIdentifier(api_key="", model="m", sleep=lambda s: None,
                             client=httpx.Client(transport=httpx.MockTransport(lambda r: (seen.append(_model_of(r)), _claude_ok())[1])))
    assert ident.from_images([_jpeg(400)])["item"]["brand"] == "Nike" and seen == ["claude"]


def test_photo_ai_first_can_put_claude_first(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("PHOTO_AI_FIRST", "claude")
    order = []

    def handler(req):
        order.append(_model_of(req))
        return _claude_ok()

    make(handler).from_images([_jpeg(400)])
    assert order == ["claude"]


def test_claude_is_not_asked_to_guess_without_the_photo(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("PHOTO_AI_FIRST", "claude")
    monkeypatch.setattr("backend.vision.gemini_identifier._shrink", lambda data, max_side=0: (data, "image/heic"))
    order = []

    def handler(req):
        order.append(_model_of(req))
        return reply(GOOD)

    out = make(handler).from_images([b"heic-bytes"])
    assert order == ["m"] and out["item"]["brand"] == "Nike"


def test_failed_photo_identify_tells_the_user_why(monkeypatch):
    from fastapi.testclient import TestClient
    import backend.main as main
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    real = main.GeminiIdentifier

    def fake(**kw):
        return real(api_key="k", model="gemini-test", sleep=lambda s: None, budget_s=kw.get("budget_s", 70),
                    client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503, text="busy"))))
    monkeypatch.setattr(main, "GeminiIdentifier", fake)
    r = TestClient(main.app).post("/api/v1/identify", files=[("images", ("a.jpg", _jpeg(300), "image/jpeg"))])
    w = r.json()["warnings"][0]
    assert r.status_code == 200 and "gemini-test: busy (HTTP 503)" in w and "ANTHROPIC_API_KEY" in w


def test_quota_429_is_called_a_usage_limit_not_busy(monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    msg = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                     "message": "You exceeded your current quota. Quota exceeded for metric: generate_content_free_tier_requests"}}
    with pytest.raises(IdentifyError) as e:
        GeminiIdentifier(api_key="k", model="m", sleep=lambda s: None, budget_s=170,
                         client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, json=msg)))).from_images([_jpeg(400)])
    why = e.value.reason()
    assert "usage limit reached (HTTP 429: You exceeded your current quota" in why and "busy" not in why


def test_429_is_not_retried_on_the_same_model(monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "other")
    order = []

    def handler(req):
        order.append(_model_of(req))
        return httpx.Response(429, json={"error": {"message": "quota"}}) if order[-1] == "m" else reply(GOOD)

    assert make(handler).from_text("x")["item"]["brand"] == "Nike" and order == ["m", "other"]



# ---- Gemma: Google's open models, with a far bigger free allowance than Flash (25 Oct)

MODELS = {"models": [{"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"]} for n in
                     ["m", "gemini-9-flash", "gemma-3-27b-it", "gemma-4-26b-a4b-it", "gemma-4-31b-it", "gemma-4-31b-it-image"]]}


def test_gemma_is_found_and_tried_after_flash(monkeypatch):
    monkeypatch.delenv("GEMINI_FALLBACK_MODELS", raising=False)
    monkeypatch.delenv("GEMMA_MODEL", raising=False)
    order, gets = [], []

    def handler(req):
        if req.method == "GET":
            gets.append(1)
            return httpx.Response(200, json=MODELS)
        order.append(_model_of(req))
        if order[-1].startswith("gemma"):
            return reply(GOOD)
        return httpx.Response(429, json={"error": {"message": "quota"}})

    out = make(handler).from_images([_jpeg(400)])
    assert order == ["m", "gemini-9-flash", "gemma-4-31b-it"] and out["model"] == "gemma-4-31b-it"
    assert len(gets) == 1  # the models list is fetched once


def test_gemma_gets_json_asked_for_in_the_prompt(monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    monkeypatch.setenv("GEMMA_MODEL", "gemma-4-31b-it")
    monkeypatch.setenv("PHOTO_AI_FIRST", "gemma")
    seen = {}

    def handler(req):
        body = json.loads(req.content)
        seen["model"] = _model_of(req)
        seen["cfg"] = body["generationConfig"]
        seen["text"] = body["contents"][0]["parts"][0]["text"]
        seen["image"] = "inline_data" in body["contents"][0]["parts"][1]
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": "```json\n" + json.dumps(GOOD) + "\n```"}]}}]})

    out = make(handler).from_images([_jpeg(400)])
    assert seen["model"] == "gemma-4-31b-it" and seen["image"] and out["item"]["brand"] == "Nike"
    assert "responseMimeType" not in seen["cfg"] and "responseSchema" not in seen["cfg"] and "thinkingConfig" not in seen["cfg"]
    assert "Reply with only one JSON object" in seen["text"]


def test_model_that_refuses_json_mode_is_asked_in_the_prompt(monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    cfgs = []

    def handler(req):
        cfg = json.loads(req.content)["generationConfig"]
        cfgs.append("responseMimeType" in cfg)
        if "responseMimeType" in cfg:
            return httpx.Response(400, json={"error": {"message": "JSON mode is not enabled for models/m"}})
        return reply(GOOD)

    assert make(handler).from_text("x")["item"]["brand"] == "Nike" and cfgs == [True, False]


def test_gemma_counts_as_a_backup_in_the_message(monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    monkeypatch.setenv("GEMMA_MODEL", "gemma-4-31b-it")
    with pytest.raises(IdentifyError) as e:
        make(lambda req: httpx.Response(429, json={"error": {"message": "quota"}})).from_text("x")
    why = e.value.reason()
    assert "gemma-4-31b-it: usage limit reached" in why and "no backup AI" not in why


# ---- 25 Oct: skip models that are out of allowance; give Gemma time

def _quota_429(qid="GenerateRequestsPerMinutePerProjectPerModel-FreeTier", delay="37s"):
    return httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
        "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits.",
        "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                     "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                                     "quotaId": qid, "quotaValue": "20"}]},
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]}})


def test_model_out_of_allowance_is_skipped_on_the_next_identify(monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    monkeypatch.setenv("GEMMA_MODEL", "gemma-4-31b-it")
    calls = []

    def handler(req):
        calls.append(_model_of(req))
        return _quota_429() if calls[-1] == "m" else reply(GOOD)

    make(handler).from_text("x")
    make(handler).from_text("x")
    assert calls == ["m", "gemma-4-31b-it", "gemma-4-31b-it"]  # the second identify doesn't ask "m" again


def test_skipped_model_comes_back_after_its_limit_resets(monkeypatch):
    from backend.vision import gemini_identifier as gi
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(gi.time, "time", lambda: now["t"])
    gi.cool_down("m", _quota_429(delay="30s"))
    assert gi.cooling("m")
    now["t"] += 31
    assert gi.cooling("m") is None


def test_daily_limit_waits_until_pacific_midnight():
    from backend.vision import gemini_identifier as gi
    t = datetime_ts = 1_761_400_000.0  # 25 Oct 2026, mid-afternoon in London
    gi.cool_down("m", _quota_429(qid="GenerateRequestsPerDayPerProjectPerModel-FreeTier"), now=t)
    until = gi.COOLDOWN["m"]["until"]
    from datetime import datetime
    from zoneinfo import ZoneInfo
    pt = datetime.fromtimestamp(until, ZoneInfo("America/Los_Angeles"))
    assert until > datetime_ts and pt.hour == 0 and until - t < 26 * 3600


def test_warning_keeps_googles_full_message_and_says_when_it_tries_again(monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    monkeypatch.setenv("GEMMA_MODEL", "")
    with pytest.raises(IdentifyError) as e:
        make(lambda req: _quota_429()).from_text("x")
    why = e.value.reason()
    assert "head to: https://ai.google.dev/gemini-api/docs/rate-limits." in why
    assert "per-minute limit: GenerateRequestsPerMinutePerProjectPerModel-FreeTier (limit 20)" in why and "skipped until" in why
    with pytest.raises(IdentifyError) as e2:
        make(lambda req: _quota_429()).from_text("x")
    assert "m: skipped: usage limit reached at" in e2.value.reason()


def test_gemma_gets_its_long_timeout_even_when_first(monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    monkeypatch.setenv("GEMMA_MODEL", "gemma-4-31b-it")
    monkeypatch.setenv("PHOTO_AI_FIRST", "gemma")
    seen = {}

    def handler(req):
        seen[_model_of(req)] = req.extensions["timeout"]["read"]
        return reply(GOOD)

    GeminiIdentifier(api_key="k", model="m", sleep=lambda s: None, budget_s=180,
                     client=httpx.Client(transport=httpx.MockTransport(handler))).from_images([_jpeg(400)])
    assert seen["gemma-4-31b-it"] >= 150


def test_slow_gemma_after_skipped_flash_still_fits_three_minutes(monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    monkeypatch.setenv("GEMMA_MODEL", "gemma-4-31b-it")
    clock = {"t": 0.0}
    monkeypatch.setattr("backend.vision.gemini_identifier.time.monotonic", lambda: clock["t"])

    def handler(req):
        if _model_of(req) == "m":
            return _quota_429()
        clock["t"] += req.extensions["timeout"]["read"]  # Gemma uses its whole wait
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(IdentifyError) as e:
        GeminiIdentifier(api_key="k", model="m", sleep=lambda s: None, budget_s=180,
                         client=httpx.Client(transport=httpx.MockTransport(handler))).from_images([_jpeg(400)])
    assert clock["t"] <= 180 and "gemma-4-31b-it: no answer within 160 s" in e.value.reason()
