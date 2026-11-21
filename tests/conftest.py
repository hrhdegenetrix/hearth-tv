"""Lets `python -m pytest tests -q` (from the repo root) import the `hearth` package."""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
