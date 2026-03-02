"""
Background scraper process for the Knowledge Base application.

Runs alongside app.py as a separate long-lived process.  It shares the same
SQLite database and polls for pending ``scrape_jobs`` rows.  When a job is
found it performs a breadth-first crawl of the configured website, extracts
readable content with *BeautifulSoup* and converts it to markdown via
*markdownify* (preserving all links), and uploads each page to AnythingLLM.

APScheduler runs in the background and creates new jobs for sources that
have a recurring schedule (daily / weekly / monthly).

Usage:
    python scraper.py

Graceful shutdown:
    Send SIGINT (Ctrl-C) or SIGTERM.  The current crawl will finish its
    in-progress page, then the process exits cleanly.
"""

import copy
import hashlib
import json
import logging
import os
import re
import signal
import sys
import time
from collections import deque
from datetime import datetime
from typing import Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

import requests
import markdownify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from bs4 import BeautifulSoup

from anythingllm import embed_document, remove_document, upload_document
from config import (
    API_URL,
    API_KEY,
    DEBUG_UPLOAD_DIR,
    JOB_POLL_INTERVAL,
    MAX_PAGES_PER_JOB,
    REQUEST_DELAY,
    REQUEST_TIMEOUT,
    SCRAPER_USER_AGENT,
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
# Global flag set by the signal handler so loops can exit cleanly.
# ---------------------------------------------------------------------------
_shutdown_requested = False

# ---------------------------------------------------------------------------
# Debug upload interception (shared with app.py via the same env var).
# ---------------------------------------------------------------------------
if DEBUG_UPLOAD_DIR:
    os.makedirs(DEBUG_UPLOAD_DIR, exist_ok=True)
    log.info("Debug upload interception ENABLED -> %s", DEBUG_UPLOAD_DIR)


def _debug_save_file(filename: str, content: bytes) -> None:
    """Save a copy of *content* to DEBUG_UPLOAD_DIR if debugging is enabled."""
    if not DEBUG_UPLOAD_DIR:
        return
    dest = os.path.join(DEBUG_UPLOAD_DIR, filename)
    if os.path.exists(dest):
        base, ext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(dest):
            dest = os.path.join(DEBUG_UPLOAD_DIR, f"{base}_{counter}{ext}")
            counter += 1
    try:
        with open(dest, "wb") as f:
            f.write(content)
        log.info("Debug: saved scraped upload to %s (%d bytes)", dest, len(content))
    except OSError:
        log.warning("Debug: failed to save scraped upload to %s", dest)


def _check_wake_signal() -> bool:
    """Return True and delete the signal file if the Flask app requested an
    immediate poll (e.g. a user clicked "Scrape Now")."""
    try:
        if os.path.exists(WAKE_SIGNAL_PATH):
            os.remove(WAKE_SIGNAL_PATH)
            return True
    except OSError:
        pass
    return False


# File extensions that should be skipped when discovering links -- these are
# binary resources that aren't useful web pages.
_SKIP_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".zip",
        ".tar",
        ".gz",
        ".rar",
        ".7z",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".svg",
        ".ico",
        ".webp",
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".wmv",
        ".css",
        ".js",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
    }
)


# ===================================================================
#  URL helpers
# ===================================================================


