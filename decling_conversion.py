import requests
from bs4 import BeautifulSoup

from docling.document_converter import DocumentConverter
from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import DocumentStream

from io import BytesIO
import time

converter = DocumentConverter()

def scrape_website_md(url):
    html_content = requests.get(url).text
    soup = BeautifulSoup(html_content, "html.parser")

    for tag in soup.find_all(["header", "footer", "nav"]):
        tag.extract()

    result = converter.convert_string(
        content=str(soup),
        format=InputFormat.HTML,
        name="page.html"
    )
    md = result.document.export_to_markdown()
    return md

def convert_file(file_bytes, file_name):
    #start_time = time.time()
    stream = DocumentStream(name=file_name, stream=BytesIO(file_bytes))
    result = converter.convert(stream)
    #end_time = time.time()
    #print(f"Conversion response time: {end_time - start_time} seconds")
    return result.document.export_to_markdown()
