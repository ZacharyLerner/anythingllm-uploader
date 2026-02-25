"""
Centralised configuration -- loads environment variables once and exposes
them as module-level constants used by both app.py and scraper.py.

Required environment variables (set in .env):
    AnythingLLM_API_URL  -- Base URL of the AnythingLLM API (e.g. http://host:3001/api/v1)
    AnythingLLM_API_Key  -- Bearer token for authenticating with the API
"""

import os
from dotenv import load_dotenv

# Load .env from the project root (idempotent if already loaded).
load_dotenv()

# ---------------------------------------------------------------------------
# AnythingLLM connection
# ---------------------------------------------------------------------------
API_URL: str = os.getenv("AnythingLLM_API_URL", "")
API_KEY: str = os.getenv("AnythingLLM_API_Key", "")
HEADERS: dict = {"Authorization": f"Bearer {API_KEY}"}

# ---------------------------------------------------------------------------
# Application API key -- when set, external callers must include this as a
# Bearer token in the Authorization header.  Leave empty to disable auth
# (backwards-compatible).
# ---------------------------------------------------------------------------
APP_API_KEY: str = os.getenv("APP_API_KEY", "")

# ---------------------------------------------------------------------------
# Upload constraints
# ---------------------------------------------------------------------------
MAX_UPLOAD_BYTES: int = 100 * 1024 * 1024  # 100 MB

# ---------------------------------------------------------------------------
# File extensions that are already plain-text and never need Docling
# conversion (they can be uploaded directly to AnythingLLM).
# ---------------------------------------------------------------------------
TEXT_EXTENSIONS: set[str] = {".txt", ".json", ".xml", ".md", ".csv"}

# ---------------------------------------------------------------------------
# Scraper tuning constants (used by scraper.py)
# ---------------------------------------------------------------------------
REQUEST_DELAY: float = 1.5  # seconds between requests to same domain
REQUEST_TIMEOUT: int = 30  # seconds per outbound HTTP request
MAX_PAGES_PER_JOB: int = 500  # safety cap on pages crawled in one job
SCRAPER_USER_AGENT: str = "KnowledgeBaseScraper/1.0"
JOB_POLL_INTERVAL: int = 10  # seconds between job-queue polls

# ---------------------------------------------------------------------------
# Wake-up signal file -- written by the Flask app when a manual "Scrape Now"
# is triggered so the scraper worker breaks out of its sleep loop immediately.
# ---------------------------------------------------------------------------
WAKE_SIGNAL_PATH: str = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".scraper_wake"
)
