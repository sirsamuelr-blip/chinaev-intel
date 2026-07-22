from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
FIREBASE_PROJECT_ID = os.environ["FIREBASE_PROJECT_ID"]
GOOGLE_APPLICATION_CREDENTIALS = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS", "./service-account.json"
)

SONNET_MODEL = "claude-sonnet-4-6"
OPUS_MODEL = "claude-opus-4-6"
HAIKU_MODEL = "claude-haiku-4-5-20251001"

SCRAPE_DELAY_MIN = 5.0
SCRAPE_DELAY_MAX = 10.0
MAX_RETRIES = 3
MAX_ARTICLES_PER_SOURCE = 25
RELEVANCE_THRESHOLD = 4
