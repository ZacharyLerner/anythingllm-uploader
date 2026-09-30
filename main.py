import asyncio
import hashlib
import io
import json
import re
import subprocess
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
from config import API_URL, HEADERS, APP_API_KEY
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
    job_cols = [c["name"] for c in sa_inspect(engine).get_columns("scrape_jobs")]
    if "urls" not in job_cols:
        conn.execute(text("ALTER TABLE scrape_jobs ADD COLUMN urls JSON"))
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


async def _discover_job_urls(job):
    """Return the list of URLs a job should scrape, according to its mode."""
    if job.mode == "single":
        return [job.base_url]
    if job.mode == "list":
        return list(job.urls or [])
    if job.mode == "prefix":
        parsed_path = urlparse(job.base_url).path or "/"
        if not parsed_path.endswith("/"):
            parsed_path = parsed_path.rsplit("/", 1)[0] + "/"
        return await get_links_by_prefix(
            job.base_url,
            prefixes=[parsed_path],
            allow_offsite=job.allow_offsite,
            max_pages=job.max_pages,
        )
    return await get_links_by_depth(
        job.base_url,
        max_depth=job.max_depth,
        allow_offsite=job.allow_offsite,
        max_pages=job.max_pages,
    )


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
            urls = await _discover_job_urls(job)

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
                    _delete_debug_file(job.workspace_id, file_rec.filename)
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
                debug_path = Path(DEBUG_UPLOAD_DIR) / job.workspace_id / filename
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


MAX_LIST_URLS = 1000

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_LIST_MARKER_RE = re.compile(r"^(?:\d+[.)]|[-*\u2022])$")
_WRAPPING_CHARS = "<>\"'`[]()"
_TRAILING_PUNCT = ".,;:!?"


def _normalize_list_url(token: str) -> str | None:
    """Clean one pasted token into an http(s) URL, or return None if it isn't one."""
    token = token.strip().lstrip(_WRAPPING_CHARS)
    token = token.rstrip(_TRAILING_PUNCT).rstrip(_WRAPPING_CHARS.replace(")", "")).rstrip(_TRAILING_PUNCT)
    # A trailing ")" with no matching "(" is sentence punctuation, not part of the URL
    while token.endswith(")") and token.count("(") < token.count(")"):
        token = token[:-1].rstrip(_TRAILING_PUNCT)
    if not token:
        return None
    if not _SCHEME_RE.match(token):
        token = "https://" + token

    parsed = urlparse(token)
    host = parsed.hostname or ""
    if parsed.scheme.lower() not in ("http", "https"):
        return None
    if not host or ("." not in host and host != "localhost"):
        return None
    try:
        parsed.port  # raises ValueError on a malformed port
    except ValueError:
        return None

    return parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=parsed.path or "/",
        fragment="",
    ).geturl()


def _parse_url_list(raw) -> tuple[list[str], list[str], int]:
    """
    Parse user-supplied URLs (a pasted block of text or a list of strings).

    URLs may be separated by newlines, spaces, tabs, or commas. List markers
    ("1.", "-", "*") and wrapping quotes/brackets are ignored, a missing scheme
    defaults to https://, and fragments are dropped. Plain words next to a URL
    are ignored; a line with no URL, or a URL-like token that can't be parsed,
    is reported as invalid.

    Returns (valid_urls, invalid_entries, duplicate_count); valid_urls keeps
    first-seen order with duplicates removed.
    """
    if raw is None:
        return [], [], 0
    text_block = "\n".join(str(x) for x in raw) if isinstance(raw, list) else str(raw)

    valid, invalid, seen = [], [], set()
    duplicates = 0
    for line in text_block.splitlines():
        line_urls, line_bad, line_has_content = [], [], False
        for tok in re.split(r"\s+", line):
            # "a.com,b.com" or "https://a.com,https://b.com" pasted without spaces
            for part in re.split(r",(?=\S)(?=[^,]*\.)", tok) if "," in tok else [tok]:
                part = part.strip().strip(",;")
                if not part or _LIST_MARKER_RE.match(part):
                    continue
                line_has_content = True
                url = _normalize_list_url(part)
                if url is not None:
                    line_urls.append(url)
                elif "." in part or "/" in part:
                    line_bad.append(part)  # looks like an attempted URL — report it

        if line_has_content and not line_urls and not line_bad:
            invalid.append(line.strip())  # a line of plain text with no URL in it
        invalid.extend(line_bad)
        for url in line_urls:
            if url in seen:
                duplicates += 1
            else:
                seen.add(url)
                valid.append(url)
    return valid, invalid, duplicates


