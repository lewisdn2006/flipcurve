"""Tests never touch live services: no marketplaces, no Gemini.
load_dotenv() never overrides variables that are already set, so these win over .env."""
import os

os.environ["FLIPCURVE_OFFLINE"] = "1"
os.environ["GEMINI_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["FLIPCURVE_COOLDOWN_FILE"] = ""   # no cooldowns written to disk
os.environ["PHOTO_AI_FIRST"] = "gemini"   # set, not removed: .env (PHOTO_AI_FIRST=gemma) would fill it back in
os.environ["EBAY_CLIENT_ID"] = ""
os.environ["EBAY_CLIENT_SECRET"] = ""


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_identify_cooldowns():
    """Models out of allowance are remembered per server process; each test starts clean."""
    from backend.vision import gemini_identifier
    gemini_identifier.COOLDOWN.clear()
    yield
    gemini_identifier.COOLDOWN.clear()
