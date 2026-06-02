import asyncio
import hashlib
import io
import json
import re
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
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
    LLM_delete_workspace,
)
from config import API_URL, HEADERS
import requests as _requests
from config import TEXT_EXTENSIONS, DEBUG_UPLOAD_DIR, MAX_UPLOAD_BYTES
from database import Base, engine, get_db
from decling_conversion import convert_file, scrape_website_md
from models import Workspace, File as FileModel, ScrapeJob
from schemas import (
    FileResponse,
    FileCreate,
    WorkspaceCreate,
    WorkspaceResponse,
    WorkspaceUpdate,
    ScrapeJobCreate,
    ScrapeJobUpdate,
    ScrapeJobResponse,
)
from scraper import get_links_by_depth, get_links_by_prefix

NY = ZoneInfo("America/New_York")

# DB Setup
Base.metadata.create_all(bind=engine)

# Lightweight migrations for columns added after initial schema
from sqlalchemy import inspect as sa_inspect, text

with engine.connect() as conn:
    file_cols = [c["name"] for c in sa_inspect(engine).get_columns("files")]
    if "source_url" not in file_cols:
        conn.execute(text("ALTER TABLE files ADD COLUMN source_url VARCHAR"))
    if "scrape_job_id" not in file_cols:
        conn.execute(text("ALTER TABLE files ADD COLUMN scrape_job_id VARCHAR"))
    if "content_hash" not in file_cols:
        conn.execute(text("ALTER TABLE files ADD COLUMN content_hash VARCHAR"))
    if "last_checked_at" not in file_cols:
        conn.execute(text("ALTER TABLE files ADD COLUMN last_checked_at DATETIME"))
    conn.commit()

# App Setup
app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Semaphores
SEM = asyncio.Semaphore(10)
DELETE_SEM = asyncio.Semaphore(5)

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

scheduler = AsyncIOScheduler()


def _compute_next_run(interval: str | None, from_time=None):
    """Return a datetime for the next scheduled run, or None if manual."""
    from datetime import datetime
    if not interval:
        return None
    if from_time is None:
        from_time = datetime.now(NY)
    deltas = {
        "hourly": timedelta(hours=1),
        "daily": timedelta(days=1),
        "weekly": timedelta(weeks=1),
    }
    delta = deltas.get(interval)
    return (from_time + delta) if delta else None


async def _run_scrape_job_background(job_id: str):
    """Background task: run a scrape job by ID, updating DB directly."""
    from database import SessionLocal
    from datetime import datetime

    db = SessionLocal()
    try:
        job = db.query(ScrapeJob).filter(ScrapeJob.id == job_id).first()
        if not job or job.is_running:
            return

        job.is_running = True
        db.commit()

        try:
            # Re-discover URLs
            if job.mode == "single":
                urls, blocked = [job.base_url], []
            elif job.mode == "prefix":
                parsed_path = urlparse(job.base_url).path or "/"
                if not parsed_path.endswith("/"):
                    parsed_path = parsed_path.rsplit("/", 1)[0] + "/"
                urls, blocked = await get_links_by_prefix(
                    job.base_url,
                    prefixes=[parsed_path],
                    allow_offsite=job.allow_offsite,
                    max_pages=job.max_pages,
                )
            else:
                urls, blocked = await get_links_by_depth(
                    job.base_url,
                    max_depth=job.max_depth,
                    allow_offsite=job.allow_offsite,
                    max_pages=job.max_pages,
                )

            url_set = set(urls)

            # Existing files for this job
            existing_files = {
                f.source_url: f
                for f in db.query(FileModel).filter(FileModel.scrape_job_id == job_id).all()
                if f.source_url
            }

            # Remove pages no longer in the crawl
            for source_url, file_rec in list(existing_files.items()):
                if source_url not in url_set:
                    await asyncio.to_thread(
                        LLM_remove_document, job.workspace_id, file_rec.id
                    )
                    db.delete(file_rec)
            db.commit()

            # Process each discovered URL
            queue = asyncio.Queue()
            completed = []

            async def run_all():
                coros = [
                    _process_job_url(url, job, existing_files, queue)
                    for url in urls
                ]
                await asyncio.gather(*coros, return_exceptions=True)
                await queue.put(None)

            task = asyncio.create_task(run_all())
            while True:
                event = await queue.get()
                if event is None:
                    break
                if event.get("_file_record"):
                    completed.append(event["_file_record"])

            # Persist completed records
            for rec in completed:
                db.merge(rec)
            db.commit()
            await task

        finally:
            from datetime import datetime
            job.is_running = False
            job.last_scraped_at = datetime.now(NY)
            job.next_scrape_at = _compute_next_run(job.schedule_interval, job.last_scraped_at)
            db.commit()

    except Exception as e:
        print(f"[scheduler] error running job {job_id}: {e}")
    finally:
        db.close()


