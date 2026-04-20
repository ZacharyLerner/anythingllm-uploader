import requests
from bs4 import BeautifulSoup

from docling.document_converter import DocumentConverter
from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import DocumentStream

from io import BytesIO
import time

converter = DocumentConverter()

_HEADERS = {"User-Agent": "KnowledgeBaseBot/1.0 (educational knowledge base indexer; respectful crawler)"}

def scrape_website_md(url):
    response = requests.get(url, headers=_HEADERS, timeout=15)

    # Treat non-200 responses as failures
    if response.status_code != 200:
        raise ValueError(f"HTTP {response.status_code}")

    html_content = response.text

    # If the page is essentially empty (JS redirect, login wall, etc.)
    # there is nothing to convert — bail early with a clear message.
    soup = BeautifulSoup(html_content, "html.parser")
    body_text = soup.get_text(strip=True)
    if len(body_text) < 50:
        raise ValueError("Page returned no usable content (may require login or redirect)")

    for tag in soup.find_all(["header", "footer", "nav"]):
        tag.extract()

    try:
        result = converter.convert_string(
            content=str(soup),
            format=InputFormat.HTML,
            name="page.html"
        )
        md = result.document.export_to_markdown()
    except Exception as e:
        raise ValueError(f"Could not convert page content: {e}") from e

    if not md or not md.strip():
        raise ValueError("Page converted to empty content")

    return md

def convert_file(file_bytes, file_name):
    #start_time = time.time()
    stream = DocumentStream(name=file_name, stream=BytesIO(file_bytes))
    result = converter.convert(stream)
    #end_time = time.time()
    #print(f"Conversion response time: {end_time - start_time} seconds")
    return result.document.export_to_markdown()