def _normalize_url(url: str) -> str:
    """Normalize *url* by stripping fragments and trailing slashes."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(
        (parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, "")
    )


def _is_same_domain(url: str, base_url: str) -> bool:
    return urlparse(url).netloc == urlparse(base_url).netloc


def _url_to_filename(url: str, title: Optional[str] = None) -> str:
    """Derive a safe ``.md`` filename from a URL (and optional page title)."""
    parsed = urlparse(url)
    path_part = parsed.path.strip("/").replace("/", "_") or "index"
    if parsed.query:
        path_part += "_" + parsed.query[:50]
    # Remove non-alphanumeric characters and collapse underscores.
    path_part = re.sub(r"[^a-zA-Z0-9_\-.]", "_", path_part)
    path_part = re.sub(r"_+", "_", path_part).strip("_")
    # Short hash for uniqueness.
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:8]
    if title:
        safe_title = re.sub(r"[^a-zA-Z0-9_\-. ]", "", title)[:60].strip()
        if safe_title:
            return f"{safe_title}_{url_hash}.md"
    return f"{path_part}_{url_hash}.md"


# ===================================================================
#  Link extraction and robots.txt
# ===================================================================


def _extract_links(html: str, base_url: str) -> set[str]:
    """Return same-domain page links found in *html*."""
    soup = BeautifulSoup(html, "html.parser")
    links: set[str] = set()
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        normalized = _normalize_url(absolute)
        if not _is_same_domain(normalized, base_url):
            continue
        # Skip binary / non-page resources.
        if any(
            urlparse(normalized).path.lower().endswith(ext) for ext in _SKIP_EXTENSIONS
        ):
            continue
        links.add(normalized)
    return links


def _fetch_robots_disallowed(base_url: str) -> set[str]:
    """Return the set of disallowed path prefixes from ``robots.txt``."""
    disallowed: set[str] = set()
    try:
        parsed = urlparse(base_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        resp = requests.get(
            robots_url, timeout=10, headers={"User-Agent": SCRAPER_USER_AGENT}
        )
        if resp.status_code != 200:
            return disallowed
        applies = False
        for line in resp.text.splitlines():
            line = line.strip()
            if line.lower().startswith("user-agent:"):
                agent = line.split(":", 1)[1].strip().lower()
                applies = agent == "*" or "knowledgebase" in agent
            elif applies and line.lower().startswith("disallow:"):
                path = line.split(":", 1)[1].strip()
                if path:
                    disallowed.add(path)
    except Exception:
        pass  # Unable to fetch robots.txt -- proceed without restrictions.
    return disallowed


def _is_path_allowed(url: str, disallowed: set[str]) -> bool:
    path = urlparse(url).path
    return not any(path.startswith(d) for d in disallowed)


# ===================================================================
#  Page fetching and content extraction
# ===================================================================


def _fetch_page(url: str) -> Tuple[Optional[str], Optional[str]]:
    """GET *url* and return ``(html, None)`` or ``(None, error)``."""
    try:
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={
                "User-Agent": SCRAPER_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
            allow_redirects=True,
        )
        ct = resp.headers.get("Content-Type", "")
        if "text/html" not in ct and "xhtml" not in ct:
            return None, f"Not HTML: {ct}"
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        return resp.text, None
    except requests.RequestException as exc:
        return None, str(exc)


def _extract_content(html: str, url: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract page content using BeautifulSoup + markdownify.

    Finds the main content container, strips junk elements, resolves
    relative URLs to absolute, converts to markdown (preserving all links),
    and appends a "Related Pages" section with nav/sidebar/breadcrumb links.

    Returns ``(title, markdown)`` or ``(None, None)`` when the page has
    insufficient text.
    """
    soup = BeautifulSoup(html, "html.parser")

    # --- Step A: Find the content container via fallback chain ----------
    container = None
    _selectors = [
        lambda s: s.select_one(".entry-content"),  # WordPress standard
        lambda s: s.select_one("[role='main']"),  # ARIA main landmark
        lambda s: s.find("main"),  # HTML5 <main>
        lambda s: s.find("article"),  # <article>
        lambda s: s.find("body"),  # last resort
    ]
    for selector_fn in _selectors:
        candidate = selector_fn(soup)
        if candidate and len(candidate.get_text(strip=True)) > 50:
            container = candidate
            break

    if container is None:
        log.warning("No suitable content container found for %s", url)
        return None, None

    # --- Step B: Clean junk elements from a copy of the container ------
    cleaned = copy.copy(container)

    # Tags that are always noise.
    for tag_name in ("script", "style", "noscript", "iframe"):
        for el in cleaned.find_all(tag_name):
            el.decompose()

    # Top-level page chrome (NOT heading tags h1-h6).
    for tag_name in ("header", "footer"):
        for el in cleaned.find_all(tag_name):
            el.decompose()

    # Elements whose class or id match common junk patterns.
    _junk_patterns = ("cookie", "popup", "modal", "advertisement", "ad-", "banner")
    for el in cleaned.find_all(True):
        classes = " ".join(el.get("class", [])).lower()
        el_id = (el.get("id") or "").lower()
        if any(pat in classes or pat in el_id for pat in _junk_patterns):
            el.decompose()

    # --- Step C: Resolve relative URLs to absolute ---------------------
    for a_tag in cleaned.find_all("a", href=True):
        href = a_tag["href"].strip()
        if href and not href.startswith(("#", "mailto:", "tel:", "javascript:")):
            a_tag["href"] = urljoin(url, href)

    # --- Step D: Convert to markdown -----------------------------------
    main_markdown = markdownify.markdownify(
        str(cleaned),
        heading_style="ATX",
        strip=["img"],
    )
    # Collapse excessive blank lines (3+ newlines → 2).
    main_markdown = re.sub(r"\n{3,}", "\n\n", main_markdown).strip()

    # Minimum length check on plain text (strip markdown formatting).
    plain_text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", main_markdown)  # links
    plain_text = re.sub(r"[#*_`\-\|>\[\]]", "", plain_text)  # formatting chars
    plain_text = plain_text.strip()
    if len(plain_text) < 50:
        log.warning(
            "Extracted content too short for %s (%d chars)", url, len(plain_text)
        )
        return None, None

    # --- Step E: Extract the page title --------------------------------
    title = None
    title_tag = soup.find("title")
    if title_tag and title_tag.get_text(strip=True):
        title = title_tag.get_text(strip=True)
    else:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True) or None

    # Strip common site-name suffixes (e.g. " | University of Rhode Island").
    if title:
        for sep in (" – ", " | ", " — "):
            if sep in title:
                title = title.split(sep)[0].strip()
                break

    # --- Step F: Extract supplementary navigation links ----------------
    # Collect absolute URLs already in the main markdown for dedup.
    main_links = set(re.findall(r"\]\((https?://[^)]+)\)", main_markdown))

    nav_links: list[tuple[str, str]] = []  # (text, absolute_url)
    seen_urls: set[str] = set()

    # Gather navigation containers from the ORIGINAL soup.
    nav_containers = []
    nav_containers.extend(soup.find_all("nav"))
    for selector in [
        ".sidebar",
        "#sidebar",
        "aside",
        ".cl-menu",
        ".localnav",
        "#localnav",
        ".subnav",
        "#subnav",
    ]:
        if selector.startswith("."):
            nav_containers.extend(soup.find_all(class_=selector.lstrip(".")))
        elif selector.startswith("#"):
            found = soup.find(id=selector.lstrip("#"))
            if found:
                nav_containers.append(found)
        else:
            nav_containers.extend(soup.find_all(selector))
    for el in soup.find_all(class_="breadcrumb"):
        nav_containers.append(el)

    for nav_container in nav_containers:
        for a_tag in nav_container.find_all("a", href=True):
            href = a_tag["href"].strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            absolute = urljoin(url, href)
            if not absolute.startswith(("http://", "https://")):
                continue
            parsed_href = urlparse(absolute)
            if any(parsed_href.path.lower().endswith(ext) for ext in _SKIP_EXTENSIONS):
                continue
            normalized = _normalize_url(absolute)
            if normalized in seen_urls or normalized in main_links:
                continue
            seen_urls.add(normalized)
            link_text = a_tag.get_text(strip=True)
            if not link_text:
                continue
            nav_links.append((link_text, normalized))

    # --- Step G: Assemble final document -------------------------------
    parts = []
    if title:
        parts.append(f"# {title}\n")
    parts.append(f"Source: {url}\n")
    parts.append("---\n")
    parts.append(main_markdown)

    if nav_links:
        parts.append("\n\n## Related Pages\n")
        for text, link_url in nav_links:
            parts.append(f"- [{text}]({link_url})")

    return title, "\n".join(parts)