async def _process_job_url(url, job, existing_files, queue):
    """Scrape a single URL for a background job run; push result to queue."""
    async with SEM:
        from datetime import datetime
        try:
            md_result = await asyncio.to_thread(scrape_website_md, url)
            new_hash = hashlib.sha256(md_result.encode()).hexdigest()

            existing = existing_files.get(url)

            if existing and existing.content_hash == new_hash:
                # Unchanged — just update last_checked_at
                existing.last_checked_at = datetime.now(NY)
                await queue.put({"url": url, "status": "unchanged", "_file_record": existing})
                return

            filename = _sanitize_url_to_filename(url) + ".md"
            llm_file = io.StringIO(md_result)
            llm_file.name = filename

            if DEBUG_UPLOAD_DIR:
                debug_path = Path(DEBUG_UPLOAD_DIR) / filename
                debug_path.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(debug_path.write_text, llm_file.getvalue())

            file_location = await asyncio.to_thread(
                LLM_upload_document, llm_file, filename, job.workspace_id
            )

            if existing:
                # Changed — delete old, record will be updated via merge
                await asyncio.to_thread(
                    LLM_remove_document, job.workspace_id, existing.id
                )
                existing.id = file_location
                existing.filename = filename
                existing.content_hash = new_hash
                existing.last_checked_at = datetime.now(NY)
                await queue.put({"url": url, "status": "changed", "_file_record": existing})
            else:
                # New page
                rec = FileModel(
                    id=file_location,
                    filename=filename,
                    original_extension=".html",
                    workspace_id=job.workspace_id,
                    category=f"scrape_{job.name}",
                    source_url=url,
                    scrape_job_id=job.id,
                    content_hash=new_hash,
                    last_checked_at=datetime.now(NY),
                )
                await queue.put({"url": url, "status": "new", "_file_record": rec})

        except Exception as e:
            print(f"[job] error processing {url}: {e}")
            await queue.put({"url": url, "status": "error", "message": str(e)})


async def _check_due_jobs():
    """APScheduler job: find all jobs that are due and run them."""
    from datetime import datetime
    from database import SessionLocal
    db = SessionLocal()
    try:
        now = datetime.now(NY)
        due_jobs = (
            db.query(ScrapeJob)
            .filter(ScrapeJob.next_scrape_at <= now)
            .filter(ScrapeJob.is_running == False)
            .all()
        )
        for job in due_jobs:
            print(f"[scheduler] triggering job {job.id} ({job.name})")
            asyncio.create_task(_run_scrape_job_background(job.id))
    except Exception as e:
        print(f"[scheduler] error checking due jobs: {e}")
    finally:
        db.close()


@app.on_event("startup")
async def start_scheduler():
    scheduler.add_job(_check_due_jobs, "interval", minutes=1, id="check_due_jobs")
    scheduler.start()
    print("[scheduler] started, checking every 60 seconds")


