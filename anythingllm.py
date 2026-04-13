import requests
import logging
from config import HEADERS, API_URL
import time
import io


# Checks if a workspace exists by ID, return boolean
def workspace_exists(workspace: str) -> bool:
    response = requests.get(f"{API_URL}/workspace/{workspace}", headers=HEADERS)
    if response.status_code == 200:
        data = response.json()
        if data.get("workspace") == []:
            return False
        else:
            return True
    else:
        print(response.status_code)
        logging.error(
            f"check on workspace {workspace} failed with error code {response.status_code}"
        )
        return False


# Uploads and embeds a document into a workspace
def upload_document(uploaded_file, file_name, workspace: str):
    start_time = time.time()
    response = requests.post(
        f"{API_URL}/document/upload",
        headers=HEADERS,
        files={"file": (file_name, uploaded_file)},
        data={"addToWorkspaces": workspace},
    )
    end_time = time.time()
    print(f"Upload response time: {end_time - start_time} seconds")
    print(f"Upload code: {response.status_code}")
    data = response.json()
    return data["documents"][0]["location"]


def remove_document(workspace: str, location: str) -> bool:
    # Step 1: Remove from workspace embeddings
    resp = requests.post(
        f"{API_URL}/workspace/{workspace}/update-embeddings",
        headers=HEADERS,
        json={"adds": [], "deletes": [location]},
    )
    if resp.status_code != 200:
        print(f"Failed to remove embedding: {resp.text}")
        return False

    # Step 2: Delete from document storage
    resp = requests.delete(
        f"{API_URL}/system/remove-documents",
        headers=HEADERS,
        json={"names": [location]},
    )
    if resp.status_code != 200:
        print(f"Failed to remove document: {resp.text}")
        return False

    return True


def json_workspace_settings(workspace: str):
    response = requests.get(
        f"{API_URL}/workspace/{workspace}",
        headers=HEADERS,
    )
    if response.status_code != 200:
        logging.error(
            f"Failed to fetch settings for workspace {workspace}: {response.status_code} {response.text}"
        )
        return None

    data = response.json()
    workspaces = data.get("workspace", [])
    if not workspaces:
        logging.error(f"Workspace {workspace} not found in AnythingLLM")
        return None

    ws = workspaces[0]
    return {
        "prompt": ws.get("openAiPrompt"),
        "similarity_threshold": ws.get("similarityThreshold", 0.25),
        "top_n": ws.get("topN", 4),
        "temperature": ws.get("openAiTemp", 0.7),
    }


# Updates workspace settings via the AnythingLLM API
def update_workspace_settings(workspace: str, settings: dict) -> bool:
    payload = {}
    if "prompt" in settings:
        payload["openAiPrompt"] = settings["prompt"]
    if "similarity_threshold" in settings:
        payload["similarityThreshold"] = settings["similarity_threshold"]
    if "top_n" in settings:
        payload["topN"] = settings["top_n"]
    if "temperature" in settings:
        payload["openAiTemp"] = settings["temperature"]

    response = requests.post(
        f"{API_URL}/workspace/{workspace}/update",
        headers=HEADERS,
        json=payload,
    )
    if response.status_code != 200:
        logging.error(
            f"Failed to update workspace {workspace}: {response.status_code} {response.text}"
        )
        return False
    return True

# create a new workspace in anythingLLM
def generate_new_workspace(workspace_id: str, workspace_name: str):
    # default options payload 
    payload = {
        "name": workspace_id,
        "chatMode": "query",
        "topN": 5 
    }
    response = requests.post(
        f"{API_URL}/workspace/new",
        headers=HEADERS,
        json=payload,
    )
    if response.status_code != 200:
        logging.error(f"Failed to create workspace {workspace_name}: {response.status_code} {response.text}")
    else:
        payload = {
            "name": workspace_name,
        }
        update_response = requests.post(
            f"{API_URL}/workspace/{workspace_id}/update",
            headers=HEADERS,
            json=payload,
        )
        if update_response.status_code != 200:
            logging.error(f"Failed to update workspace {workspace_name}: {response.status_code} {response.text}")
            return False
    return True

def delete_workspace(workspace_id: str):
    request = requests.delete(f"{API_URL}/workspace/{workspace_id}",headers=HEADERS,)
    return request