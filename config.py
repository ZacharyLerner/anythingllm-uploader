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

DEBUG_UPLOAD_DIR: str = os.getenv("DEBUG_UPLOAD_DIR", "")
