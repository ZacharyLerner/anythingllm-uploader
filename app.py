"""
Flask web server for the Knowledge Base Upload application.

Serves a single-page UI where users can:
  - Upload documents (PDF, DOCX, etc.) to an AnythingLLM workspace.
  - Manage (list / delete) all documents in a unified view.
  - Configure website scrape sources and view scraped pages.

Documents from every origin (browser upload, API upload, scraper) are stored
in a single ``documents`` table with a ``source_type`` column (``upload`` or
``scrape``) that determines which section of the UI they appear in.

Upload progress is streamed to the browser using Server-Sent Events (SSE).
A separate endpoint is available for programmatic API callers.

Run:
    python app.py
"""

import json
import logging
import os
import re
import sqlite3
import tempfile
from functools import wraps
from typing import Optional

from flask import Flask, Response, jsonify, render_template, request

from anythingllm import (
    embed_document,
    remove_document,
    upload_document,
    workspace_exists,
)

from config import (
    APP_API_KEY,
    DEBUG_UPLOAD_DIR,
    MAX_UPLOAD_BYTES,
    TEXT_EXTENSIONS,
    WAKE_SIGNAL_PATH,
)
from db import init_db, open_db

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

# ---------------------------------------------------------------------------
# Debug upload interception
# When DEBUG_UPLOAD_DIR is set, every file destined for AnythingLLM is saved
# to that directory so you can inspect exactly what gets uploaded.  Files
# persist until you manually remove them.
# ---------------------------------------------------------------------------
if DEBUG_UPLOAD_DIR:
    os.makedirs(DEBUG_UPLOAD_DIR, exist_ok=True)
    log.info("Debug upload interception ENABLED -> %s", DEBUG_UPLOAD_DIR)


def _debug_save_file(filename: str, content: bytes) -> None:
    """Save a copy of *content* to DEBUG_UPLOAD_DIR if debugging is enabled.

    If a file with the same name already exists, a numeric suffix is appended
    to avoid overwriting previous captures.
    """
    if not DEBUG_UPLOAD_DIR:
        return
    dest = os.path.join(DEBUG_UPLOAD_DIR, filename)
    # Avoid overwriting earlier captures of the same filename.
    if os.path.exists(dest):
        base, ext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(dest):
            dest = os.path.join(DEBUG_UPLOAD_DIR, f"{base}_{counter}{ext}")
            counter += 1
    try:
        with open(dest, "wb") as f:
            f.write(content)
        log.info("Debug: saved upload to %s (%d bytes)", dest, len(content))
    except OSError:
        log.warning("Debug: failed to save upload to %s", dest)


# ---------------------------------------------------------------------------
# Optional Docling integration for rich document-to-Markdown conversion.
# If the library is not installed the app still works -- files are uploaded
# in their original format and AnythingLLM handles parsing.
# ---------------------------------------------------------------------------
try:
    from docling.backend.docling_parse_v4_backend import DoclingParseV4DocumentBackend
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        OcrAutoOptions,
        PdfPipelineOptions,
        TableStructureOptions,
    )
    from docling.datamodel.pipeline_options import TableFormerMode
    from docling.document_converter import DocumentConverter, FormatOption
    from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline

    _pdf_opts = PdfPipelineOptions(
        do_table_structure=True,
        table_structure_options=TableStructureOptions(
            do_cell_matching=True,
            mode=TableFormerMode.FAST,
        ),
        do_ocr=True,
        ocr_options=OcrAutoOptions(bitmap_area_threshold=0.10),
        # Disable features we don't need.
        do_picture_classification=False,
        do_picture_description=False,
        do_code_enrichment=False,
        do_formula_enrichment=False,
        generate_page_images=False,
        generate_picture_images=False,
        generate_table_images=False,
        # Abort conversion after 5 minutes.
        document_timeout=300.0,
    )

    _docling_converter = DocumentConverter(
        format_options={
            InputFormat.PDF: FormatOption(
                pipeline_options=_pdf_opts,
                pipeline_cls=StandardPdfPipeline,
                backend=DoclingParseV4DocumentBackend,
            ),
        },
    )
    DOCLING_ENABLED = True
    log.info("Docling enabled -- rich document conversion available")
except ImportError:
    DOCLING_ENABLED = False
    log.info("Docling not installed -- files uploaded in original format")


