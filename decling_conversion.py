import re
import requests
from bs4 import BeautifulSoup

from docling.document_converter import DocumentConverter
from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import DocumentStream

from io import BytesIO
import time

converter = DocumentConverter()

_HEADERS = {"User-Agent": "KnowledgeBaseBot/1.0 (educational knowledge base indexer; respectful crawler)"}
_NOISE_TAGS = ["header", "footer", "nav", "script", "style", "noscript", "aside", "form"]


def scrape_website_md(url):
    try:
        response = requests.get(url, headers=_HEADERS, timeout=15)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise ValueError(f"Request failed: {e}") from e

    response.encoding = response.apparent_encoding
    soup = BeautifulSoup(response.text, "html.parser")

    # If the page is essentially empty (JS redirect, login wall, etc.)
    # there is nothing to convert — bail early with a clear message.
    if len(soup.get_text(strip=True)) < 50:
        raise ValueError("Page returned no usable content (may require login or redirect)")

    # Strip noise tags (header/footer/nav + scripts/styles/forms)
    for tag in soup.find_all(_NOISE_TAGS):
        tag.extract()

    # Strip loading spinners and breadcrumbs by id/class
    for el in soup.find_all(id=re.compile(r"loader|spinner", re.I)):
        el.extract()
    for el in soup.find_all(class_=re.compile(r"loader|spinner|breadcrumb", re.I)):
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