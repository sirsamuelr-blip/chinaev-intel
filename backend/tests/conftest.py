"""Shared test fixtures and environment defaults.

``config.settings`` reads required environment variables at import time.
Defaults are set here — conftest is imported before any test module — so
the suite runs without a real ``.env``. Tests never make real API calls.
"""

from __future__ import annotations

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("FIREBASE_PROJECT_ID", "chinaev-intel-test")