# ===================================================================
#  Helper functions
# ===================================================================


def _needs_conversion(filename: str) -> bool:
    """Return True if the file type benefits from Docling conversion."""
    ext = os.path.splitext(filename)[1].lower()
    return ext not in TEXT_EXTENSIONS


def _convert_with_docling(content: bytes, filename: str) -> Optional[str]:
    """Run *content* through Docling and return Markdown, or None on failure.

    Writes to a temp file because Docling requires a filesystem path.
    """
    if not DOCLING_ENABLED:
        return None
    suffix = os.path.splitext(filename)[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        result = _docling_converter.convert(tmp_path)
        return result.document.export_to_markdown()
    finally:
        os.unlink(tmp_path)


def _stream_json(data: dict) -> str:
    """Format *data* as a single SSE ``data:`` line."""
    return f"data: {json.dumps(data)}\n\n"


def _delete_document(workspace: str, doc_id: int):
    """Remove a document from AnythingLLM and the local DB.

    Returns (success, error_string_or_None).
    """
    with open_db() as db:
        doc = db.execute(
            "SELECT location FROM documents WHERE id = ? AND workspace = ?",
            (doc_id, workspace),
        ).fetchone()
        if not doc:
            return False, "Document not found"

    ok, err = remove_document(workspace, doc["location"])
    if not ok:
        return False, err

    with open_db() as db:
        with db:
            db.execute(
                "DELETE FROM documents WHERE id = ? AND workspace = ?",
                (doc_id, workspace),
            )
    return True, None


def _safe_error(message: str) -> str:
    """Sanitise error text before returning to the client.

    Strips internal filesystem paths and traceback fragments, then
    truncates to 200 characters.
    """
    message = re.sub(r"(/[\w./-]+)+", "[path]", message)
    message = re.sub(r'File ".*?"', "[internal]", message)
    return message[:200]


def _validate_scrape_source(data: dict, *, existing: Optional[dict] = None):
    """Validate scrape-source fields from request JSON.

    When *existing* is provided (update case) missing fields fall back to
    the existing row values.

    Returns ``(fields_dict, None)`` on success or ``(None, error_string)``
    on validation failure.
    """
    if existing:
        url = (data.get("url") or "").strip() or existing["url"]
        category = (data.get("category") or "").strip() or existing["category"]
        max_depth = data.get("max_depth", existing["max_depth"])
        schedule = data.get("schedule", existing["schedule"])
        enabled = data.get("enabled", existing["enabled"])
        crawl_mode = data.get("crawl_mode", existing.get("crawl_mode", "depth"))
        max_pages = data.get("max_pages", existing.get("max_pages", 100))
        allow_offsite = data.get("allow_offsite", existing.get("allow_offsite", 0))
        offsite_depth = data.get("offsite_depth", existing.get("offsite_depth", 1))
    else:
        url = (data.get("url") or "").strip()
        category = (data.get("category") or "").strip()
        max_depth = data.get("max_depth", 1)
        schedule = data.get("schedule")
        enabled = 1
        crawl_mode = data.get("crawl_mode", "depth")
        max_pages = data.get("max_pages", 100)
        allow_offsite = data.get("allow_offsite", 0)
        offsite_depth = data.get("offsite_depth", 1)

    if not url:
        return None, "URL is required"
    if not category:
        return None, "Category is required"
    if crawl_mode not in ("depth", "prefix"):
        return None, "Crawl mode must be 'depth' or 'prefix'"
    if crawl_mode == "depth":
        if not isinstance(max_depth, int) or not 1 <= max_depth <= 5:
            return None, "Depth must be between 1 and 5"
    if crawl_mode == "prefix":
        if not isinstance(max_pages, int) or max_pages < 1:
            return None, "Max pages must be a positive integer"
    if schedule and schedule not in ("daily", "weekly", "monthly"):
        return None, "Schedule must be daily, weekly, monthly, or null"

    # Validate offsite crawling fields.
    allow_offsite = int(bool(allow_offsite))
    if not isinstance(offsite_depth, int) or not 1 <= offsite_depth <= 3:
        return None, "Off-domain depth must be between 1 and 3"

    # Handle allowed_prefixes.
    if "allowed_prefixes" in data:
        allowed_prefixes = data["allowed_prefixes"]
        if crawl_mode == "prefix" and (
            not isinstance(allowed_prefixes, list) or not allowed_prefixes
        ):
            return None, "At least one allowed prefix is required for prefix mode"
        prefixes_json = json.dumps(allowed_prefixes) if allowed_prefixes else None
    elif existing:
        prefixes_json = existing.get("allowed_prefixes")
    else:
        allowed_prefixes = data.get("allowed_prefixes")
        if crawl_mode == "prefix" and not allowed_prefixes:
            return None, "At least one allowed prefix is required for prefix mode"
        prefixes_json = json.dumps(allowed_prefixes) if allowed_prefixes else None

    return {
        "url": url,
        "category": category,
        "max_depth": max_depth,
        "schedule": schedule,
        "enabled": int(bool(enabled)),
        "crawl_mode": crawl_mode,
        "max_pages": max_pages,
        "allowed_prefixes": prefixes_json,
        "allow_offsite": allow_offsite,
        "offsite_depth": offsite_depth,
    }, None


# ===================================================================
#  Auth & workspace decorators
# ===================================================================


def _require_api_key(f):
    """Decorator that checks the ``Authorization: Bearer <key>`` header.

    If ``APP_API_KEY`` is empty the check is skipped (auth disabled).
    """

    @wraps(f)
    def wrapper(*args, **kwargs):
        if not APP_API_KEY:
            return f(*args, **kwargs)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != APP_API_KEY:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)

    return wrapper


