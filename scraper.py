import asyncio
import aiohttp
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

USER_AGENT = "KnowledgeBaseBot/1.0 (educational knowledge base indexer; respectful crawler)"

# Max simultaneous connections across all hosts
DEFAULT_CONCURRENCY = 10


def _build_robots_checker(base_url: str):
    """Fetch and parse robots.txt synchronously (called via run_in_executor). Returns a can_fetch callable."""
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
    except Exception:
        # Unreachable robots.txt — assume everything is allowed
        pass
    return lambda url: rp.can_fetch(USER_AGENT, url)


async def _get_robots_checker(base_url: str):
    """Async wrapper — runs the blocking robots.txt fetch off the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _build_robots_checker, base_url)


async def get_links_by_depth(url, max_depth, allow_offsite=False, max_concurrent=DEFAULT_CONCURRENCY, max_pages=100):
    can_fetch = await _get_robots_checker(url)
    visited = set()
    results = []
    blocked = []
    queue = asyncio.Queue()
    queue.put_nowait((url, 0))
    semaphore = asyncio.Semaphore(max_concurrent)

    headers = {"User-Agent": USER_AGENT}

    async def fetch(session, page_url):
        async with semaphore:
            try:
                async with session.get(page_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as response:
                    return await response.text()
            except Exception as e:
                print(f"Failed to fetch {page_url}: {e}")
                return None

    async with aiohttp.ClientSession() as session:
        while not queue.empty():
            batch = []
            while not queue.empty():
                batch.append(queue.get_nowait())

            tasks = []
            for page_url, depth in batch:
                page_url = page_url.split('#')[0]
                if page_url in visited or depth > max_depth:
                    continue
                if max_pages is not None and len(results) >= max_pages:
                    break
                if not can_fetch(page_url):
                    print(f"Blocked by robots.txt: {page_url}")
                    visited.add(page_url)
                    blocked.append(page_url)
                    continue
                visited.add(page_url)
                results.append(page_url)
                print(f"Depth {depth}: {page_url}")
                tasks.append((page_url, depth, asyncio.ensure_future(fetch(session, page_url))))

            if max_pages is not None and len(results) >= max_pages:
                for _, _, task in tasks:
                    await task
                break

            for page_url, depth, task in tasks:
                html = await task
                if html is None:
                    continue
                soup = BeautifulSoup(html, 'html.parser')
                for a_tag in soup.find_all('a', href=True):
                    absolute_link = urljoin(page_url, a_tag['href']).split('#')[0]
                    same_domain = urlparse(url).netloc == urlparse(absolute_link).netloc
                    if absolute_link not in visited and (same_domain or allow_offsite):
                        queue.put_nowait((absolute_link, depth + 1))

    return results, blocked


async def get_links_by_prefix(url, prefixes, allow_offsite=False, max_concurrent=DEFAULT_CONCURRENCY, max_pages=100):
    if isinstance(prefixes, str):
        prefixes = [prefixes]

    can_fetch = await _get_robots_checker(url)
    visited = set()
    results = []
    blocked = []
    queue = asyncio.Queue()
    queue.put_nowait(url)
    semaphore = asyncio.Semaphore(max_concurrent)

    headers = {"User-Agent": USER_AGENT}

    async def fetch(session, page_url):
        async with semaphore:
            try:
                async with session.get(page_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as response:
                    return await response.text()
            except Exception as e:
                print(f"Failed to fetch {page_url}: {e}")
                return None

    async with aiohttp.ClientSession() as session:
        while not queue.empty():
            batch = []
            while not queue.empty():
                batch.append(queue.get_nowait())

            tasks = []
            for page_url in batch:
                page_url = page_url.split('#')[0]
                if page_url in visited:
                    continue
                if max_pages is not None and len(results) >= max_pages:
                    break
                if not can_fetch(page_url):
                    print(f"Blocked by robots.txt: {page_url}")
                    visited.add(page_url)
                    blocked.append(page_url)
                    continue
                visited.add(page_url)
                results.append(page_url)
                print(f"Prefix match: {page_url}")
                tasks.append((page_url, asyncio.ensure_future(fetch(session, page_url))))

            if max_pages is not None and len(results) >= max_pages:
                for _, task in tasks:
                    await task
                break

            for page_url, task in tasks:
                html = await task
                if html is None:
                    continue
                soup = BeautifulSoup(html, 'html.parser')
                for a_tag in soup.find_all('a', href=True):
                    absolute_link = urljoin(page_url, a_tag['href']).split('#')[0]
                    same_domain = urlparse(url).netloc == urlparse(absolute_link).netloc
                    matches_prefix = any(urlparse(absolute_link).path.startswith(p) for p in prefixes)

                    if absolute_link not in visited:
                        if same_domain and matches_prefix:
                            queue.put_nowait(absolute_link)
                        elif allow_offsite and not same_domain:
                            visited.add(absolute_link)
                            results.append(absolute_link)
                            print(f"Offsite: {absolute_link}")

    return results, blocked