@app.on_event("shutdown")
async def stop_scheduler():
    scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_url_to_filename(url: str) -> str:
    """Turn a URL into a safe, readable filename slug."""
    parsed = urlparse(url)
    path = parsed.path.strip("/").replace("/", "_") or "index"
    slug = re.sub(r"[^a-zA-Z0-9_\-]", "_", path)
    slug = re.sub(r"_+", "_", slug).strip("_")
    domain = parsed.netloc.replace(".", "_")
    return f"{domain}_{slug}" if slug else domain


# ---------------------------------------------------------------------------
# Web UI Routes
# ---------------------------------------------------------------------------

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

    # Collect distinct file extensions present in this workspace (uploaded only)
    extensions = sorted(
        {
            (f.original_extension or Path(f.filename).suffix).lower()
            for f in uploaded_files
            if (f.original_extension or Path(f.filename).suffix)
        }
    )

    # Scrape jobs for this workspace, with page counts attached
    scrape_jobs = (
        db.query(ScrapeJob)
        .filter(ScrapeJob.workspace_id == workspace_id)
        .order_by(ScrapeJob.created_at.desc())
        .all()
    )

    # Attach page count to each job for the template
    for job in scrape_jobs:
        job.page_count = (
            db.query(FileModel)
            .filter(FileModel.scrape_job_id == job.id)
            .count()
        )

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "files": uploaded_files,
            "workspace": workspace,
            "extensions": extensions,
            "scrape_jobs": scrape_jobs,
            "max_upload_bytes": MAX_UPLOAD_BYTES,
        },
    )


# ---------------------------------------------------------------------------
# File upload (web UI SSE)
# ---------------------------------------------------------------------------

async def processes_file(content, fname, workspace_id, queue):
    async with SEM:
        try:
            await queue.put({"file": fname, "status": "uploaded"})
            file_extension = Path(fname).suffix.lower()
            original_ext = file_extension
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


async def _stream_upload_progress(file_data, workspace_id, db):
    queue = asyncio.Queue()
    completed_files = []
    valid_files = []
    rejected_events = []

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


