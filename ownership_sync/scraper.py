"""Scrape every Bellhaven community from the public website.

Strategy: a tiny crawler rather than "loop over ?page=N until empty".
  * Start at the home page and visit every same-site page that is not a community page
    (directory pages, the pager's "Next" links, About ...), each exactly once.
  * Collect every /communities/<slug> link seen on any of them, then fetch each detail page.

Why:
  1. The directory clamps out-of-range pages (?page=99 quietly re-serves the last page), so
     "increment until empty" never ends. Following the links that are actually on the page does.
  2. Not every community is in the directory. A newly announced one can be linked only from the
     home page, so links are harvested from every page, not just the directory.
"""
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

DETAIL_PATH = re.compile(r"^/communities/[^/]+$")  # one path segment below the directory = a community page
SKIP_LINK = re.compile(r"^/(api|crm)(/|$)|\.(pdf|jpe?g|png|gif|svg|css|js|ico|xml|zip)$", re.I)  # not public web pages
CITY_LINE = re.compile(r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})(?:-\d{4})?$")
MAX_PAGES = 60  # crawl guard: the site has ~6 non-detail pages


class ScrapeError(RuntimeError):
    """The website could not be read reliably. The run aborts rather than act on partial data."""


@dataclass
class Location:
    slug: str
    url: str
    name: str
    street: str
    city: str
    state: str
    zip: str
    care_offerings: list = field(default_factory=list)
    administrator: str = ""
    phone: str = ""


def fetch(session, url, retries=3):
    """GET with a short retry on network errors / 5xx. A 4xx is a real answer and fails at once."""
    last = None
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp.text
            last = f"HTTP {resp.status_code}"
        except requests.HTTPError as exc:
            raise ScrapeError(f"{url}: {exc}") from exc
        except requests.RequestException as exc:
            last = exc
        time.sleep(1 + attempt)
    raise ScrapeError(f"{url}: gave up after {retries} attempts ({last})")


def _clean(text):
    return " ".join((text or "").split())


def parse_detail(html, url):
    """One community page -> Location. Raises ScrapeError if a required field is missing."""
    soup = BeautifulSoup(html, "html.parser")
    try:
        name = _clean(soup.select_one(".wrap h1").get_text())
        fields = {_clean(dt.get_text()).lower(): dt.find_next_sibling("dd") for dt in soup.select("dl.detail dt")}
        address_lines = [_clean(s) for s in fields["address"].stripped_strings]
        place = CITY_LINE.match(address_lines[-1])
        care = [_clean(b.get_text()) for b in fields["care offerings"].select(".badge")]
        if not (name and place and len(address_lines) >= 2 and care):
            raise ValueError("name, address or care offerings missing")
    except (AttributeError, KeyError, IndexError, ValueError) as exc:
        raise ScrapeError(f"{url}: could not parse community page ({exc})") from exc
    optional = lambda key: _clean(fields[key].get_text()) if key in fields else ""
    return Location(
        slug=urlparse(url).path.rsplit("/", 1)[-1],
        url=url,
        name=name,
        street=", ".join(address_lines[:-1]),
        city=place["city"],
        state=place["state"],
        zip=place["zip"],
        care_offerings=care,
        administrator=optional("administrator"),
        phone=optional("phone"),
    )


def scrape(base_url, session=None, fetch_fn=fetch):
    """Crawl the site. Returns (locations sorted by slug, stats dict with self-check numbers)."""
    root = base_url.rstrip("/")
    session = session or requests.Session()
    session.headers["User-Agent"] = "bellhaven-ownership-sync/1.0"

    queue, visited, found_on = ["/"], set(), {}  # found_on: detail path -> pages that link to it
    claims, warnings = {}, []
    while queue:
        page = queue.pop(0)
        if page in visited:
            continue
        if len(visited) >= MAX_PAGES:
            raise ScrapeError(f"crawl did not converge after {MAX_PAGES} pages")
        visited.add(page)
        try:
            html = fetch_fn(session, root + page)
        except ScrapeError as exc:
            # A dead link in the nav or footer must not block the sync. The home page and the
            # directory are different: without them the list of communities cannot be trusted.
            if page == "/" or page.startswith("/communities"):
                raise
            warnings.append(f"skipped a page that would not load: {exc}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ")
        # The site states its own totals; keep them to check the scrape against.
        if m := re.search(r"(\d+)\s+communities listed", text):
            claims["directory_claims"] = int(m.group(1))
        if m := re.search(r"serve\s+(\d+)\s+communities", text):
            claims["home_page_claims"] = int(m.group(1))
        for a in soup.select("a[href]"):
            target = urlparse(urljoin(root + page, a["href"]))
            if target.netloc != urlparse(root).netloc:
                continue
            path = target.path.rstrip("/") or "/"
            if SKIP_LINK.search(path):
                continue
            if DETAIL_PATH.match(path):
                found_on.setdefault(path, set()).add(page)
            else:
                link = path + (f"?{target.query}" if target.query else "")
                if link not in visited and link not in queue:
                    queue.append(link)

    locations = [parse_detail(fetch_fn(session, root + path), root + path) for path in sorted(found_on)]
    in_directory = [p for p, pages in found_on.items() if any(pg.startswith("/communities") for pg in pages)]
    stats = {
        "pages_crawled": len(visited),
        "communities_found": len(locations),
        "in_directory": len(in_directory),
        "outside_directory": sorted(p.rsplit("/", 1)[-1] for p in found_on if p not in in_directory),
        **claims,
        "warnings": warnings,
    }
    if claims.get("directory_claims") not in (None, len(in_directory)):
        stats["warnings"].append(f"directory says {claims['directory_claims']} communities, scraped {len(in_directory)} from it")
    if claims.get("home_page_claims") not in (None, len(locations)):
        stats["warnings"].append(f"home page says {claims['home_page_claims']} communities, scraped {len(locations)} in total")
    if len({loc.slug for loc in locations}) != len(locations):
        raise ScrapeError("duplicate community slugs scraped")
    return locations, stats
