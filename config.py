"""
Centralised configuration -- loads environment variables once and exposes
them as module-level constants used by both main.py and scraper.py.

Required environment variables (set in .env):
    RAG_API_URL  -- Base URL of the RAG backend (e.g. http://host:8000)
    RAG_API_KEY  -- API key sent as X-API-Key header
"""

import os
from dotenv import load_dotenv

# Load .env from the project root (idempotent if already loaded).
load_dotenv()

# ---------------------------------------------------------------------------
# RAG backend connection
# ---------------------------------------------------------------------------
API_URL: str = os.getenv("RAG_API_URL", "").rstrip("/")
API_KEY: str = os.getenv("RAG_API_KEY", "")
HEADERS: dict = {"X-API-Key": API_KEY} if API_KEY else {}

APP_API_KEY: str = os.getenv("APP_API_KEY", "")

# ---------------------------------------------------------------------------
# Upload constraints
# ---------------------------------------------------------------------------
MAX_UPLOAD_BYTES: int = 100 * 1024 * 1024  # 100 MB

# ---------------------------------------------------------------------------
# File extensions that are already plain-text and never need Docling
# conversion (they can be uploaded directly to the RAG backend).
# ---------------------------------------------------------------------------
TEXT_EXTENSIONS: set[str] = {".txt", ".json", ".xml", ".md", ".csv"}

DEBUG_UPLOAD_DIR: str = os.getenv("DEBUG_UPLOAD_DIR", "")
