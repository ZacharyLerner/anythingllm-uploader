# Knowledge Base Upload

A document management front-end for [AnythingLLM](https://anythingllm.com/).
Users upload files or configure website scrape sources through a web UI, and the
application handles conversion, storage, and embedding into an AnythingLLM
workspace.  External systems can also add documents programmatically via a JSON
API.

The project has **two processes** that share a single SQLite database:

| Process | Purpose |
|---------|---------|
| `app.py` | Flask web server (port 3000). Serves the UI, handles file uploads with optional Docling conversion, and provides REST endpoints for document and scrape-source management. |
| `scraper.py` | Long-running background worker. Polls for pending scrape jobs, performs breadth-first web crawls, extracts readable content with BeautifulSoup and converts it to Markdown via markdownify, and uploads each page to AnythingLLM. APScheduler handles recurring schedules. |

### Module layout

```
.
├── app.py              # Flask web server & API routes
├── scraper.py          # Background scrape worker
├── config.py           # Shared configuration (env vars, constants)
├── db.py               # Shared database connection & schema
├── anythingllm.py      # Shared AnythingLLM API helper functions
├── templates/
│   ├── index.html      # Main single-page UI
│   └── 404.html        # Workspace-not-found page
├── requirements.txt    # Python dependencies
├── .env.example        # Template for environment variables
└── .gitignore
```

## Quick start

### 1. Prerequisites

- Python 3.11+
- A running AnythingLLM instance with API access enabled

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

> **Note:** The `docling` package and its dependencies (`onnxruntime`,
> `opencv-python-headless`) are optional.  If not installed, uploaded files are
> sent to AnythingLLM in their original format (AnythingLLM has its own
> parsers).  When Docling *is* available, PDFs and Office documents are
> converted to Markdown first for better extraction quality.

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env with your AnythingLLM API URL and key
```

| Variable | Description |
|----------|-------------|
| `AnythingLLM_API_URL` | Base URL of the AnythingLLM API, e.g. `http://localhost:3001/api/v1` |
| `AnythingLLM_API_Key` | Bearer token for API authentication |
| `APP_API_KEY` | Optional. When set, external API callers must include this as a `Bearer` token. Leave empty to disable. |
| `DEBUG_UPLOAD_DIR` | Optional. Directory path to save copies of all files before sending to AnythingLLM. Useful for debugging. Leave empty to disable. |

### 4. Run the web server

```bash
python app.py
```

Open `http://localhost:3000/<workspace-slug>` in a browser, where
`<workspace-slug>` matches a workspace in your AnythingLLM instance.

To enable Flask debug mode during development:

```bash
FLASK_DEBUG=true python app.py
```

### 5. Run the scraper (optional)

If you want web-scrape functionality, start the scraper in a separate terminal:

```bash
python scraper.py
```

The scraper polls for pending jobs every 10 seconds and checks recurring
schedules every 5 minutes.  Send `SIGINT` (Ctrl-C) or `SIGTERM` for a
graceful shutdown.

## API reference

All routes are prefixed with `/<workspace>`.

### Web UI

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/<workspace>` | Serve the upload UI (or 404 if workspace is invalid) |
| `POST` | `/<workspace>/upload` | Upload a file via the browser. Returns an SSE stream: `converting` -> `uploading` -> `embedding` -> `complete`. Supports `?replace=true`. |

### Documents (unified)

All documents -- uploaded and scraped -- live in a single table with a
`source_type` field (`upload` or `scrape`).

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/<workspace>/documents` | List documents. Filter with `?source_type=upload\|scrape` and/or `?category=`. |
| `POST` | `/<workspace>/documents` | **API upload.** Multipart file upload with optional form fields (see below). Requires `APP_API_KEY` when set. |
| `DELETE` | `/<workspace>/documents/<id>` | Delete a single document by id. |
| `DELETE` | `/<workspace>/documents/batch` | Batch delete. Body: `{"ids": [1, 2, 3]}` |

### Scrape sources

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/<workspace>/scrape-sources` | List sources with latest job status and document count. |
| `POST` | `/<workspace>/scrape-sources` | Create a source. Body: `{url, category, crawl_mode, max_depth, schedule, allowed_prefixes, max_pages}` |
| `PUT` | `/<workspace>/scrape-sources/<id>` | Update a source (partial updates supported). |
| `DELETE` | `/<workspace>/scrape-sources/<id>` | Delete a source and all its scraped documents. |
| `POST` | `/<workspace>/scrape-sources/<id>/run` | Queue an on-demand scrape job. |

## Using the API

External systems can add documents to a workspace by calling
`POST /<workspace>/documents`.  When `APP_API_KEY` is set in `.env`, include
it as a bearer token.

The `source_type` form field controls which section of the UI the document
appears in:

- `upload` (default) -- appears in the **Uploaded Documents** section.
- `scrape` -- appears in the **Scraped Documents** section.  Requires
  `source_url` so the UI can display the document's origin.

### Upload a document

Uploads a file that appears in the Uploaded Documents section.

```bash
curl -X POST http://localhost:3000/my-workspace/documents \
  -H "Authorization: Bearer YOUR_APP_API_KEY" \
  -F "file=@report.pdf"
```

Response:

```json
{
  "id": 42,
  "filename": "report.pdf",
  "location": "custom-documents/report.md-abc123.json",
  "source_type": "upload",
  "converted": true
}
```

If Docling is installed, binary formats (PDF, DOCX, etc.) are automatically
converted to Markdown before upload.  Text formats (`.txt`, `.md`, `.csv`,
`.json`, `.xml`) are uploaded as-is.

### Upload a web-sourced document

Uploads a file that appears in the Scraped Documents section alongside
crawler results.  Useful for KB articles or pages that can't be auto-scraped.

```bash
curl -X POST http://localhost:3000/my-workspace/documents \
  -H "Authorization: Bearer YOUR_APP_API_KEY" \
  -F "file=@kb-article.md" \
  -F "source_type=scrape" \
  -F "source_url=https://help.example.com/article/123" \
  -F "title=Password Reset Guide" \
  -F "category=IT Support"
```

Response:

```json
{
  "id": 43,
  "filename": "kb-article.md",
  "location": "custom-documents/kb-article.md-def456.json",
  "source_type": "scrape",
  "converted": false,
  "source_url": "https://help.example.com/article/123",
  "title": "Password Reset Guide",
  "category": "IT Support"
}
```

### Form fields reference

| Field | Required | Description |
|-------|----------|-------------|
| `file` | Yes | The file to upload. |
| `source_type` | No | `upload` (default) or `scrape`. Controls which UI section the document appears in. |
| `source_url` | When `scrape` | The original URL of the content. Displayed in the scraped documents list. |
| `title` | No | Document title. Displayed in the scraped documents section. |
| `category` | No | Freeform label for filtering (e.g. "IT Support", "HR Policies"). |

### List documents

```bash
# All documents
curl http://localhost:3000/my-workspace/documents

# Only uploaded documents
curl http://localhost:3000/my-workspace/documents?source_type=upload

# Only scraped documents
curl http://localhost:3000/my-workspace/documents?source_type=scrape

# Filter by category
curl http://localhost:3000/my-workspace/documents?category=IT+Support
```

### Delete documents

```bash
# Single document
curl -X DELETE http://localhost:3000/my-workspace/documents/42 \
  -H "Authorization: Bearer YOUR_APP_API_KEY"

# Batch delete
curl -X DELETE http://localhost:3000/my-workspace/documents/batch \
  -H "Authorization: Bearer YOUR_APP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"ids": [42, 43, 44]}'
```

### Error responses

All errors return JSON with an `error` field:

```json
{"error": "A document with this filename already exists"}
```

| Status | Meaning |
|--------|---------|
| `400` | Missing or invalid request data. |
| `401` | `APP_API_KEY` is set and the request is missing or has an invalid bearer token. |
| `404` | Workspace or document not found. |
| `409` | Duplicate filename in the workspace. |
| `413` | File exceeds 100 MB limit. |
| `502` | AnythingLLM backend error (upload or embedding failed). |

## Database

SQLite with WAL mode (`documents.db`).  Three tables:

- **`documents`** -- Unified document store.  Every document has a `source_type`
  (`upload` or `scrape`) and optional fields for scrape provenance
  (`source_id`, `source_url`, `category`, `depth`).
- **`scrape_sources`** -- Website URLs to crawl, with crawl mode, depth/prefix
  config, and schedule.
- **`scrape_jobs`** -- Crawl execution log (pending -> running -> completed/failed).

The schema is defined in `db.py` and auto-created on first run.

## Security notes

- **Authentication:** The web UI has no built-in authentication.  It should be
  deployed behind a reverse proxy (e.g. nginx) with access control.  The API
  endpoints support optional bearer-token auth via `APP_API_KEY`.
- **Debug mode:** Never enable `FLASK_DEBUG=true` in production -- the Werkzeug
  debugger allows arbitrary code execution.
- **Secrets:** Keep your `.env` file out of version control (it is in
  `.gitignore`).
- **SSRF risk:** Scrape sources accept arbitrary URLs.  In sensitive
  environments, consider adding URL allowlists or blocking private IP ranges.