def _job_to_dict(job, page_count: int = 0) -> dict:
    return {
        "id": job.id,
        "workspace_id": job.workspace_id,
        "name": job.name,
        "base_url": job.base_url,
        "mode": job.mode,
        "urls": job.urls,
        "max_depth": job.max_depth,
        "max_pages": job.max_pages,
        "allow_offsite": job.allow_offsite,
        "schedule_interval": job.schedule_interval,
        "last_scraped_at": job.last_scraped_at.isoformat() if job.last_scraped_at else None,
        "next_scrape_at": job.next_scrape_at.isoformat() if job.next_scrape_at else None,
        "is_running": bool(job.is_running),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "page_count": page_count,
    }


def _require_url_list(raw) -> list[str]:
    """Parse a URL list for saving on a job; 400 unless every entry is a usable URL."""
    urls, invalid, _ = _parse_url_list(raw)
    if invalid:
        shown = ", ".join(invalid[:5]) + (" …" if len(invalid) > 5 else "")
        raise HTTPException(status_code=400, detail=f"Not valid URLs: {shown}")
    if not urls:
        raise HTTPException(status_code=400, detail="At least one URL is required")
    if len(urls) > MAX_LIST_URLS:
        raise HTTPException(
            status_code=400, detail=f"Too many URLs ({len(urls)}); the limit is {MAX_LIST_URLS}"
        )
    return urls


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
                debug_path = Path(DEBUG_UPLOAD_DIR) / workspace_id / LLM_File.name
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

def _debug_filename(original_filename: str) -> str:
    """Return the filename as it was written to debug_uploads.

    Text-extension files are stored under their original name; all other
    files are converted to Markdown and stored with a .md suffix.
    """
    if Path(original_filename).suffix.lower() in TEXT_EXTENSIONS:
        return original_filename
    return Path(original_filename).with_suffix(".md").name