def _upload_and_embed(content: str, filename: str, workspace: str):
    """Upload Markdown to AnythingLLM and embed it.

    Returns ``(location, None)`` on success or ``(None, error)`` on failure.
    """
    content_bytes = content.encode("utf-8")
    _debug_save_file(filename, content_bytes)
    location, err = upload_document(filename, content_bytes, "text/markdown")
    if err or not location:
        return None, f"Upload failed: {err}"
    ok, embed_err = embed_document(workspace, location)
    if not ok:
        return None, embed_err or "Embedding failed"
    return location, None


# ===================================================================
#  Crawl engine
# ===================================================================


def _clear_source_documents(workspace: str, source_id: int, job_id: int):
    """Remove all existing scraped documents for a source from AnythingLLM
    and the local database.  Called at the start of every scrape so the
    source's documents are always a fresh snapshot of the site."""
    with open_db() as db:
        docs = db.execute(
            "SELECT id, location FROM documents "
            "WHERE source_id = ? AND source_type = 'scrape'",
            (source_id,),
        ).fetchall()

        if not docs:
            return

        log.info(
            "[Job %s] Clearing %d existing document(s) before re-scrape",
            job_id,
            len(docs),
        )

        for doc in docs:
            ok, err = remove_document(workspace, doc["location"])
            if not ok:
                log.warning(
                    "[Job %s] Failed to remove doc %s from AnythingLLM: %s",
                    job_id,
                    doc["location"],
                    err,
                )

        with db:
            db.execute(
                "DELETE FROM documents WHERE source_id = ? AND source_type = 'scrape'",
                (source_id,),
            )


