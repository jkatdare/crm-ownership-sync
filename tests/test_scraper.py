import pytest

from conftest import FIXTURES
from ownership_sync.scraper import PageGone, ScrapeError, fetch, parse_detail, scrape


def detail(name, street="1 Main St", place="Springfield, OH 45501"):
    return (f'<div class="wrap"><h1>{name}</h1><dl class="detail"><dt>Address</dt><dd>{street}<br>{place}</dd>'
            '<dt>Care Offerings</dt><dd><span class="badge">Assisted Living</span></dd>'
            '<dt>Administrator</dt><dd>Pat Doe</dd><dt>Phone</dt><dd>(555) 010-0000</dd></dl></div>')


# A miniature copy of the real site's quirks.
LAST_PAGE = ('<p>Page 2 of 2 · 3 communities listed</p><a href="/communities/gamma">Gamma</a>'
             '<div class="pager"><a href="/communities?page=1">Previous</a></div>')
SITE = {
    "/": '<nav><a href="/">Home</a><a href="/communities">Our Communities</a><a href="/about">About</a></nav>'
         '<p>Today we proudly serve 4 communities.</p><a href="/communities/hidden-new-one">New this year</a>',
    "/about": "<p>About us</p>",
    "/communities": '<p>Page 1 of 2 · 3 communities listed</p><a href="/communities/alpha">Alpha</a>'
                    '<a href="/communities/beta">Beta</a><div class="pager"><a href="/communities?page=2">Next</a></div>',
    "/communities?page=1": '<p>Page 1 of 2 · 3 communities listed</p><a href="/communities/alpha">Alpha</a>'
                           '<a href="/communities/beta">Beta</a><div class="pager"><a href="/communities?page=2">Next</a></div>',
    "/communities?page=2": LAST_PAGE,
    "/communities?page=3": LAST_PAGE,  # out-of-range pages re-serve the last page, like the real site
    "/communities/alpha": detail("Alpha House", "1 Main St"),
    "/communities/beta": detail("Beta House", "2 Main St"),
    "/communities/gamma": detail("Gamma House", "3 Main St"),
    "/communities/hidden-new-one": detail("Hidden New One", "4 Main St"),
}


def fake_fetch(_session, url):
    return SITE[url.removeprefix("https://example.test")]


def test_parse_real_detail_page():
    html = (FIXTURES / "detail_findlay.html").read_text(encoding="utf-8")
    loc = parse_detail(html, "https://example.test/communities/bellhaven-meadows-of-findlay")
    assert (loc.slug, loc.name) == ("bellhaven-meadows-of-findlay", "Bellhaven Meadows of Findlay")
    assert (loc.street, loc.city, loc.state, loc.zip) == ("1800 N Blanchard St", "Findlay", "OH", "45840")
    assert loc.care_offerings == ["Assisted Living", "Memory Support"]
    assert (loc.administrator, loc.phone) == ("Sam Pruitt", "(231) 533-2969")


def test_crawl_finds_every_community_including_one_missing_from_the_directory():
    locations, stats = scrape("https://example.test", fetch_fn=fake_fetch)
    assert [loc.slug for loc in locations] == ["alpha", "beta", "gamma", "hidden-new-one"]
    assert stats["outside_directory"] == ["hidden-new-one"]
    assert (stats["in_directory"], stats["directory_claims"], stats["home_page_claims"]) == (3, 3, 4)
    assert stats["warnings"] == []


def test_crawl_terminates_even_though_out_of_range_pages_never_come_back_empty():
    # Only links that are really on a page are followed, so ?page=3 is never requested.
    requested = []
    scrape("https://example.test", fetch_fn=lambda s, url: requested.append(url) or fake_fetch(s, url))
    assert not any(url.endswith("page=3") for url in requested)
    assert len(requested) == len(set(requested))


def test_count_mismatch_is_reported():
    site = dict(SITE, **{"/": SITE["/"].replace("serve 4 communities", "serve 9 communities")})
    _, stats = scrape("https://example.test", fetch_fn=lambda s, url: site[url.removeprefix("https://example.test")])
    assert any("home page says 9" in w for w in stats["warnings"])


def test_unparseable_community_page_aborts_instead_of_being_skipped():
    site = dict(SITE, **{"/communities/beta": "<div class='wrap'><h1>Beta House</h1></div>"})
    with pytest.raises(ScrapeError, match="beta"):
        scrape("https://example.test", fetch_fn=lambda s, url: site[url.removeprefix("https://example.test")])


def failing(path_to_fail, error):
    def fetch_fn(_session, url):
        path = url.removeprefix("https://example.test")
        if path == path_to_fail:
            raise error(f"{url}: failed")
        return SITE[path]
    return fetch_fn


def test_dead_nav_link_is_skipped_with_a_warning():
    locations, stats = scrape("https://example.test", fetch_fn=failing("/about", PageGone))
    assert len(locations) == 4 and any("/about" in w for w in stats["warnings"])


def test_outage_on_any_page_or_a_missing_directory_page_aborts():
    with pytest.raises(ScrapeError):  # a timeout / 5xx on a secondary page: it might have linked a community
        scrape("https://example.test", fetch_fn=failing("/about", ScrapeError))
    with pytest.raises(ScrapeError):  # the directory itself must load, even a 404 there is fatal
        scrape("https://example.test", fetch_fn=failing("/communities?page=2", PageGone))


def test_same_address_under_two_urls_aborts():
    site = dict(SITE)
    site["/"] += '<a href="/communities/alpha-renamed">Alpha, rebranded</a>'
    site["/communities/alpha-renamed"] = detail("Alpha Gardens", "1 Main Street")
    with pytest.raises(ScrapeError, match="share one address"):
        scrape("https://example.test", fetch_fn=lambda s, url: site[url.removeprefix("https://example.test")])


class FakeResponse:
    def __init__(self, status_code, text="<html></html>"):
        self.status_code, self.text = status_code, text

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, *statuses):
        self.statuses, self.calls = list(statuses), 0

    def get(self, url, timeout):
        self.calls += 1
        return FakeResponse(self.statuses[min(self.calls, len(self.statuses)) - 1])


def test_fetch_tells_a_dead_link_from_an_outage(monkeypatch):
    monkeypatch.setattr("ownership_sync.scraper.time.sleep", lambda _s: None)
    with pytest.raises(PageGone):
        fetch(FakeSession(404), "https://example.test/x")
    for status in (503, 429):  # retried, then a plain ScrapeError - never mistaken for a dead link
        session = FakeSession(status)
        with pytest.raises(ScrapeError) as err:
            fetch(session, "https://example.test/x")
        assert not isinstance(err.value, PageGone) and session.calls == 3
    assert fetch(FakeSession(503, 200), "https://example.test/x") == "<html></html>"  # recovers on retry


def test_unusual_slugs_and_non_page_links_are_handled():
    site = dict(SITE)
    site["/"] += ('<a href="/communities/Delta_House.2">Delta</a><a href="/files/brochure.pdf">Brochure</a>'
                  '<a href="/api/docs">API</a><a href="mailto:hello@example.test">Mail</a><a href="https://elsewhere.test/x">Out</a>')
    site["/communities/Delta_House.2"] = detail("Delta House", "5 Main St")
    requested = []

    def fetch_fn(_session, url):
        requested.append(url)
        return site[url.removeprefix("https://example.test")]

    locations, _ = scrape("https://example.test", fetch_fn=fetch_fn)
    assert "Delta_House.2" in [loc.slug for loc in locations]
    assert not any(part in url for url in requested for part in ("brochure", "/api", "elsewhere", "mailto"))