def _delete_debug_file(workspace_id: str, filename: str) -> None:
    """Remove the cached debug copy from debug_uploads if it exists.

    `filename` should be the *original* filename as stored in FileModel.filename;
    this function resolves the correct on-disk name automatically.
    """
    if not DEBUG_UPLOAD_DIR:
        return
    debug_path = Path(DEBUG_UPLOAD_DIR) / workspace_id / _debug_filename(filename)
    try:
        debug_path.unlink(missing_ok=True)
    except Exception as e:
        print(f"[debug] failed to remove debug file {debug_path}: {e}")


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

    _delete_debug_file(file_to_delete.workspace_id, file_to_delete.filename)
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
            f_rec = files[file_id]
            _delete_debug_file(f_rec.workspace_id, f_rec.filename)
            db.delete(f_rec)
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
    mode = body.get("mode", "depth")

    if mode == "list":
        urls, invalid, duplicates = _parse_url_list(body.get("urls"))
        if len(urls) > MAX_LIST_URLS:
            raise HTTPException(
                status_code=400,
                detail=f"Too many URLs ({len(urls)}); the limit is {MAX_LIST_URLS}",
            )
        return {
            "urls": urls,
            "count": len(urls),
            "invalid": invalid,
            "duplicates": duplicates,
        }

    base_url = body.get("base_url", "").strip()
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
            urls = await get_links_by_prefix(
                base_url,
                prefixes=[parsed_path],
                allow_offsite=allow_offsite,
                max_pages=max_pages,
            )
        else:
            urls = await get_links_by_depth(
                base_url,
                max_depth=max_depth,
                allow_offsite=allow_offsite,
                max_pages=max_pages,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Crawl failed: {str(e)}")

    return {"urls": urls, "count": len(urls)}


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

    return [
        _job_to_dict(
            job, db.query(FileModel).filter(FileModel.scrape_job_id == job.id).count()
        )
        for job in jobs
    ]


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
    mode = body.get("mode", "depth")
    job = ScrapeJob(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        name=body.get("name", "Untitled Job"),
        base_url=body.get("base_url", ""),
        mode=mode,
        max_depth=int(body.get("max_depth", 2)),
        max_pages=int(body.get("max_pages", 100)),
        allow_offsite=bool(body.get("allow_offsite", False)),
        schedule_interval=body.get("schedule_interval") or None,
    )

    if mode == "list":
        job.urls = _require_url_list(body.get("urls"))
        job.base_url = job.urls[0]
        job.max_depth = 0
        job.max_pages = len(job.urls)
        job.allow_offsite = True

    if job.schedule_interval:
        from datetime import datetime
        job.next_scrape_at = _compute_next_run(job.schedule_interval, datetime.now(NY))

    db.add(job)
    db.commit()
    db.refresh(job)

    return _job_to_dict(job)


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
    if "urls" in body or (job.mode == "list" and not job.urls):
        job.urls = _require_url_list(body.get("urls"))
        job.base_url = job.urls[0]
        job.max_pages = len(job.urls)

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

    return _job_to_dict(job, page_count)


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
    file_map = {f.id: f for f in files}
    results = await asyncio.gather(
        *[_delete_file(f.id, workspace_id) for f in files],
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, Exception):
            print(f"[delete-job] exception during file delete: {r}")
            continue
        file_id, success = r
        if success and file_id in file_map:
            _delete_debug_file(workspace_id, file_map[file_id].filename)

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
                debug_path = Path(DEBUG_UPLOAD_DIR) / job.workspace_id / filename
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
            urls = await _discover_job_urls(job)
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
                _delete_debug_file(workspace_id, file_rec.filename)
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

        if DEBUG_UPLOAD_DIR:
            debug_path = Path(DEBUG_UPLOAD_DIR) / workspace_id / file_name
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            debug_path.write_text(LLM_File.getvalue())
            LLM_File.seek(0)

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


# ---------------------------------------------------------------------------
# Debug: rsync between workspace debug folders
# ---------------------------------------------------------------------------

def _validate_workspace_slug(slug: str) -> None:
    """Reject slugs with path traversal or shell-unsafe characters."""
    if not slug or ".." in slug or "/" in slug or "\\" in slug:
        raise HTTPException(status_code=400, detail=f"Invalid workspace slug: {slug!r}")


@app.post("/api/v1/debug/rsync")
async def debug_rsync(request: Request):
    """
    Rsync the debug_uploads folder of one workspace to another.

    Body: { "source_workspace": "<slug>", "destination_workspace": "<slug>" }
    Passing the same slug for both is a safe no-op (rsync idempotent).
    Requires X-API-Key header when APP_API_KEY is configured.
    """
    if APP_API_KEY and request.headers.get("X-API-Key") != APP_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not DEBUG_UPLOAD_DIR:
        raise HTTPException(status_code=400, detail="DEBUG_UPLOAD_DIR is not configured")

    body = await request.json()
    source_slug = body.get("source_workspace", "")
    dest_slug = body.get("destination_workspace", "")

    _validate_workspace_slug(source_slug)
    _validate_workspace_slug(dest_slug)

    source = Path(DEBUG_UPLOAD_DIR) / source_slug
    destination = Path(DEBUG_UPLOAD_DIR) / dest_slug

    if not source.exists():
        raise HTTPException(status_code=404, detail=f"Source workspace folder not found: {source}")

    destination.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["rsync", "-a", f"{source}/", f"{destination}/"],
        capture_output=True,
        text=True,
    )

    return {
        "source": str(source),
        "destination": str(destination),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


# ---------------------------------------------------------------------------
# Reembed: re-upload all cached .md files for a workspace to the RAG backend
# ---------------------------------------------------------------------------

@app.post("/api/v1/workspaces/{workspace_id}/reembed")
async def reembed_workspace(workspace_id: str, request: Request, db: Session = Depends(get_db)):
    """
    Re-upload every cached .md file in debug_uploads/{workspace_id}/ back to
    the RAG backend. Useful for recovering from a wiped backend without user
    involvement.

    Updates existing FileModel records with the new doc_id. Creates a new
    record (category='reembedded') for any file not found in the DB.
    Requires X-API-Key header when APP_API_KEY is configured.
    """
    if APP_API_KEY and request.headers.get("X-API-Key") != APP_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not DEBUG_UPLOAD_DIR:
        raise HTTPException(status_code=400, detail="DEBUG_UPLOAD_DIR is not configured")

    workspace = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    workspace_dir = Path(DEBUG_UPLOAD_DIR) / workspace_id
    if not workspace_dir.exists():
        raise HTTPException(status_code=404, detail=f"No debug folder found for workspace: {workspace_id}")

    md_files = sorted(workspace_dir.glob("*.md"))
    if not md_files:
        return {"reembedded": 0, "failed": []}

    # Build a filename → FileModel lookup for fast matching
    existing = {
        f.filename: f
        for f in db.query(FileModel).filter(FileModel.workspace_id == workspace_id).all()
    }
    # Also index by the .md name (for documents stored with original filename)
    existing_by_md = {
        Path(f.filename).with_suffix(".md").name: f
        for f in existing.values()
    }

    reembedded = 0
    failed = []

    for md_path in md_files:
        try:
            content = md_path.read_text(encoding="utf-8")
            llm_file = io.StringIO(content)
            llm_file.name = md_path.name

            new_doc_id = await asyncio.to_thread(
                LLM_upload_document, llm_file, md_path.name, workspace_id
            )

            # Find matching DB record (exact .md name, or original filename mapped to .md)
            rec = existing.get(md_path.name) or existing_by_md.get(md_path.name)

            if rec:
                rec.id = new_doc_id
            else:
                rec = FileModel(
                    id=new_doc_id,
                    filename=md_path.name,
                    original_extension=".md",
                    workspace_id=workspace_id,
                    category="reembedded",
                )
                db.add(rec)

            db.commit()
            reembedded += 1

        except Exception as e:
            print(f"[reembed] failed for {md_path.name}: {e}")
            failed.append({"file": md_path.name, "error": str(e)})

    return {"reembedded": reembedded, "failed": failed}
