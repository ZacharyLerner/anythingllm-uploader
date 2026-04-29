import asyncio
import io
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from anythingllm import (
    LLM_workspace_exists,
    LLM_upload_document,
    LLM_remove_document,
    LLM_json_workspace_settings,
    LLM_update_workspace_settings,
    LLM_generate_new_workspace,
    LLM_delete_workspace,
)
from config import TEXT_EXTENSIONS, DEBUG_UPLOAD_DIR, MAX_UPLOAD_BYTES
from database import Base, engine, get_db
from decling_conversion import convert_file, scrape_website_md
from models import Workspace, File as FileModel
from schemas import FileResponse, FileCreate, WorkspaceCreate, WorkspaceResponse
from scraper import get_links_by_depth, get_links_by_prefix

# DB Setup
Base.metadata.create_all(bind=engine)

# Add source_url column if it doesn't exist (lightweight migration for SQLite)
from sqlalchemy import inspect as sa_inspect, text
with engine.connect() as conn:
    columns = [c["name"] for c in sa_inspect(engine).get_columns("files")]
    if "source_url" not in columns:
        conn.execute(text("ALTER TABLE files ADD COLUMN source_url VARCHAR"))
        conn.commit()

# App Setup
app = FastAPI()

# Add Templates from libraries
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


# Async Semaphore Value
SEM = asyncio.Semaphore(10)


# Web Endpoints

# -------------------------------------------------------------------------------------------#

# fetches files in a specific workspace
@app.get("/{workspace_id}", include_in_schema=False, name="home")
async def home(request: Request, workspace_id: str, db: Session = Depends(get_db)):
    workspace = db.query(Workspace).where(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    uploaded_files = (
        db.query(FileModel)
        .filter(FileModel.workspace_id == workspace_id)
        .filter(~FileModel.category.like("scrape_%"))
        .all()
    )
    scraped_files = (
        db.query(FileModel)
        .filter(FileModel.workspace_id == workspace_id)
        .filter(FileModel.category.like("scrape_%"))
        .all()
    )

    # Collect distinct file extensions present in this workspace (uploaded only)
    extensions = sorted(
        {
            (f.original_extension or Path(f.filename).suffix).lower()
            for f in uploaded_files
            if (f.original_extension or Path(f.filename).suffix)
        }
    )
    # Collect distinct scrape categories (strip "scrape_" prefix for display)
    scrape_categories = sorted(
        {
            f.category.replace("scrape_", "", 1)
            for f in scraped_files
            if f.category
        }
    )
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "files": uploaded_files,
            "scraped_files": scraped_files,
            "workspace": workspace,
            "extensions": extensions,
            "scrape_categories": scrape_categories,
            "max_upload_bytes": MAX_UPLOAD_BYTES,
        },
    )

# Processes a file into MD and uploads to anythingLLM
async def processes_file(content, fname, workspace_id, queue):
    async with SEM:
        try:
            await queue.put({"file": fname, "status": "uploaded"})
            file_extension = Path(fname).suffix
            original_ext = file_extension.lower()
            file_name = fname

            if file_extension not in TEXT_EXTENSIONS:
                await queue.put({"file": fname, "status": "converted"})
                md_result = await asyncio.to_thread(convert_file, content, fname)
                LLM_File = io.StringIO(md_result)
                LLM_File.name = Path(fname).with_suffix(".md").name
            else:
                LLM_File = io.StringIO(content.decode("utf-8"))
                LLM_File.name = fname

            if DEBUG_UPLOAD_DIR:
                debug_path = Path(DEBUG_UPLOAD_DIR) / LLM_File.name
                debug_path.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(debug_path.write_text, LLM_File.getvalue())

            await queue.put({"file": fname, "status": "embedded"})
            file_location = await asyncio.to_thread(
                LLM_upload_document, LLM_File, LLM_File.name, workspace_id
            )

            await queue.put(
                {
                    "file": fname,
                    "status": "done",
                    "location": file_location,
                    "name": file_name,
                    "original_extension": original_ext,
                }
            )
        except Exception as e:
            print(f"Error processing file {fname}: {e}")
            await queue.put(
                {
                    "file": fname,
                    "status": "error",
                    "message": f"Processing failed: {str(e)}",
                }
            )