@app.post("/{workspace_id}/uploadfiles/", include_in_schema=False)
async def create_upload_files(
    workspace_id: str,
    uploaded_files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    workspace = db.query(Workspace).where(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    file_data = [(await f.read(), f.filename) for f in uploaded_files]

    return StreamingResponse(
        _stream_upload_progress(file_data, workspace_id, db),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ---------------------------------------------------------------------------
# Delete endpoints
# ---------------------------------------------------------------------------

async def _delete_file(file_id, workspace_id):
    async with DELETE_SEM:
        success = await asyncio.to_thread(LLM_remove_document, workspace_id, file_id)
        return file_id, success


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


@app.post("/delete-bulk", include_in_schema=False)
async def delete_bulk_files(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    file_ids = body.get("file_ids", [])

    files = {
        f.id: f for f in db.query(FileModel).filter(FileModel.id.in_(file_ids)).all()
    }

    print(f"[bulk-delete] received {len(file_ids)} id(s), {len(files)} found in DB")
    if len(file_ids) != len(files):
        missing = set(file_ids) - set(files)
        print(f"[bulk-delete] {len(missing)} id(s) not in DB (will be skipped): {list(missing)[:5]}")

    results = await asyncio.gather(
        *[_delete_file(fid, files[fid].workspace_id) for fid in files],
        return_exceptions=True,
    )
    deleted = []
    for r in results:
        if isinstance(r, Exception):
            print(f"[bulk-delete] exception during delete: {r}")
            continue
        file_id, success = r
        if success:
            db.delete(files[file_id])
            deleted.append(file_id)
        else:
            print(f"[bulk-delete] LLM_remove_document returned False for {file_id!r}")

    db.commit()
    print(f"[bulk-delete] done: {len(deleted)}/{len(files)} deleted successfully")
    return {"deleted": deleted}


# ---------------------------------------------------------------------------
# Settings endpoints
# ---------------------------------------------------------------------------

@app.get("/api/v1/workspaces/{workspace_id}/settings", include_in_schema=False)
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


@app.post("/api/v1/workspaces/{workspace_id}/settings", include_in_schema=False)
async def save_workspace_settings(workspace_id: str, request: Request):
    body = await request.json()
    success = LLM_update_workspace_settings(workspace_id, body)
    if not success:
        raise HTTPException(
            status_code=500, detail="Failed to update workspace settings"
        )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Scrape: discover (unchanged)
# ---------------------------------------------------------------------------

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
            urls, blocked = await get_links_by_prefix(
                base_url,
                prefixes=[parsed_path],
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


# ---------------------------------------------------------------------------
# Scrape Jobs CRUD
# ---------------------------------------------------------------------------

@app.get("/{workspace_id}/scrape/jobs", include_in_schema=False)
async def list_scrape_jobs(workspace_id: str, db: Session = Depends(get_db)):
    workspace = db.query(Workspace).where(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    jobs = (
        db.query(ScrapeJob)
        .filter(ScrapeJob.workspace_id == workspace_id)
        .order_by(ScrapeJob.created_at.desc())
        .all()
    )

    result = []
    for job in jobs:
        page_count = db.query(FileModel).filter(FileModel.scrape_job_id == job.id).count()
        result.append({
            "id": job.id,
            "workspace_id": job.workspace_id,
            "name": job.name,
            "base_url": job.base_url,
            "mode": job.mode,
            "max_depth": job.max_depth,
            "max_pages": job.max_pages,
            "allow_offsite": job.allow_offsite,
            "schedule_interval": job.schedule_interval,
            "last_scraped_at": job.last_scraped_at.isoformat() if job.last_scraped_at else None,
            "next_scrape_at": job.next_scrape_at.isoformat() if job.next_scrape_at else None,
            "is_running": job.is_running,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "page_count": page_count,
        })

    return result


@app.get("/{workspace_id}/scrape/jobs/{job_id}/pages", include_in_schema=False)
async def list_job_pages(workspace_id: str, job_id: str, db: Session = Depends(get_db)):
    job = (
        db.query(ScrapeJob)
        .filter(ScrapeJob.id == job_id, ScrapeJob.workspace_id == workspace_id)
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Scrape job not found")

    files = (
        db.query(FileModel)
        .filter(FileModel.scrape_job_id == job_id)
        .order_by(FileModel.uploaded_at.desc())
        .all()
    )

    return [
        {
            "id": f.id,
            "filename": f.filename,
            "source_url": f.source_url,
            "last_checked_at": f.last_checked_at.isoformat() if f.last_checked_at else None,
            "uploaded_at": f.uploaded_at.isoformat() if f.uploaded_at else None,
        }
        for f in files
    ]


@app.post("/{workspace_id}/scrape/jobs", include_in_schema=False)
async def create_scrape_job(workspace_id: str, request: Request, db: Session = Depends(get_db)):
    workspace = db.query(Workspace).where(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    body = await request.json()
    job = ScrapeJob(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        name=body.get("name", "Untitled Job"),
        base_url=body.get("base_url", ""),
        mode=body.get("mode", "depth"),
        max_depth=int(body.get("max_depth", 2)),
        max_pages=int(body.get("max_pages", 100)),
        allow_offsite=bool(body.get("allow_offsite", False)),
        schedule_interval=body.get("schedule_interval") or None,
    )

    if job.schedule_interval:
        from datetime import datetime
        job.next_scrape_at = _compute_next_run(job.schedule_interval, datetime.now(NY))

    db.add(job)
    db.commit()
    db.refresh(job)

    return {
        "id": job.id,
        "workspace_id": job.workspace_id,
        "name": job.name,
        "base_url": job.base_url,
        "mode": job.mode,
        "max_depth": job.max_depth,
        "max_pages": job.max_pages,
        "allow_offsite": job.allow_offsite,
        "schedule_interval": job.schedule_interval,
        "last_scraped_at": None,
        "next_scrape_at": job.next_scrape_at.isoformat() if job.next_scrape_at else None,
        "is_running": False,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "page_count": 0,
    }


@app.patch("/{workspace_id}/scrape/jobs/{job_id}", include_in_schema=False)
async def update_scrape_job(workspace_id: str, job_id: str, request: Request, db: Session = Depends(get_db)):
    job = (
        db.query(ScrapeJob)
        .filter(ScrapeJob.id == job_id, ScrapeJob.workspace_id == workspace_id)
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Scrape job not found")

    body = await request.json()

    if "name" in body and body["name"]:
        job.name = body["name"]
    if "base_url" in body and body["base_url"]:
        job.base_url = body["base_url"]
    if "mode" in body:
        job.mode = body["mode"]
    if "max_depth" in body:
        job.max_depth = int(body["max_depth"])
    if "max_pages" in body:
        job.max_pages = int(body["max_pages"])
    if "allow_offsite" in body:
        job.allow_offsite = bool(body["allow_offsite"])

    # Schedule change: recompute next_scrape_at
    if "schedule_interval" in body:
        new_interval = body["schedule_interval"] or None
        job.schedule_interval = new_interval
        if new_interval:
            from datetime import datetime
            job.next_scrape_at = _compute_next_run(new_interval, datetime.now(NY))
        else:
            job.next_scrape_at = None

    db.commit()
    db.refresh(job)
    page_count = db.query(FileModel).filter(FileModel.scrape_job_id == job.id).count()

    return {
        "id": job.id,
        "workspace_id": job.workspace_id,
        "name": job.name,
        "base_url": job.base_url,
        "mode": job.mode,
        "max_depth": job.max_depth,
        "max_pages": job.max_pages,
        "allow_offsite": job.allow_offsite,
        "schedule_interval": job.schedule_interval,
        "last_scraped_at": job.last_scraped_at.isoformat() if job.last_scraped_at else None,
        "next_scrape_at": job.next_scrape_at.isoformat() if job.next_scrape_at else None,
        "is_running": job.is_running,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "page_count": page_count,
    }


@app.delete("/{workspace_id}/scrape/jobs/{job_id}", include_in_schema=False)
async def delete_scrape_job(workspace_id: str, job_id: str, db: Session = Depends(get_db)):
    job = (
        db.query(ScrapeJob)
        .filter(ScrapeJob.id == job_id, ScrapeJob.workspace_id == workspace_id)
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Scrape job not found")

    # Delete all files associated with this job from the RAG backend
    files = db.query(FileModel).filter(FileModel.scrape_job_id == job_id).all()
    results = await asyncio.gather(
        *[_delete_file(f.id, workspace_id) for f in files],
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, Exception):
            print(f"[delete-job] exception during file delete: {r}")

    db.delete(job)  # cascade deletes FileModel rows via ORM
    db.commit()
    return {"deleted": job_id}


# ---------------------------------------------------------------------------
# Scrape Job: Run (SSE stream with smart change detection)
# ---------------------------------------------------------------------------

async def _process_scraped_url_sse(url, job, existing_files, queue):
    """
    Process one URL for an SSE-streamed job run.
    Detects new / changed / unchanged pages.
    """
    async with SEM:
        from datetime import datetime
        try:
            await queue.put({"url": url, "status": "fetching"})

            md_result = await asyncio.to_thread(scrape_website_md, url)
            new_hash = hashlib.sha256(md_result.encode()).hexdigest()

            await queue.put({"url": url, "status": "converted"})

            existing = existing_files.get(url)

            if existing and existing.content_hash == new_hash:
                # Unchanged — skip upload
                existing.last_checked_at = datetime.now(NY)
                await queue.put({
                    "url": url,
                    "status": "unchanged",
                    "name": existing.filename,
                    "_file_record": existing,
                })
                return

            filename = _sanitize_url_to_filename(url) + ".md"
            llm_file = io.StringIO(md_result)
            llm_file.name = filename

            if DEBUG_UPLOAD_DIR:
                debug_path = Path(DEBUG_UPLOAD_DIR) / filename
                debug_path.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(debug_path.write_text, llm_file.getvalue())

            file_location = await asyncio.to_thread(
                LLM_upload_document, llm_file, filename, job.workspace_id
            )

            if existing:
                # Changed — remove old from RAG, update record
                await asyncio.to_thread(LLM_remove_document, job.workspace_id, existing.id)
                existing.id = file_location
                existing.filename = filename
                existing.content_hash = new_hash
                existing.last_checked_at = datetime.now(NY)
                await queue.put({
                    "url": url,
                    "status": "changed",
                    "location": file_location,
                    "name": filename,
                    "category": job.name,
                    "_file_record": existing,
                })
            else:
                # New page
                rec = FileModel(
                    id=file_location,
                    filename=filename,
                    original_extension=".html",
                    workspace_id=job.workspace_id,
                    category=f"scrape_{job.name}",
                    source_url=url,
                    scrape_job_id=job.id,
                    content_hash=new_hash,
                    last_checked_at=datetime.now(NY),
                )
                await queue.put({
                    "url": url,
                    "status": "new",
                    "location": file_location,
                    "name": filename,
                    "category": job.name,
                    "_file_record": rec,
                })

        except Exception as e:
            print(f"[job-run] error processing {url}: {e}")
            await queue.put({"url": url, "status": "error", "message": str(e)})


async def _stream_job_run(job_id: str, workspace_id: str, db: Session):
    from datetime import datetime

    job = (
        db.query(ScrapeJob)
        .filter(ScrapeJob.id == job_id, ScrapeJob.workspace_id == workspace_id)
        .first()
    )
    if not job:
        yield f"data: {json.dumps({'status': 'error', 'message': 'Job not found'})}\n\n"
        return

    job.is_running = True
    db.commit()

    try:
        # Re-discover URLs
        yield f"data: {json.dumps({'status': 'discovering'})}\n\n"
        try:
            if job.mode == "single":
                urls, blocked = [job.base_url], []
            elif job.mode == "prefix":
                parsed_path = urlparse(job.base_url).path or "/"
                if not parsed_path.endswith("/"):
                    parsed_path = parsed_path.rsplit("/", 1)[0] + "/"
                urls, blocked = await get_links_by_prefix(
                    job.base_url,
                    prefixes=[parsed_path],
                    allow_offsite=job.allow_offsite,
                    max_pages=job.max_pages,
                )
            else:
                urls, blocked = await get_links_by_depth(
                    job.base_url,
                    max_depth=job.max_depth,
                    allow_offsite=job.allow_offsite,
                    max_pages=job.max_pages,
                )
        except Exception as e:
            yield f"data: {json.dumps({'status': 'error', 'message': f'Discovery failed: {str(e)}'})}\n\n"
            job.is_running = False
            db.commit()
            return

        url_set = set(urls)
        yield f"data: {json.dumps({'status': 'discovered', 'count': len(urls)})}\n\n"

        # Existing files for this job
        existing_files = {
            f.source_url: f
            for f in db.query(FileModel).filter(FileModel.scrape_job_id == job_id).all()
            if f.source_url
        }

        # Remove pages no longer in the crawl
        removed_count = 0
        for source_url, file_rec in list(existing_files.items()):
            if source_url not in url_set:
                await asyncio.to_thread(LLM_remove_document, workspace_id, file_rec.id)
                db.delete(file_rec)
                removed_count += 1
                yield f"data: {json.dumps({'url': source_url, 'status': 'removed'})}\n\n"
        if removed_count:
            db.commit()

        # Process each URL via SSE
        queue = asyncio.Queue()
        file_records = []

        async def run_all():
            coros = [
                _process_scraped_url_sse(url, job, existing_files, queue)
                for url in urls
            ]
            await asyncio.gather(*coros, return_exceptions=True)
            await queue.put(None)

        task = asyncio.create_task(run_all())

        while True:
            event = await queue.get()
            if event is None:
                break

            rec = event.pop("_file_record", None)
            if rec is not None:
                file_records.append(rec)

            # Don't stream internal unchanged events for now — just count them
            if event.get("status") != "unchanged":
                yield f"data: {json.dumps(event)}\n\n"

        # Persist all file records
        for rec in file_records:
            db.merge(rec)
        db.commit()

        await task

    finally:
        job.is_running = False
        job.last_scraped_at = datetime.now(NY)
        job.next_scrape_at = _compute_next_run(job.schedule_interval, job.last_scraped_at)
        db.commit()

    # Final summary
    page_count = db.query(FileModel).filter(FileModel.scrape_job_id == job_id).count()
    yield f"data: {json.dumps({'status': 'done', 'job_id': job_id, 'page_count': page_count})}\n\n"
    yield "data: [DONE]\n\n"


@app.post("/{workspace_id}/scrape/jobs/{job_id}/run", include_in_schema=False)
async def run_scrape_job(workspace_id: str, job_id: str, db: Session = Depends(get_db)):
    workspace = db.query(Workspace).where(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    job = (
        db.query(ScrapeJob)
        .filter(ScrapeJob.id == job_id, ScrapeJob.workspace_id == workspace_id)
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Scrape job not found")

    if job.is_running:
        raise HTTPException(status_code=409, detail="Job is already running")

    return StreamingResponse(
        _stream_job_run(job_id, workspace_id, db),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ---------------------------------------------------------------------------
# REST API endpoints
# ---------------------------------------------------------------------------

@app.post("/api/v1/workspaces/{workspace_id}/upload", response_model=list[FileResponse])
async def upload_to_workspace(
    workspace_id: str,
    uploaded_files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """
    Upload one or more files to a workspace.

    Non-text files are converted to Markdown before being sent to the RAG backend.
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

        file_extension = Path(f.filename).suffix.lower()
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


@app.post("/api/v1/workspaces/new")
async def create_new_workspace(workspace: WorkspaceCreate, request: Request, db: Session = Depends(get_db)):
    """
    Register a workspace in the local database.

    Workspaces are created and managed externally in AnythingLLM. This endpoint
    only records the workspace in the local database.

    - **id**: the workspace slug as it exists in AnythingLLM
    - **name**: display name for the workspace
    - **owners**: list of owner user IDs

    Raises **409** if a workspace with the given ID already exists in the database.
    """
    existing = db.query(Workspace).filter(Workspace.id == workspace.id).first()
    if existing:
        raise HTTPException(status_code=409, detail="Workspace already exists")

    db_workspace = Workspace(id=workspace.id, name=workspace.name, owners=workspace.owners)
    db.add(db_workspace)
    db.commit()
    db.refresh(db_workspace)
    return db_workspace


@app.post("/api/v1/workspaces/db")
async def create_new_workspace_DB_only(workspace: WorkspaceCreate, request: Request, db: Session = Depends(get_db)):
    """
    Create a new workspace in the local database only (does not create it in the RAG backend).

    Use this endpoint when the workspace already exists in the RAG backend and you only need
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


@app.get("/api/v1/workspaces/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace_info(workspace_id: str, request: Request, db: Session = Depends(get_db)):
    """
    Retrieve a workspace by its ID, including its associated files.

    Raises **404** if no workspace with the given ID exists.
    """
    workspace = db.query(Workspace).where(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace


@app.patch("/api/v1/workspaces/{workspace_id}")
async def rename_workspace(workspace_id: str, body: WorkspaceUpdate, db: Session = Depends(get_db)):
    """
    Rename a workspace by its ID.

    Raises **404** if no workspace with the given ID exists.
    """
    workspace = db.query(Workspace).where(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    workspace.name = body.name
    db.commit()
    db.refresh(workspace)
    return workspace


@app.delete("/api/v1/workspaces/{workspace_id}")
async def delete_workspace_by_id(workspace_id: str, db: Session = Depends(get_db)):
    """
    Delete a workspace by its ID from both the RAG backend and the local database.

    Raises **404** if the workspace does not exist, or **500** if deletion in the RAG backend fails.
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
