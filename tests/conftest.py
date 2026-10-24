"""Tests never touch live services: no marketplaces, no Gemini.
load_dotenv() never overrides variables that are already set, so these win over .env."""
import os

os.environ["FLIPCURVE_OFFLINE"] = "1"
os.environ["GEMINI_API_KEY"] = ""
os.environ["EBAY_CLIENT_ID"] = ""
os.environ["EBAY_CLIENT_SECRET"] = ""