def _require_workspace(f):
    """Decorator that validates the ``workspace`` path parameter exists in
    AnythingLLM.  Returns 404 if the workspace slug is invalid."""

    @wraps(f)
    def wrapper(workspace, *args, **kwargs):
        if not workspace_exists(workspace):
            return jsonify({"error": "Workspace not found"}), 404
        return f(workspace, *args, **kwargs)

    return wrapper


# ===================================================================
#  Error handlers
# ===================================================================


@app.errorhandler(413)
def _file_too_large(_e):
    return jsonify({"error": "File too large. Maximum size is 100 MB."}), 413


# ===================================================================
#  Page routes
# ===================================================================


@app.route("/<workspace>")
def workspace_ui(workspace):
    """Serve the main upload page for a workspace, or 404 if invalid."""
    if not workspace_exists(workspace):
        return render_template("404.html", workspace=workspace), 404
    return render_template("index.html", workspace=workspace)


# ===================================================================
#  Document upload (SSE -- for the browser UI)
# ===================================================================


@app.route("/<workspace>/upload", methods=["POST"])
def upload(workspace):
    """Upload a file to a workspace.

    The response is an SSE stream so the browser can show real-time progress:
    ``converting`` -> ``uploading`` -> ``embedding`` -> ``complete``
    (or ``duplicate`` / ``error`` at any point).

    Query params:
        replace=true  -- overwrite an existing document with the same name.
    """
    file = request.files.get("file")
    if file is None:
        return jsonify({"error": "No file provided"}), 400

    original_filename: str = file.filename or "untitled"
    file_content: bytes = file.read()
    file_content_type: str = file.content_type or "application/octet-stream"
    replace: bool = request.args.get("replace") == "true"

    def generate():
        try:
            # --- Duplicate check ------------------------------------------------
            with open_db() as db:
                existing = db.execute(
                    "SELECT id, location FROM documents "
                    "WHERE workspace = ? AND filename = ? AND source_type = 'upload'",
                    (workspace, original_filename),
                ).fetchone()

            if existing and not replace:
                yield _stream_json({"step": "duplicate", "filename": original_filename})
                return

            if existing and replace:
                ok, err = _delete_document(workspace, existing["id"])
                if not ok:
                    yield _stream_json(
                        {
                            "step": "error",
                            "error": _safe_error(f"Replace failed: {err}"),
                        }
                    )
                    return

            # --- Step 1: Convert (optional) -------------------------------------
            converted = False
            upload_filename = original_filename
            upload_content = file_content
            upload_ct = file_content_type

            if DOCLING_ENABLED and _needs_conversion(original_filename):
                yield _stream_json({"step": "converting"})
                md = _convert_with_docling(file_content, original_filename)
                if md:
                    converted = True
                    upload_filename = os.path.splitext(original_filename)[0] + ".md"
                    upload_content = md.encode("utf-8")
                    upload_ct = "text/markdown"

            # --- Debug: save a copy before sending to AnythingLLM ----------------
            _debug_save_file(upload_filename, upload_content)

            # --- Step 2: Upload to AnythingLLM ----------------------------------
            yield _stream_json({"step": "uploading"})
            location, err = upload_document(upload_filename, upload_content, upload_ct)
            if err or not location:
                yield _stream_json(
                    {"step": "error", "error": _safe_error(err or "Upload failed")}
                )
                return

            # --- Step 3: Embed into workspace -----------------------------------
            yield _stream_json({"step": "embedding"})
            ok, embed_err = embed_document(workspace, location)
            if not ok:
                # FIX Leak 1: Clean up the orphaned file from AnythingLLM storage
                # so it doesn't persist with no local DB reference.
                _cleanup_ok, _cleanup_err = remove_document(workspace, location)
                if not _cleanup_ok:
                    log.warning(
                        "Failed to clean up uploaded file after embed failure: %s",
                        _cleanup_err,
                    )
                yield _stream_json(
                    {
                        "step": "error",
                        "error": _safe_error(embed_err or "Embedding failed"),
                    }
                )
                return

            # --- Step 4: Record in local DB -------------------------------------
            try:
                with open_db() as db:
                    with db:
                        db.execute(
                            "INSERT INTO documents "
                            "(workspace, filename, location, source_type, converted) "
                            "VALUES (?, ?, ?, 'upload', ?)",
                            (workspace, original_filename, location, int(converted)),
                        )
            except sqlite3.IntegrityError:
                # Race condition: another upload for the same file completed first.
                yield _stream_json({"step": "duplicate", "filename": original_filename})
                return

            yield _stream_json(
                {
                    "step": "complete",
                    "filename": original_filename,
                    "converted": converted,
                }
            )

        except Exception:
            log.exception("Upload failed for %s", original_filename)
            yield _stream_json(
                {"step": "error", "error": "An internal error occurred."}
            )

    return Response(generate(), mimetype="text/event-stream")


