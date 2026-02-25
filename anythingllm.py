"""
AnythingLLM API helper -- thin wrapper around the REST calls used by both
the Flask app and the background scraper.

Every function returns a simple (success, detail) or (value, error) tuple
so callers can decide how to surface failures.
"""

import logging
import requests as _requests

from config import API_URL, HEADERS

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Workspace helpers
# ---------------------------------------------------------------------------


def workspace_exists(workspace: str) -> bool:
    """Return True if *workspace* is a valid slug in AnythingLLM."""
    try:
        resp = _requests.get(f"{API_URL}/workspace/{workspace}", headers=HEADERS)
        if resp.status_code != 200:
            return False
        ws = resp.json().get("workspace")
        if isinstance(ws, list):
            return len(ws) > 0 and ws[0].get("id") is not None
        return ws is not None and ws.get("id") is not None
    except Exception:
        log.exception("workspace_exists check failed for %s", workspace)
        return False


# ---------------------------------------------------------------------------
# Document lifecycle
# ---------------------------------------------------------------------------


def upload_document(filename: str, content: bytes, content_type: str):
    """Upload a single file to AnythingLLM's document storage.

    Returns (location_string, None) on success or (None, error_string) on
    failure.
    """
    resp = _requests.post(
        f"{API_URL}/document/upload",
        headers=HEADERS,
        files={"file": (filename, content, content_type)},
    )
    if resp.status_code != 200 or not resp.json().get("documents"):
        error = resp.json().get("error", "Upload failed")
        return None, error
    return resp.json()["documents"][0]["location"], None


def embed_document(workspace: str, location: str):
    """Add a document to a workspace's embedding index.

    Returns (True, None) on success or (False, error_string) on failure.
    """
    resp = _requests.post(
        f"{API_URL}/workspace/{workspace}/update-embeddings",
        headers=HEADERS,
        json={"adds": [location], "deletes": []},
    )
    if resp.status_code != 200:
        return False, f"Embedding failed (HTTP {resp.status_code})"
    return True, None


def remove_document(workspace: str, location: str):
    """Remove a document from both embeddings and storage in AnythingLLM.

    Returns (True, None) on success or (False, error_string) on failure.
    Failures are logged but non-fatal so callers can decide how to proceed.
    """
    # Step 1 -- remove from the workspace embedding index.
    resp = _requests.post(
        f"{API_URL}/workspace/{workspace}/update-embeddings",
        headers=HEADERS,
        json={"adds": [], "deletes": [location]},
    )
    if resp.status_code != 200:
        return False, f"Failed to remove embedding: {resp.text}"

    # Step 2 -- delete the underlying file from document storage.
    resp = _requests.delete(
        f"{API_URL}/system/remove-documents",
        headers=HEADERS,
        json={"names": [location]},
    )
    if resp.status_code != 200:
        return False, f"Failed to remove document: {resp.text}"

    return True, None
