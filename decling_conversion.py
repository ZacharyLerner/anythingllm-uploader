import re
from bs4 import BeautifulSoup
from curl_cffi import requests as browser_requests

from docling.document_converter import DocumentConverter
from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import DocumentStream

from io import BytesIO
import time

converter = DocumentConverter()

# Fetch pages with a real browser's TLS/HTTP fingerprint and headers. Many sites
# (Cloudflare, Akamai, Varnish) reject Python's default client outright.
BROWSER_IMPERSONATE = "chrome"
_NOISE_TAGS = ["header", "footer", "nav", "script", "style", "noscript", "aside"]

# Real breadcrumb classes ("breadcrumb", "breadcrumbs", "usda-breadcrumb-list"), but not
# layout modifiers like "with-breadcrumb" that some sites put on the main content area.
_BREADCRUMB_CLASS_RE = re.compile(r"^(?!(?:with|has)-).*breadcrumb", re.I)
_LOADER_RE = re.compile(r"loader|spinner", re.I)


def _is_pdf(response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    return "application/pdf" in content_type or response.content[:5] == b"%PDF-"


def _word_count(el) -> int:
    return len(el.get_text(" ", strip=True).split())


def _wraps_page_content(el, page_words: int) -> bool:
    """True if removing `el` would take the page's main content with it."""
    if el.name in ("html", "body", "main", "article"):
        return True
    if el.find(["main", "article"]):
        return True
    return page_words > 0 and _word_count(el) > page_words * 0.5


def scrape_website_md(url):
    try:
        response = browser_requests.get(url, impersonate=BROWSER_IMPERSONATE, timeout=15)
    except Exception as e:
        raise ValueError(f"Request failed: {e}") from e
    if response.status_code >= 400:
        raise ValueError(f"Request failed: HTTP {response.status_code} from {url}")

    if _is_pdf(response):
        try:
            md = convert_file(response.content, "page.pdf")
        except Exception as e:
            raise ValueError(f"Could not convert PDF: {e}") from e
        if not md or not md.strip():
            raise ValueError("PDF converted to empty content")
        return _clean_markdown(md)

    # Pass bytes so BeautifulSoup picks the charset from the page itself
    soup = BeautifulSoup(response.content, "html.parser")

    # If the page is essentially empty (JS redirect, login wall, etc.)
    # there is nothing to convert — bail early with a clear message.
    if len(soup.get_text(strip=True)) < 50:
        raise ValueError("Page returned no usable content (may require login or redirect)")

    # Strip noise tags (header/footer/nav + scripts/styles)
    for tag in soup.find_all(_NOISE_TAGS):
        tag.extract()

    # Forms, spinners and breadcrumbs are usually clutter, but some sites wrap the whole
    # page in them (ASP.NET's page-level <form>, class="with-breadcrumb" on <main>),
    # so never remove one that holds the page's main content.
    page_words = _word_count(soup)
    candidates = (
        soup.find_all("form")
        + soup.find_all(id=_LOADER_RE)
        + soup.find_all(class_=_LOADER_RE)
        + soup.find_all(class_=_BREADCRUMB_CLASS_RE)
    )
    for el in candidates:
        if el.parent is not None and not _wraps_page_content(el, page_words):
            el.extract()

    try:
        buf = BytesIO(str(soup).encode("utf-8"))
        result = converter.convert(DocumentStream(name="page.html", stream=buf))
        md = result.document.export_to_markdown()
    except Exception as e:
        raise ValueError(f"Could not convert page content: {e}") from e

    if not md or not md.strip():
        raise ValueError("Page converted to empty content")

    md = _clean_markdown(md)

    return md


def _clean_markdown(md):
    # Drop docling's empty image placeholder comments
    md = re.sub(r"<!--\s*image\s*-->", "", md, flags=re.IGNORECASE)

    # Drop leftover "Loading..." text from spinner divs
    md = re.sub(r"^\s*Loading\.\.\.\s*$", "", md, flags=re.MULTILINE)

    # Collapse 3+ consecutive blank lines into 2
    md = re.sub(r"\n{3,}", "\n\n", md)

    return md.strip()


def convert_file(file_bytes, file_name):
    #start_time = time.time()
    stream = DocumentStream(name=file_name, stream=BytesIO(file_bytes))
    result = converter.convert(stream)
    #end_time = time.time()
    #print(f"Conversion response time: {end_time - start_time} seconds")
    return result.document.export_to_markdown()