# ===================================================================
#  Document upload (for external API callers)
# ===================================================================


@app.route("/<workspace>/documents", methods=["POST"])
@_require_workspace
@_require_api_key
def api_upload(workspace):
    """Upload a document via the API (non-SSE).

    Accepts a multipart file upload with optional form fields:
        file         -- the file to upload (required).
        source_type  -- ``upload`` (default) or ``scrape``.
        source_url   -- required when source_type is ``scrape``.
        title        -- optional, displayed in the scraped-documents section.
        category     -- optional, used for filtering in the UI.

    Returns a JSON response with the document record on success.
    """
    file = request.files.get("file")
    if file is None:
        return jsonify({"error": "No file provided"}), 400

    # --- Read metadata from form fields ---
    source_type = request.form.get("source_type", "upload").strip().lower()
    if source_type not in ("upload", "scrape"):
        return jsonify({"error": "source_type must be 'upload' or 'scrape'"}), 400

    source_url = (request.form.get("source_url") or "").strip() or None
    title = (request.form.get("title") or "").strip() or None
    category = (request.form.get("category") or "").strip() or None

    if source_type == "scrape" and not source_url:
        return jsonify(
            {"error": "source_url is required when source_type is 'scrape'"}
        ), 400

    # --- Upload + optional Docling conversion ---
    original_filename = file.filename or "untitled"
    file_content = file.read()
    file_content_type = file.content_type or "application/octet-stream"

    converted = False
    upload_filename = original_filename
    upload_content = file_content
    upload_ct = file_content_type

    if DOCLING_ENABLED and _needs_conversion(original_filename):
        md = _convert_with_docling(file_content, original_filename)
        if md:
            converted = True
            upload_filename = os.path.splitext(original_filename)[0] + ".md"
            upload_content = md.encode("utf-8")
            upload_ct = "text/markdown"

    # --- Debug: save a copy before sending to AnythingLLM ---
    _debug_save_file(upload_filename, upload_content)

    location, err = upload_document(upload_filename, upload_content, upload_ct)
    if err or not location:
        return jsonify({"error": _safe_error(err or "Upload failed")}), 502

    ok, embed_err = embed_document(workspace, location)
    if not ok:
        # FIX Leak 1: Clean up the orphaned file from AnythingLLM storage
        # so it doesn't persist with no local DB reference.
        _cleanup_ok, _cleanup_err = remove_document(workspace, location)
        if not _cleanup_ok:
            log.warning(
                "Failed to clean up uploaded file after embed failure: %s",
                _cleanup_err,
            )
        return jsonify({"error": _safe_error(embed_err or "Embedding failed")}), 502

    # --- Record in local DB ---
    try:
        with open_db() as db:
            with db:
                cur = db.execute(
                    "INSERT INTO documents "
                    "(workspace, filename, location, source_type, "
                    "source_url, title, category, converted) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        workspace,
                        original_filename,
                        location,
                        source_type,
                        source_url,
                        title,
                        category,
                        int(converted),
                    ),
                )
                doc_id = cur.lastrowid
    except sqlite3.IntegrityError:
        return jsonify({"error": "A document with this filename already exists"}), 409

    result = {
        "id": doc_id,
        "filename": original_filename,
        "location": location,
        "source_type": source_type,
        "converted": converted,
    }
    if source_url:
        result["source_url"] = source_url
    if title:
        result["title"] = title
    if category:
        result["category"] = category

    return jsonify(result), 201