def _crawl_source(source: dict, job_id: int) -> tuple[int, int]:
    """Breadth-first crawl of a scrape source.

    Deletes all existing documents for this source first, then discovers
    links up to ``max_depth`` levels from the seed URL, extracts readable
    content, and uploads each page to AnythingLLM.

    Returns ``(pages_found, pages_scraped)``.
    """
    workspace = source["workspace"]
    seed_url = _normalize_url(source["url"])
    max_depth = source["max_depth"]
    category = source["category"]
    source_id = source["id"]

    log.info(
        "[Job %s] Starting crawl of %s (depth=%s, category=%s)",
        job_id,
        seed_url,
        max_depth,
        category,
    )

    # --- Delete all existing documents for this source before re-scraping ---
    _clear_source_documents(workspace, source_id, job_id)

    disallowed = _fetch_robots_disallowed(seed_url)

    # BFS queue: (url, depth).  Using deque for efficient popleft().
    queue: deque[tuple[str, int]] = deque([(seed_url, 0)])
    visited: set[str] = set()
    pages_found = 0
    pages_scraped = 0

    # Keep a single DB connection for progress updates inside the loop to
    # avoid the overhead of opening/closing on every page.
    with open_db() as db:
        while queue and pages_found < MAX_PAGES_PER_JOB and not _shutdown_requested:
            url, depth = queue.popleft()

            if url in visited:
                continue
            visited.add(url)

            if not _is_path_allowed(url, disallowed):
                log.debug("[Job %s] Skipping (robots.txt): %s", job_id, url)
                continue

            # Polite delay between requests.
            if pages_found > 0:
                time.sleep(REQUEST_DELAY)

            html, err = _fetch_page(url)
            if err or not html:
                log.debug("[Job %s] Fetch failed %s: %s", job_id, url, err)
                continue

            pages_found += 1
            with db:
                db.execute(
                    "UPDATE scrape_jobs SET pages_found = ? WHERE id = ?",
                    (pages_found, job_id),
                )

            # Discover links for the next depth level.
            if depth < max_depth - 1:
                for link in _extract_links(html, url):
                    if link not in visited:
                        queue.append((link, depth + 1))

            title, markdown = _extract_content(html, url)
            if not markdown:
                log.debug("[Job %s] No extractable content: %s", job_id, url)
                continue

            filename = _url_to_filename(url, title)
            location, upload_err = _upload_and_embed(markdown, filename, workspace)
            if upload_err:
                log.warning(
                    "[Job %s] Upload failed for %s: %s", job_id, url, upload_err
                )
                continue

            with db:
                db.execute(
                    "INSERT OR REPLACE INTO documents "
                    "(workspace, filename, location, source_type, "
                    "source_id, source_url, title, category, depth) "
                    "VALUES (?, ?, ?, 'scrape', ?, ?, ?, ?, ?)",
                    (
                        workspace,
                        filename,
                        location,
                        source_id,
                        url,
                        title,
                        category,
                        depth,
                    ),
                )

            pages_scraped += 1
            with db:
                db.execute(
                    "UPDATE scrape_jobs SET pages_scraped = ? WHERE id = ?",
                    (pages_scraped, job_id),
                )
            log.info("[Job %s] Scraped (%d): %s", job_id, pages_scraped, title or url)

    return pages_found, pages_scraped


