import asyncio
import io
import json
from pathlib import Path
import time

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from anythingllm import (
    workspace_exists,
    upload_document,
    remove_document,
    json_workspace_settings,
    update_workspace_settings,
    generate_new_workspace,
    delete_workspace,
)
from config import TEXT_EXTENSIONS, DEBUG_UPLOAD_DIR, MAX_UPLOAD_BYTES
from database import Base, engine, get_db
from decling_conversion import convert_file, scrape_website_md
from models import Workspace, File as FileModel
from schemas import FileResponse, FileCreate, WorkspaceCreate, WorkspaceResponse

# DB Setup
Base.metadata.create_all(bind=engine)

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
    files = db.query(FileModel).filter(FileModel.workspace_id == workspace_id).all()

    # Collect distinct file extensions present in this workspace
    extensions = sorted(
        {
            (f.original_extension or Path(f.filename).suffix).lower()
            for f in files
            if (f.original_extension or Path(f.filename).suffix)
        }
    )
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "files": files,
            "workspace": workspace,
            "extensions": extensions,
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
                upload_document, LLM_File, LLM_File.name, workspace_id
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
        success = await asyncio.to_thread(remove_document, workspace_id, file_id)
        return file_id, success


# web endpoint for deleting files
@app.delete("/delete/{file_id:path}", include_in_schema=False)
async def delete_uploaded_file(file_id: str, db: Session = Depends(get_db)):
    file_to_delete = db.query(FileModel).where(FileModel.id == file_id).first()
    if not file_to_delete:
        raise HTTPException(status_code=404, detail="File not found")

    success = remove_document(file_to_delete.workspace_id, file_id)
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
    settings = json_workspace_settings(workspace_id)
    if settings is None:
        raise HTTPException(status_code=404, detail="Workspace settings not found")
    return settings


@app.post("/api/v1/workspaces/{workspace_id}/settings",include_in_schema=False)
async def save_workspace_settings(workspace_id: str, request: Request):
    body = await request.json()
    success = update_workspace_settings(workspace_id, body)
    if not success:
        raise HTTPException(
            status_code=500, detail="Failed to update workspace settings"
        )
    return {"ok": True}


# api endpoints
# -------------------------------------------------------------------------------------------#


@app.post("/api/v1/workspaces/{workspace_id}/upload", response_model=list[FileResponse])
async def upload_to_workspace(
    workspace_id: str,
    uploaded_files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
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

        file_location = upload_document(LLM_File, file_name, workspace.id)

        db_file = FileModel(
            id=file_location,
            filename=f.filename,
            original_extension=file_extension.lower(),
            workspace_id=workspace_id,
        )
        db.add(db_file)
        saved_files.append(db_file)

    db.commit()
    for f in saved_files:
        db.refresh(f)
    return saved_files

@app.post("/api/v1/workspaces/new")
async def create_new_workspace(workspace: WorkspaceCreate, request: Request,db: Session = Depends(get_db)):
    existing = db.query(Workspace).filter(Workspace.id == workspace.id).first()
    if existing:
        raise HTTPException(status_code=409, detail="Workspace already exists")
    elif (generate_new_workspace(workspace.id, workspace.name)):
        db_workspace = Workspace(id=workspace.id, name=workspace.name, owners=workspace.owners)
        db.add(db_workspace)
        db.commit()
        db.refresh(db_workspace)
        return db_workspace
    
@app.get("/api/v1/workspaces/{workspace_id}")
async def get_workspace_info(workspace_id: str, request: Request,db: Session = Depends(get_db)):
    workspace = db.query(Workspace).where(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace

@app.delete("/api/v1/workspaces/{workspace_id}")
async def delete_workspace_by_id(workspace_id: str, db: Session = Depends(get_db)):
    workspace = db.query(Workspace).where(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    response = delete_workspace(workspace_id)
    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to delete workspace")
    db.delete(workspace)
    db.commit()
    return {"deleted": workspace_id}