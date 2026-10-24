"""Tests never touch live marketplaces."""
import os

os.environ["FLIPCURVE_OFFLINE"] = "1"