def _crawl_source_prefix(source: dict, job_id: int) -> tuple[int, int]:
    """Prefix-based crawl of a scrape source.

    Deletes all existing documents for this source first, then starting
    from the seed URL, follows only links whose path starts with one of
    the allowed prefixes AND whose domain matches the seed URL.  Stops
    when all reachable in-scope pages are visited or ``max_pages``
    is reached.

    Returns ``(pages_found, pages_scraped)``.
    """
    workspace = source["workspace"]
    seed_url = _normalize_url(source["url"])
    category = source["category"]
    source_id = source["id"]

    # Parse allowed prefixes from JSON string.
    allowed_prefixes_raw = source.get("allowed_prefixes") or "[]"
    allowed_prefixes = (
        json.loads(allowed_prefixes_raw)
        if isinstance(allowed_prefixes_raw, str)
        else allowed_prefixes_raw
    )
    max_pages = source.get("max_pages") or MAX_PAGES_PER_JOB

    log.info(
        "[Job %s] Starting prefix crawl of %s (prefixes=%s, max_pages=%s, category=%s)",
        job_id,
        seed_url,
        allowed_prefixes,
        max_pages,
        category,
    )

    # --- Delete all existing documents for this source before re-scraping ---
    _clear_source_documents(workspace, source_id, job_id)

    disallowed = _fetch_robots_disallowed(seed_url)

    # BFS queue -- no depth tracking needed in prefix mode.
    queue: deque[str] = deque([seed_url])
    visited: set[str] = set()
    pages_found = 0
    pages_scraped = 0

    seed_domain = urlparse(seed_url).netloc

    def _is_in_prefix_scope(url: str) -> bool:
        """Return True if *url* is on the seed domain and its path starts
        with at least one of the allowed prefixes."""
        parsed = urlparse(url)
        if parsed.netloc != seed_domain:
            return False
        path = parsed.path
        return any(path.startswith(prefix) for prefix in allowed_prefixes)

    with open_db() as db:
        while queue and pages_found < max_pages and not _shutdown_requested:
            url = queue.popleft()

            if url in visited:
                continue
            visited.add(url)

            if not _is_path_allowed(url, disallowed):
                log.debug("[Job %s] Skipping (robots.txt): %s", job_id, url)
                continue

            # Polite delay between requests.
            if pages_found > 0:
                time.sleep(REQUEST_DELAY)

            html, err = _fetch_page(url)
            if err or not html:
                log.debug("[Job %s] Fetch failed %s: %s", job_id, url, err)
                continue

            pages_found += 1
            with db:
                db.execute(
                    "UPDATE scrape_jobs SET pages_found = ? WHERE id = ?",
                    (pages_found, job_id),
                )

            # Discover all same-domain links, then filter by prefix scope.
            for link in _extract_links(html, url):
                if link not in visited and _is_in_prefix_scope(link):
                    queue.append(link)

            title, markdown = _extract_content(html, url)
            if not markdown:
                log.debug("[Job %s] No extractable content: %s", job_id, url)
                continue

            filename = _url_to_filename(url, title)
            location, upload_err = _upload_and_embed(markdown, filename, workspace)
            if upload_err:
                log.warning(
                    "[Job %s] Upload failed for %s: %s", job_id, url, upload_err
                )
                continue

            with db:
                db.execute(
                    "INSERT OR REPLACE INTO documents "
                    "(workspace, filename, location, source_type, "
                    "source_id, source_url, title, category, depth) "
                    "VALUES (?, ?, ?, 'scrape', ?, ?, ?, ?, ?)",
                    (
                        workspace,
                        filename,
                        location,
                        source_id,
                        url,
                        title,
                        category,
                        0,  # depth is not meaningful in prefix mode
                    ),
                )

            pages_scraped += 1
            with db:
                db.execute(
                    "UPDATE scrape_jobs SET pages_scraped = ? WHERE id = ?",
                    (pages_scraped, job_id),
                )
            log.info("[Job %s] Scraped (%d): %s", job_id, pages_scraped, title or url)

    return pages_found, pages_scraped