# ===================================================================
#  Unified document management
# ===================================================================


@app.route("/<workspace>/documents")
def list_docs(workspace):
    """Return JSON array of documents, with optional filtering.

    Query params:
        source_type  -- Filter by origin: 'upload' or 'scrape'.
        category     -- Filter by category.
    """
    query = "SELECT * FROM documents WHERE workspace = ?"
    params: list = [workspace]

    source_type = request.args.get("source_type")
    if source_type:
        query += " AND source_type = ?"
        params.append(source_type)

    category = request.args.get("category")
    if category:
        query += " AND category = ?"
        params.append(category)

    query += " ORDER BY created_at DESC"

    with open_db() as db:
        rows = db.execute(query, params).fetchall()

    def _doc_dict(r):
        d = {
            "id": r["id"],
            "filename": r["filename"],
            "location": r["location"],
            "source_type": r["source_type"],
            "created_at": r["created_at"],
        }
        # Include extended fields when present.
        if r["source_url"]:
            d["source_url"] = r["source_url"]
        if r["title"]:
            d["title"] = r["title"]
        if r["category"]:
            d["category"] = r["category"]
        if r["source_id"]:
            d["source_id"] = r["source_id"]
        if r["depth"]:
            d["depth"] = r["depth"]
        if r["converted"]:
            d["converted"] = bool(r["converted"])
        return d

    return jsonify([_doc_dict(r) for r in rows])


@app.route("/<workspace>/documents/<int:doc_id>", methods=["DELETE"])
@_require_api_key
def delete_doc(workspace, doc_id):
    """Delete a single document by its id."""
    ok, err = _delete_document(workspace, doc_id)
    if not ok:
        status = 404 if err == "Document not found" else 502
        return jsonify({"error": _safe_error(err or "Delete failed")}), status
    return jsonify({"removed": doc_id})


@app.route("/<workspace>/documents/batch", methods=["DELETE"])
@_require_api_key
def delete_docs_batch(workspace):
    """Delete multiple documents in one request.

    Body: ``{"ids": [1, 2, 3, ...]}``
    """
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])

    if not ids:
        return jsonify({"error": "No ids provided"}), 400

    results = []
    for doc_id in ids:
        ok, err = _delete_document(workspace, doc_id)
        results.append(
            {"id": doc_id, "removed": ok, "error": _safe_error(err) if err else None}
        )
    return jsonify({"results": results})


# ===================================================================
#  Scrape source CRUD
# ===================================================================


