import requests
import logging
from config import HEADERS, API_URL


# ---------------------------------------------------------------------------
# Workspace existence check
# ---------------------------------------------------------------------------

def LLM_workspace_exists(workspace: str) -> bool:
    """Return True if the workspace slug exists on the RAG backend."""
    response = requests.get(f"{API_URL}/workspace/{workspace}", headers=HEADERS)
    if response.status_code == 200:
        return True
    if response.status_code == 404:
        return False
    logging.error(
        f"Workspace existence check for '{workspace}' failed: {response.status_code}"
    )
    return False


# ---------------------------------------------------------------------------
# Document embedding
# ---------------------------------------------------------------------------

def LLM_upload_document(uploaded_file, file_name: str, workspace: str) -> str:
    """
    Upload *uploaded_file* (file-like) to the RAG backend and embed it into
    *workspace*.  Returns the doc_id string that identifies the embedded
    document (used as the file's primary key in the local DB).
    """
    response = requests.post(
        f"{API_URL}/workspace/{workspace}/embed",
        headers=HEADERS,
        files={"file": (file_name, uploaded_file)},
    )
    response.raise_for_status()
    data = response.json()
    return data["doc_id"]


def LLM_remove_document(workspace: str, doc_id: str) -> bool:
    """Delete an embedded document from the workspace by its doc_id."""
    resp = requests.delete(
        f"{API_URL}/workspace/{workspace}/embed/{doc_id}",
        headers=HEADERS,
    )
    if resp.status_code != 200:
        logging.error(f"Failed to remove document '{doc_id}': {resp.text}")
        return False
    return True


# ---------------------------------------------------------------------------
# Workspace settings
# ---------------------------------------------------------------------------

def LLM_json_workspace_settings(workspace: str):
    """
    Fetch workspace settings from the RAG backend.
    Returns a dict with keys: prompt, similarity_threshold, top_n, temperature.
    Returns None on failure.
    """
    response = requests.get(f"{API_URL}/workspace/{workspace}", headers=HEADERS)
    if response.status_code != 200:
        logging.error(
            f"Failed to fetch settings for workspace '{workspace}': "
            f"{response.status_code} {response.text}"
        )
        return None

    ws = response.json()
    return {
        "prompt": ws.get("system_prompt", ""),
        "similarity_threshold": ws.get("similarity_threshold", 0.5),
        "top_n": ws.get("top_n", 5),
        "temperature": ws.get("temperature", 0.7),
    }


def LLM_update_workspace_settings(workspace: str, settings: dict) -> bool:
    """
    Update mutable workspace settings via PUT /workspace/{slug}.
    Accepted keys: prompt, similarity_threshold, top_n, temperature.
    """
    payload = {}
    if "prompt" in settings:
        payload["system_prompt"] = settings["prompt"]
    if "similarity_threshold" in settings:
        payload["similarity_threshold"] = settings["similarity_threshold"]
    if "top_n" in settings:
        payload["top_n"] = settings["top_n"]
    if "temperature" in settings:
        payload["temperature"] = settings["temperature"]

    response = requests.put(
        f"{API_URL}/workspace/{workspace}",
        headers=HEADERS,
        json=payload,
    )
    if response.status_code != 200:
        logging.error(
            f"Failed to update workspace '{workspace}': "
            f"{response.status_code} {response.text}"
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Workspace lifecycle
# ---------------------------------------------------------------------------

def LLM_generate_new_workspace(workspace_id: str, workspace_name: str) -> bool:
    """
    Create a new workspace on the RAG backend.
    *workspace_id* is used as the slug; *workspace_name* is the display name.
    Returns True on success.
    """
    payload = {
        "name": workspace_name,
    }
    response = requests.post(
        f"{API_URL}/workspace",
        headers=HEADERS,
        json=payload,
    )
    if response.status_code != 200:
        logging.error(
            f"Failed to create workspace '{workspace_name}': "
            f"{response.status_code} {response.text}"
        )
        return False
    return True


def LLM_delete_workspace(workspace_id: str):
    """Delete a workspace from the RAG backend. Returns the requests.Response."""
    return requests.delete(
        f"{API_URL}/workspace/{workspace_id}",
        headers=HEADERS,
    )