# ===================================================================
#  Job processing
# ===================================================================


def _process_job(job_id: int):
    """Load and execute a single pending scrape job."""
    with open_db() as db:
        job = db.execute("SELECT * FROM scrape_jobs WHERE id = ?", (job_id,)).fetchone()
        if not job or job["status"] != "pending":
            return

        source = db.execute(
            "SELECT * FROM scrape_sources WHERE id = ?", (job["source_id"],)
        ).fetchone()
        if not source:
            with db:
                db.execute(
                    "UPDATE scrape_jobs SET status='failed', "
                    "error='Source not found', completed_at=CURRENT_TIMESTAMP "
                    "WHERE id=?",
                    (job_id,),
                )
            return

        # Mark running.
        with db:
            db.execute(
                "UPDATE scrape_jobs SET status='running', started_at=CURRENT_TIMESTAMP "
                "WHERE id=?",
                (job_id,),
            )

    try:
        source_dict = dict(source)
        crawl_mode = source_dict.get("crawl_mode", "depth")
        if crawl_mode == "prefix":
            pages_found, pages_scraped = _crawl_source_prefix(source_dict, job_id)
        else:
            pages_found, pages_scraped = _crawl_source(source_dict, job_id)

        with open_db() as db:
            with db:
                db.execute(
                    "UPDATE scrape_jobs SET status='completed', "
                    "completed_at=CURRENT_TIMESTAMP, pages_found=?, pages_scraped=? "
                    "WHERE id=?",
                    (pages_found, pages_scraped, job_id),
                )
                db.execute(
                    "UPDATE scrape_sources SET last_scraped_at=CURRENT_TIMESTAMP "
                    "WHERE id=?",
                    (source["id"],),
                )
        log.info(
            "[Job %s] Completed: %d found, %d scraped",
            job_id,
            pages_found,
            pages_scraped,
        )

    except Exception:
        log.exception("[Job %s] Failed", job_id)
        with open_db() as db:
            with db:
                db.execute(
                    "UPDATE scrape_jobs SET status='failed', error=?, "
                    "completed_at=CURRENT_TIMESTAMP WHERE id=?",
                    (str(sys.exc_info()[1])[:500], job_id),
                )