@app.route("/<workspace>/scrape-sources")
def list_scrape_sources(workspace):
    """List all scrape sources for a workspace with latest job status."""
    with open_db() as db:
        rows = db.execute(
            """
            SELECT s.*,
                   j.id           AS job_id,
                   j.status       AS job_status,
                   j.pages_found  AS job_pages_found,
                   j.pages_scraped AS job_pages_scraped,
                   j.error        AS job_error,
                   j.started_at   AS job_started_at,
                   j.completed_at AS job_completed_at,
                   (SELECT COUNT(*) FROM documents d
                    WHERE d.source_id = s.id AND d.source_type = 'scrape') AS doc_count
            FROM scrape_sources s
            LEFT JOIN scrape_jobs j ON j.id = (
                SELECT j2.id FROM scrape_jobs j2
                WHERE j2.source_id = s.id
                ORDER BY j2.requested_at DESC LIMIT 1
            )
            WHERE s.workspace = ?
            ORDER BY s.created_at DESC
            """,
            (workspace,),
        ).fetchall()

    def _source_dict(row):
        latest_job = None
        if row["job_id"]:
            latest_job = {
                "id": row["job_id"],
                "status": row["job_status"],
                "pages_found": row["job_pages_found"],
                "pages_scraped": row["job_pages_scraped"],
                "error": row["job_error"],
                "started_at": row["job_started_at"],
                "completed_at": row["job_completed_at"],
            }
        return {
            "id": row["id"],
            "url": row["url"],
            "category": row["category"],
            "max_depth": row["max_depth"],
            "crawl_mode": row["crawl_mode"],
            "allowed_prefixes": row["allowed_prefixes"],
            "max_pages": row["max_pages"],
            "schedule": row["schedule"],
            "enabled": bool(row["enabled"]),
            "allow_offsite": row["allow_offsite"],
            "offsite_depth": row["offsite_depth"],
            "created_at": row["created_at"],
            "last_scraped_at": row["last_scraped_at"],
            "doc_count": row["doc_count"],
            "latest_job": latest_job,
        }

    return jsonify([_source_dict(r) for r in rows])


@app.route("/<workspace>/scrape-sources", methods=["POST"])
def create_scrape_source(workspace):
    """Create a new scrape source.

    Body: ``{"url", "category", "max_depth": 1-5, "schedule": "daily"|"weekly"|"monthly"|null,
             "crawl_mode": "depth"|"prefix", "allowed_prefixes": [...], "max_pages": int}``
    """
    data = request.get_json(silent=True) or {}
    fields, err = _validate_scrape_source(data)
    if err or fields is None:
        return jsonify({"error": err}), 400

    try:
        with open_db() as db:
            with db:
                cur = db.execute(
                    "INSERT INTO scrape_sources "
                    "(workspace, url, category, max_depth, schedule, crawl_mode, "
                    "allowed_prefixes, max_pages, allow_offsite, offsite_depth) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        workspace,
                        fields["url"],
                        fields["category"],
                        fields["max_depth"],
                        fields["schedule"],
                        fields["crawl_mode"],
                        fields["allowed_prefixes"],
                        fields["max_pages"],
                        fields["allow_offsite"],
                        fields["offsite_depth"],
                    ),
                )
                source_id = cur.lastrowid
    except sqlite3.IntegrityError:
        return jsonify(
            {"error": "This URL is already configured for this workspace"}
        ), 409

    return jsonify({"id": source_id, "message": "Source created"}), 201


@app.route("/<workspace>/scrape-sources/<int:source_id>", methods=["PUT"])
def update_scrape_source(workspace, source_id):
    """Update an existing scrape source.

    Body: any subset of ``{url, category, max_depth, schedule, enabled,
          crawl_mode, allowed_prefixes, max_pages}``.
    """
    data = request.get_json(silent=True) or {}

    with open_db() as db:
        existing = db.execute(
            "SELECT * FROM scrape_sources WHERE id = ? AND workspace = ?",
            (source_id, workspace),
        ).fetchone()
        if not existing:
            return jsonify({"error": "Source not found"}), 404

        fields, err = _validate_scrape_source(data, existing=dict(existing))
        if err or fields is None:
            return jsonify({"error": err}), 400

        try:
            with db:
                db.execute(
                    "UPDATE scrape_sources "
                    "SET url=?, category=?, max_depth=?, schedule=?, enabled=?, "
                    "crawl_mode=?, allowed_prefixes=?, max_pages=?, "
                    "allow_offsite=?, offsite_depth=? "
                    "WHERE id=? AND workspace=?",
                    (
                        fields["url"],
                        fields["category"],
                        fields["max_depth"],
                        fields["schedule"],
                        fields["enabled"],
                        fields["crawl_mode"],
                        fields["allowed_prefixes"],
                        fields["max_pages"],
                        fields["allow_offsite"],
                        fields["offsite_depth"],
                        source_id,
                        workspace,
                    ),
                )
        except sqlite3.IntegrityError:
            return jsonify(
                {"error": "This URL is already configured for this workspace"}
            ), 409

    return jsonify({"message": "Source updated"})