# Streams the progress on uploads as they pass
async def _stream_upload_progress(file_data, workspace_id, db):
    queue = asyncio.Queue()
    completed_files = []
    valid_files = []
    rejected_events = []

    # Check file sizes before processing
    for content, filename in file_data:
        if len(content) > MAX_UPLOAD_BYTES:
            size_mb = len(content) / (1024 * 1024)
            limit_mb = MAX_UPLOAD_BYTES / (1024 * 1024)
            rejected_events.append(
                {
                    "file": filename,
                    "status": "error",
                    "message": f"File is {size_mb:.1f} MB — exceeds {limit_mb:.0f} MB limit",
                }
            )
        else:
            valid_files.append((content, filename))

    # Yield rejection events immediately
    for event in rejected_events:
        yield f"data: {json.dumps(event)}\n\n"

    async def run_all():
        coroutines = []
        for content, filename in valid_files:
            coroutines.append(processes_file(content, filename, workspace_id, queue))
        await asyncio.gather(*coroutines, return_exceptions=True)
        await queue.put(None)

    task = asyncio.create_task(run_all())

    while True:
        event = await queue.get()
        if event is None:
            break
        if event["status"] == "done":
            completed_files.append(event)
        yield f"data: {json.dumps(event)}\n\n"

    for f in completed_files:
        db.add(
            FileModel(
                id=f["location"],
                filename=f["name"],
                original_extension=f.get("original_extension", ""),
                workspace_id=workspace_id,
                category="uploaded_file",
            )
        )
    db.commit()

    yield "data: [DONE]\n\n"
    await task