def _poll_for_jobs():
    """Check for pending jobs and process them sequentially."""
    if _shutdown_requested:
        return
    with open_db() as db:
        pending = db.execute(
            "SELECT id FROM scrape_jobs WHERE status='pending' "
            "ORDER BY requested_at ASC"
        ).fetchall()
    for row in pending:
        if _shutdown_requested:
            break
        _process_job(row["id"])


def _reset_stale_jobs():
    """Reset jobs stuck in 'running' from a previous crash back to 'pending'."""
    with open_db() as db:
        with db:
            count = db.execute(
                "UPDATE scrape_jobs SET status='pending', started_at=NULL "
                "WHERE status='running'"
            ).rowcount
            if count:
                log.info("Reset %d stale job(s) from 'running' to 'pending'", count)


def _schedule_recurring_jobs():
    """Create pending jobs for sources that are due based on their schedule."""
    with open_db() as db:
        sources = db.execute(
            "SELECT * FROM scrape_sources WHERE enabled=1 AND schedule IS NOT NULL"
        ).fetchall()

        for source in sources:
            # Skip if there's already an active job for this source.
            active = db.execute(
                "SELECT id FROM scrape_jobs WHERE source_id=? "
                "AND status IN ('pending','running')",
                (source["id"],),
            ).fetchone()
            if active:
                continue

            # Check minimum elapsed time since last scrape.
            last = source["last_scraped_at"]
            if last:
                last_dt = (
                    datetime.fromisoformat(last) if isinstance(last, str) else last
                )
                elapsed_hours = (datetime.now() - last_dt).total_seconds() / 3600
                min_hours = {"daily": 23, "weekly": 167, "monthly": 719}
                if elapsed_hours < min_hours.get(source["schedule"], 23):
                    continue

            with db:
                db.execute(
                    "INSERT INTO scrape_jobs (source_id, status) VALUES (?, 'pending')",
                    (source["id"],),
                )
                log.info(
                    "Scheduled recurring job for source %s (%s)",
                    source["id"],
                    source["url"],
                )


# ===================================================================
#  Signal handling and entry point
# ===================================================================


def _signal_handler(_signum, _frame):
    global _shutdown_requested
    log.info("Shutdown signal received -- finishing current work...")
    _shutdown_requested = True


def main():
    global _shutdown_requested

    log.info("=" * 60)
    log.info("Knowledge Base Scraper")
    log.info("AnythingLLM API: %s", API_URL)
    log.info(
        "Poll interval: %ds  |  Request delay: %.1fs", JOB_POLL_INTERVAL, REQUEST_DELAY
    )
    log.info("=" * 60)

    if not API_URL or not API_KEY:
        log.error("AnythingLLM_API_URL and AnythingLLM_API_Key must be set in .env")
        sys.exit(1)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    init_db()
    _reset_stale_jobs()
    _check_wake_signal()  # Clear any stale signal from a previous run

    # APScheduler checks for due recurring sources every 5 minutes.
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _schedule_recurring_jobs,
        CronTrigger(minute="*/5"),
        id="schedule_check",
        name="Check for recurring scrape schedules",
    )
    scheduler.start()
    log.info("Scheduler started.  Polling for jobs...")

    # Also run a schedule check immediately on startup.
    _schedule_recurring_jobs()

    try:
        while not _shutdown_requested:
            _poll_for_jobs()
            # Sleep in 1-second increments so we respond to signals quickly.
            # Also check for a wake signal from the Flask app (e.g. user
            # clicked "Scrape Now") so we can process the job immediately
            # instead of waiting for the full poll interval.
            for _ in range(JOB_POLL_INTERVAL):
                if _shutdown_requested:
                    break
                time.sleep(1)
                if _check_wake_signal():
                    log.info("Wake signal received -- polling for jobs immediately")
                    break
    finally:
        log.info("Shutting down scheduler...")
        scheduler.shutdown(wait=False)
        log.info("Scraper stopped.")


if __name__ == "__main__":
    main()