@app.route("/<workspace>/scrape-sources/<int:source_id>", methods=["DELETE"])
def delete_scrape_source(workspace, source_id):
    """Delete a scrape source *and* all its scraped documents.

    If some documents cannot be removed from AnythingLLM, their local DB
    rows are retained so they can be retried later.  The scrape source
    itself is only deleted when *all* associated documents have been
    successfully cleaned up.  Returns HTTP 207 on partial success.
    """
    with open_db() as db:
        existing = db.execute(
            "SELECT * FROM scrape_sources WHERE id = ? AND workspace = ?",
            (source_id, workspace),
        ).fetchone()
        if not existing:
            return jsonify({"error": "Source not found"}), 404

        # Collect locations so we can remove them from AnythingLLM.
        docs = db.execute(
            "SELECT location FROM documents WHERE source_id = ? AND source_type = 'scrape'",
            (source_id,),
        ).fetchall()

        # FIX Leak 3: Track which removals fail so we only delete DB rows
        # for documents that were actually cleaned up in AnythingLLM.
        failed_locations = set()
        for doc in docs:
            ok, err = remove_document(workspace, doc["location"])
            if not ok:
                log.warning("Failed to remove scraped doc from AnythingLLM: %s", err)
                failed_locations.add(doc["location"])

        with db:
            if failed_locations:
                # Only delete docs that were successfully removed remotely.
                placeholders = ",".join("?" for _ in failed_locations)
                db.execute(
                    f"DELETE FROM documents WHERE source_id = ? "
                    f"AND source_type = 'scrape' "
                    f"AND location NOT IN ({placeholders})",
                    (source_id, *failed_locations),
                )
            else:
                db.execute(
                    "DELETE FROM documents WHERE source_id = ? AND source_type = 'scrape'",
                    (source_id,),
                )

            db.execute("DELETE FROM scrape_jobs WHERE source_id = ?", (source_id,))

            # Only delete the source itself if ALL docs were cleaned up.
            if not failed_locations:
                db.execute(
                    "DELETE FROM scrape_sources WHERE id = ? AND workspace = ?",
                    (source_id, workspace),
                )

    if failed_locations:
        return jsonify({
            "message": "Source partially deleted",
            "warning": (
                f"{len(failed_locations)} document(s) could not be removed "
                f"from AnythingLLM and were retained for retry"
            ),
        }), 207

    return jsonify({"message": "Source and its documents deleted"})


@app.route("/<workspace>/scrape-sources/<int:source_id>/run", methods=["POST"])
def trigger_scrape(workspace, source_id):
    """Queue an on-demand scrape job for a source."""
    with open_db() as db:
        existing = db.execute(
            "SELECT * FROM scrape_sources WHERE id = ? AND workspace = ?",
            (source_id, workspace),
        ).fetchone()
        if not existing:
            return jsonify({"error": "Source not found"}), 404

        active = db.execute(
            "SELECT id FROM scrape_jobs WHERE source_id = ? AND status IN ('pending', 'running')",
            (source_id,),
        ).fetchone()
        if active:
            return jsonify(
                {"error": "A scrape job is already in progress for this source"}
            ), 409

        with db:
            cur = db.execute(
                "INSERT INTO scrape_jobs (source_id, status) VALUES (?, 'pending')",
                (source_id,),
            )
            job_id = cur.lastrowid

    # Signal the scraper worker to wake up and process this job immediately
    # instead of waiting for the next poll cycle.
    try:
        with open(WAKE_SIGNAL_PATH, "w") as f:
            f.write(str(job_id))
    except OSError:
        log.warning("Could not write scraper wake signal file")

    return jsonify({"job_id": job_id, "message": "Scrape job queued"}), 201


# ===================================================================
#  Entry point
# ===================================================================

if __name__ == "__main__":
    init_db()
    # NOTE: debug=True exposes the Werkzeug interactive debugger which allows
    # arbitrary code execution.  Never use debug=True in production -- set the
    # FLASK_DEBUG environment variable instead during local development.
    debug = os.getenv("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=3000, debug=debug)