# web endpoint for uploading multiple files
@app.post("/{workspace_id}/uploadfiles/", include_in_schema=False)
async def create_upload_files(
    workspace_id: str,
    uploaded_files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    workspace = db.query(Workspace).where(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Read all files first (can't read UploadFile in threads)
    file_data = [(await f.read(), f.filename) for f in uploaded_files]

    return StreamingResponse(
        _stream_upload_progress(file_data, workspace_id, db),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )

# function for deleting a file asyncronsoly 
async def _delete_file(file_id, workspace_id):
    async with SEM:
        success = await asyncio.to_thread(LLM_remove_document, workspace_id, file_id)
        return file_id, success


# web endpoint for deleting files
@app.delete("/delete/{file_id:path}", include_in_schema=False)
async def delete_uploaded_file(file_id: str, db: Session = Depends(get_db)):
    file_to_delete = db.query(FileModel).where(FileModel.id == file_id).first()
    if not file_to_delete:
        raise HTTPException(status_code=404, detail="File not found")

    success = LLM_remove_document(file_to_delete.workspace_id, file_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete from LLM")

    db.delete(file_to_delete)
    db.commit()
    return {"deleted": file_id}


# web endpoint for bulk deleting files
@app.post("/delete-bulk", include_in_schema=False)
async def delete_bulk_files(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    file_ids = body.get("file_ids", [])

    files = {
        f.id: f for f in db.query(FileModel).filter(FileModel.id.in_(file_ids)).all()
    }

    results = await asyncio.gather(
        *[_delete_file(fid, files[fid].workspace_id) for fid in files],
        return_exceptions=True,
    )
    deleted = []
    for r in results:
        if isinstance(r, Exception):
            print(f"Delete failed: {r}")
            continue
        file_id, success = r
        if success:
            db.delete(files[file_id])
            deleted.append(file_id)

    db.commit()
    return {"deleted": deleted}

@app.get("/api/v1/workspaces/{workspace_id}/settings",include_in_schema=False)
async def fetch_workspace_settings(workspace_id: str):
    settings = LLM_json_workspace_settings(workspace_id)
    if settings is None:
        raise HTTPException(status_code=404, detail="Workspace settings not found")
    return settings


@app.get("/{workspace_id}/settings", include_in_schema=False, name="workspace_settings")
async def workspace_settings_page(request: Request, workspace_id: str, db: Session = Depends(get_db)):
    workspace = db.query(Workspace).where(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"workspace": workspace},
    )


@app.post("/api/v1/workspaces/{workspace_id}/settings",include_in_schema=False)
async def save_workspace_settings(workspace_id: str, request: Request):
    body = await request.json()
    success = LLM_update_workspace_settings(workspace_id, body)
    if not success:
        raise HTTPException(
            status_code=500, detail="Failed to update workspace settings"
        )
    return {"ok": True}


# Scrape endpoints
# -------------------------------------------------------------------------------------------#


def _sanitize_url_to_filename(url: str) -> str:
    """Turn a URL into a safe, readable filename slug."""
    parsed = urlparse(url)
    path = parsed.path.strip("/").replace("/", "_") or "index"
    # Remove query/fragment noise, keep only alphanum + underscores + hyphens
    slug = re.sub(r"[^a-zA-Z0-9_\-]", "_", path)
    slug = re.sub(r"_+", "_", slug).strip("_")
    # Prepend domain for uniqueness
    domain = parsed.netloc.replace(".", "_")
    return f"{domain}_{slug}" if slug else domain


# Phase 1: Discover URLs by crawling the site
@app.post("/{workspace_id}/scrape/discover", include_in_schema=False)
async def scrape_discover(workspace_id: str, request: Request, db: Session = Depends(get_db)):
    workspace = db.query(Workspace).where(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    body = await request.json()
    base_url = body.get("base_url", "").strip()
    mode = body.get("mode", "depth")
    max_depth = int(body.get("max_depth", 2))
    max_pages = int(body.get("max_pages", 100))
    allow_offsite = bool(body.get("allow_offsite", False))

    if not base_url:
        raise HTTPException(status_code=400, detail="base_url is required")

    try:
        if mode == "prefix":
            parsed_path = urlparse(base_url).path or "/"
            if not parsed_path.endswith("/"):
                parsed_path = parsed_path.rsplit("/", 1)[0] + "/"
            prefix_path = parsed_path if parsed_path else "/"
            urls, blocked = await get_links_by_prefix(
                base_url,
                prefixes=[prefix_path],
                allow_offsite=allow_offsite,
                max_pages=max_pages,
            )
        else:
            urls, blocked = await get_links_by_depth(
                base_url,
                max_depth=max_depth,
                allow_offsite=allow_offsite,
                max_pages=max_pages,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Crawl failed: {str(e)}")

    return {"urls": urls, "count": len(urls), "blocked": blocked}


# Process a single scraped URL: fetch HTML -> convert to MD -> upload to AnythingLLM
async def process_scraped_url(url, category, workspace_id, queue):
    async with SEM:
        try:
            await queue.put({"url": url, "status": "fetching"})

            md_result = await asyncio.to_thread(scrape_website_md, url)

            await queue.put({"url": url, "status": "converted"})

            filename = _sanitize_url_to_filename(url) + ".md"
            LLM_File = io.StringIO(md_result)
            LLM_File.name = filename

            if DEBUG_UPLOAD_DIR:
                debug_path = Path(DEBUG_UPLOAD_DIR) / filename
                debug_path.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(debug_path.write_text, LLM_File.getvalue())

            file_location = await asyncio.to_thread(
                LLM_upload_document, LLM_File, filename, workspace_id
            )

            await queue.put(
                {
                    "url": url,
                    "status": "done",
                    "location": file_location,
                    "name": filename,
                    "original_extension": ".html",
                    "category": category,
                }
            )
        except Exception as e:
            print(f"Error processing scraped URL {url}: {e}")
            await queue.put(
                {
                    "url": url,
                    "status": "error",
                    "message": str(e),
                }
            )


# SSE generator for scrape processing
async def _stream_scrape_progress(urls, category, workspace_id, db):
    queue = asyncio.Queue()
    completed_files = []

    async def run_all():
        coroutines = [
            process_scraped_url(url, category, workspace_id, queue)
            for url in urls
        ]
        await asyncio.gather(*coroutines, return_exceptions=True)
        await queue.put(None)

    task = asyncio.create_task(run_all())

    while True:
        event = await queue.get()
        if event is None:
            break
        if event["status"] == "done":
            completed_files.append(event)
        yield f"data: {json.dumps(event)}\n\n"

    cat_value = f"scrape_{category}" if category else "scrape_default"
    for f in completed_files:
        db.add(
            FileModel(
                id=f["location"],
                filename=f["name"],
                original_extension=f.get("original_extension", ".html"),
                workspace_id=workspace_id,
                category=cat_value,
                source_url=f["url"],
            )
        )
    db.commit()

    yield "data: [DONE]\n\n"
    await task


# Phase 2: Process confirmed URLs via SSE streaming
@app.post("/{workspace_id}/scrape/process", include_in_schema=False)
async def scrape_process(workspace_id: str, request: Request, db: Session = Depends(get_db)):
    workspace = db.query(Workspace).where(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    body = await request.json()
    urls = body.get("urls", [])
    category = body.get("category", "default")

    if not urls:
        raise HTTPException(status_code=400, detail="No URLs provided")

    return StreamingResponse(
        _stream_scrape_progress(urls, category, workspace_id, db),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# api endpoints
# -------------------------------------------------------------------------------------------#

# Uploads a document through API call
@app.post("/api/v1/workspaces/{workspace_id}/upload", response_model=list[FileResponse])
async def upload_to_workspace(
    workspace_id: str,
    uploaded_files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """
    Upload one or more files to a workspace.

    Non-text files are converted to Markdown before being sent to AnythingLLM.
    Text files are uploaded as-is. Each file is registered in the local database
    with its original extension preserved.

    Raises **404** if the workspace does not exist, or **413** if any file exceeds
    the maximum upload size.
    """
    workspace = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    saved_files = []
    for f in uploaded_files:
        content = await f.read()

        if len(content) > MAX_UPLOAD_BYTES:
            size_mb = len(content) / (1024 * 1024)
            limit_mb = MAX_UPLOAD_BYTES / (1024 * 1024)
            raise HTTPException(
                status_code=413,
                detail=f"File '{f.filename}' is {size_mb:.1f} MB — exceeds {limit_mb:.0f} MB limit",
            )

        file_extension = Path(f.filename).suffix
        file_name = f.filename

        if file_extension not in TEXT_EXTENSIONS:
            md_result = convert_file(content, f.filename)
            LLM_File = io.StringIO(md_result)
            file_name = Path(f.filename).with_suffix(".md").name
        else:
            LLM_File = io.StringIO(content.decode("utf-8"))

        file_location = LLM_upload_document(LLM_File, file_name, workspace.id)

        db_file = FileModel(
            id=file_location,
            filename=f.filename,
            original_extension=file_extension.lower(),
            workspace_id=workspace_id,
            category="uploaded_file",
        )
        db.add(db_file)
        saved_files.append(db_file)

    db.commit()
    for f in saved_files:
        db.refresh(f)
    return saved_files

# Creates a new workspace in AnythingLLM and the database 
@app.post("/api/v1/workspaces/new")
async def create_new_workspace(workspace: WorkspaceCreate, request: Request,db: Session = Depends(get_db)):
    """
    Create a new workspace in AnythingLLM and the local database.

    - **id**: unique workspace identifier
    - **name**: display name for the workspace
    - **owners**: list of owner user IDs

    Raises **409** if a workspace with the given ID already exists.
    """
    existing = db.query(Workspace).filter(Workspace.id == workspace.id).first()
    if existing:
        raise HTTPException(status_code=409, detail="Workspace already exists")
    elif (LLM_generate_new_workspace(workspace.id, workspace.name)):
        db_workspace = Workspace(id=workspace.id, name=workspace.name, owners=workspace.owners)
        db.add(db_workspace)
        db.commit()
        db.refresh(db_workspace)
        return db_workspace
    
# Creates a new workspace in just the database
@app.post("/api/v1/workspaces/db")
async def create_new_workspace_DB_only(workspace: WorkspaceCreate, request: Request,db: Session = Depends(get_db)):
    """
    Create a new workspace in the local database only (does not create it in AnythingLLM).

    Use this endpoint when the workspace already exists in AnythingLLM and you only need
    to register it in the local database.

    - **id**: unique workspace identifier
    - **name**: display name for the workspace
    - **owners**: list of owner user IDs

    Raises **409** if a workspace with the given ID already exists in the database.
    """
    existing = db.query(Workspace).filter(Workspace.id == workspace.id).first()
    if existing:
        raise HTTPException(status_code=409, detail="Workspace already exists")
    else:
        db_workspace = Workspace(id=workspace.id, name=workspace.name, owners=workspace.owners)
        db.add(db_workspace)
        db.commit()
        db.refresh(db_workspace)
        return db_workspace

# Gets the workspace by workspace id
@app.get("/api/v1/workspaces/{workspace_id}")
async def get_workspace_info(workspace_id: str, request: Request,db: Session = Depends(get_db)):
    """
    Retrieve a workspace by its ID.

    Raises **404** if no workspace with the given ID exists.
    """
    workspace = db.query(Workspace).where(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace

# Deletes a workspace by workspaceID
@app.delete("/api/v1/workspaces/{workspace_id}")
async def delete_workspace_by_id(workspace_id: str, db: Session = Depends(get_db)):
    """
    Delete a workspace by its ID from both AnythingLLM and the local database.

    Raises **404** if the workspace does not exist, or **500** if deletion in AnythingLLM fails.
    """
    workspace = db.query(Workspace).where(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    response = LLM_delete_workspace(workspace_id)
    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to delete workspace")
    db.delete(workspace)
    db.commit()
    return {"deleted": workspace_id}