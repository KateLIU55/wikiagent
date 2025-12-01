# tests/test_crawler.py
# Unit tests for crawler/app.py

import os
import json
import sqlite3
import importlib
from pathlib import Path

import pytest


def make_crawler_module(tmp_path, monkeypatch, cfg_text=None):
    """
    Reload crawler.app so that DATA_DIR and CRAWL_CFG point into tmp_path.

    cfg_text (str | None): if provided, write this YAML to whitelist.yml
    and set CRAWL_CFG to that path.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    cfg_path = tmp_path / "whitelist.yml"
    if cfg_text is not None:
        cfg_path.write_text(cfg_text, encoding="utf-8")

    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("CRAWL_CFG", str(cfg_path))

    import crawler.app as crawler_app  # type: ignore
    crawler_app = importlib.reload(crawler_app)

    # force BeautifulSoup to use html.parser so lxml is not required
    from bs4 import BeautifulSoup as BS
    monkeypatch.setattr(
        crawler_app,
        "BeautifulSoup",
        lambda markup, *args, **kwargs: BS(markup, "html.parser"),
    )

    return crawler_app



def test_atomic_write_and_read(tmp_path, monkeypatch):
    crawler = make_crawler_module(tmp_path, monkeypatch)

    bpath = tmp_path / "bytes.bin"
    tpath = tmp_path / "text.txt"

    crawler._atomic_write_bytes(str(bpath), b"hello world")
    assert bpath.read_bytes() == b"hello world"

    crawler._atomic_write_text(str(tpath), "你好，世界")
    assert tpath.read_text(encoding="utf-8") == "你好，世界"


def test_write_raw_and_meta(tmp_path, monkeypatch):
    crawler = make_crawler_module(tmp_path, monkeypatch)

    # page_id=42
    html_path = crawler.write_raw(42, b"<html>hi</html>")
    meta_path = os.path.join(crawler.RAW_DIR, "42.meta.json")
    crawler.write_meta(42, {"url": "https://example.com"})

    assert Path(html_path).is_file()
    assert Path(meta_path).is_file()
    meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    assert meta["url"] == "https://example.com"


# config / pattern
def test_load_cfg_flattens_lists(tmp_path, monkeypatch):
    crawler = make_crawler_module(tmp_path, monkeypatch)

    cfg_file = tmp_path / "cfg.yml"
    cfg_file.write_text(
        """
include_patterns:
  - [ "foo", "bar" ]
  - [ "baz" ]
exclude_patterns:
  - [ "skip" ]
seeds:
  - [ "https://a", "https://b" ]
  - [ "https://c" ]
""",
        encoding="utf-8",
    )

    cfg = crawler.load_cfg(str(cfg_file))

    assert cfg["include_patterns"] == ["foo", "bar", "baz"]
    assert cfg["exclude_patterns"] == ["skip"]
    assert cfg["seeds"] == ["https://a", "https://b", "https://c"]


def test_canon_url_basic(tmp_path, monkeypatch):
    crawler = make_crawler_module(tmp_path, monkeypatch)

    # strips fragment
    u = "https://en.wikipedia.org/wiki/Nanjing#History"
    assert crawler.canon_url(u) == "https://en.wikipedia.org/wiki/Nanjing"

    # keeps query
    u2 = "https://en.wikipedia.org/wiki/Nanjing?x=1"
    assert crawler.canon_url(u2) == u2

    # drops edit pages
    edit = "https://en.wikipedia.org/w/index.php?title=Nanjing&action=edit"
    assert crawler.canon_url(edit) is None


def test_compile_patterns_and_allowed(tmp_path, monkeypatch):
    crawler = make_crawler_module(tmp_path, monkeypatch)

    inc = crawler.compile_patterns([r"foo", r"bar"])
    exc = crawler.compile_patterns([r"bad"])

    assert crawler.allowed_by_patterns("https://x/foo", inc, exc) is True
    assert crawler.allowed_by_patterns("https://x/baz", inc, exc) is False
    assert crawler.allowed_by_patterns("https://x/bad", inc, exc) is False

    # no include list => allow unless excluded
    assert crawler.allowed_by_patterns("https://x/ok", [], exc) is True
    assert crawler.allowed_by_patterns("https://x/bad", [], exc) is False


# topic & link extraction 
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://en.wikipedia.org/wiki/Nanjing", True),
        ("https://zh.wikipedia.org/wiki/Nanjing_Metro", True),
        ("https://en.wikipedia.org/wiki/History_of_Nanjing", True),
        ("https://en.wikipedia.org/wiki/Category:Transport_in_Nanjing", True),
        ("https://en.wikipedia.org/wiki/Shanghai", False),
        ("https://en.wikipedia.org/wiki/Category:Something_else", False),
    ],
)
def test_is_topic_url(tmp_path, monkeypatch, url, expected):
    crawler = make_crawler_module(tmp_path, monkeypatch)
    assert crawler.is_topic_url(url) is expected


def test_extract_links_category_page(tmp_path, monkeypatch):
    crawler = make_crawler_module(tmp_path, monkeypatch)

    html = b"""
<html><body>
<div id="mw-pages">
  <a href="/wiki/Nanjing_Wall">Wall</a>
  <a href="/wiki/Talk:Nanjing">Talk page</a>
  <a href="/wiki/Category:Not_used">Category only</a>
</div>
<div id="mw-subcategories">
  <a href="/wiki/Category:Monuments_in_Nanjing">Nanjing Monuments</a>
  <a href="/wiki/Category:Something_else">Other</a>
</div>
<div id="mw-pages">
  <a href="/w/index.php?title=Category:Monuments_in_Nanjing&page=2">next</a>
</div>
</body></html>
"""

    base = "https://en.wikipedia.org/wiki/Category:Monuments_in_Nanjing"
    links = crawler.extract_links(base, html)
    urls = {u for (u, a) in links}

    assert "https://en.wikipedia.org/wiki/Nanjing_Wall" in urls
    # Talk: and non-article category members should be filtered out
    assert not any("Talk:" in u for u in urls)
    # Nanjing subcategory kept, other subcategory skipped
    assert "https://en.wikipedia.org/wiki/Category:Monuments_in_Nanjing" in urls
    assert not any("Something_else" in u for u in urls)
    # category pagination link present
    assert any("index.php?title=Category:Monuments_in_Nanjing" in u for u in urls)


def test_extract_links_article_page_with_interlanguage(tmp_path, monkeypatch):
    crawler = make_crawler_module(tmp_path, monkeypatch)

    html = b"""
<html><body>
  <a href="/wiki/Nanjing_Metro">Metro</a>
  <a href="/wiki/History_of_Nanjing">History</a>
  <a href="/wiki/Random_page">Random</a>
  <a href="/wiki/File:Image.jpg">File</a>

  <a hreflang="zh" href="https://zh.wikipedia.org/wiki/%E5%8D%97%E4%BA%AC">Nanjing zh</a>
  <a hreflang="en" href="https://en.wikipedia.org/wiki/Nanjing">Nanjing en</a>
</body></html>
"""

    base = "https://en.wikipedia.org/wiki/Nanjing"
    links = crawler.extract_links(base, html)
    urls = {u for (u, a) in links}
    anchors = {a for (u, a) in links}

    # topic-filtered article links
    assert "https://en.wikipedia.org/wiki/Nanjing_Metro" in urls
    assert "https://en.wikipedia.org/wiki/History_of_Nanjing" in urls
    # not Nanjing-related => excluded
    assert not any("Random_page" in u for u in urls)
    # file namespace excluded
    assert not any("Image.jpg" in u for u in urls)

    # interlanguage links present with special anchors
    assert "interlanguage-zh" in anchors
    assert "interlanguage-en" in anchors
    assert any(u.startswith("https://zh.wikipedia.org/wiki/") for (u, a) in links if a == "interlanguage-zh")
    assert any(u.startswith("https://en.wikipedia.org/wiki/") for (u, a) in links if a == "interlanguage-en")


# rate limiter 
def test_rate_limiter_wait_respects_rps(tmp_path, monkeypatch):
    crawler = make_crawler_module(tmp_path, monkeypatch)

    # Simulate monotonic time and capture sleep intervals
    times = iter([0.0, 0.1])  # first call at t=0.0, second at t=0.1
    sleeps = []

    monkeypatch.setattr(crawler.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(crawler.time, "sleep", lambda s: sleeps.append(s))

    rl = crawler.RateLimiter(rps_per_host=2.0)  # 2 rps -> min 0.5s interval

    rl.wait("example.com")  # first call: no sleep
    rl.wait("example.com")  # second call: should sleep ~0.4

    # With rps=2, period = 0.5s
    # At t=0.0  -> sleep 0.5
    # At t=0.1  -> next_ok = 0.5 + 0.5 = 1.0, so sleep 0.9
    assert len(sleeps) == 2
    assert sleeps[0] == pytest.approx(0.5, rel=1e-3, abs=1e-3)
    assert sleeps[1] == pytest.approx(0.9, rel=1e-3, abs=1e-3)


# DB helpers 
def test_init_db_and_upsert_save_fetch(tmp_path, monkeypatch):
    crawler = make_crawler_module(tmp_path, monkeypatch)

    crawler.init_db()
    conn = crawler.db()

    # upsert_page should insert once and then reuse the same id
    pid1, etag1, lm1 = crawler.upsert_page(conn, "https://en.wikipedia.org/wiki/Nanjing", 0)
    pid2, etag2, lm2 = crawler.upsert_page(conn, "https://en.wikipedia.org/wiki/Nanjing", 1)

    assert pid1 == pid2
    cur = conn.execute("SELECT COUNT(*) FROM pages")
    assert cur.fetchone()[0] == 1

    crawler.save_fetch_log(conn, pid1, 200, 1234, None)
    cur = conn.execute("SELECT status, bytes FROM fetch_log WHERE page_id=?", (pid1,))
    row = cur.fetchone()
    assert row == (200, 1234)

    conn.close()


# robots.txt behaviour
def test_robots_ok_allow_and_deny(tmp_path, monkeypatch):
    crawler = make_crawler_module(tmp_path, monkeypatch)

    crawler._rp_cache.clear()

    class DummyResp:
        def __init__(self, text):
            self.status_code = 200
            self.text = text

    def fake_get(url, headers=None, timeout=10):
        host = Path(url).parts[0]  # url like 'https://allow.com/robots.txt'
        # simpler: check substring in url
        if "allow.com" in url:
            return DummyResp("User-agent: *\nAllow: /\n")
        if "block.com" in url:
            return DummyResp("User-agent: *\nDisallow: /private\n")
        # unreachable / no robots
        raise RuntimeError("network error")

    monkeypatch.setattr(crawler.requests, "get", fake_get)

    assert crawler.robots_ok("https://allow.com/some/path") is True
    assert crawler.robots_ok("https://block.com/private/secret") is False
    assert crawler.robots_ok("https://block.com/public/info") is True
    # host with failing robots.txt -> allowed by policy
    assert crawler.robots_ok("https://unreachable.com/foo") is True


# crawl() smoke test 
def test_crawl_smoke(tmp_path, monkeypatch):
    """
    Small end-to-end smoke test for crawl(): one seed, one page, no links.
    Uses a fake HTTP session and skips robots.
    """
    cfg_text = """
seeds:
  - https://en.wikipedia.org/wiki/Nanjing
include_patterns:
  - ".*"
exclude_patterns: []
limits:
  max_pages: 1
  max_depth: 0
rate_limit:
  per_host_rps: 1000
  max_parallel: 1
respect_robots: false
"""
    crawler = make_crawler_module(tmp_path, monkeypatch, cfg_text=cfg_text)

    # Fake HTTP session that returns a single HTML page
    class DummyResp:
        def __init__(self):
            self.status_code = 200
            self.headers = {
                "Content-Type": "text/html; charset=UTF-8",
                "ETag": "etag-1",
                "Last-Modified": "Wed, 21 Nov 2025 10:00:00 GMT",
            }
            self.content = b"<html><body><p>Hello Nanjing</p></body></html>"

    class DummySession:
        def __init__(self):
            self.called = False
            self.last_url = None

        def get(self, url, timeout=25, headers=None):
            self.called = True
            self.last_url = url
            return DummyResp()

    monkeypatch.setattr(crawler.requests, "Session", lambda: DummySession())
    # No-op rate limiting
    monkeypatch.setattr(crawler.time, "sleep", lambda s: None)
    # No links extracted to keep frontier small
    monkeypatch.setattr(crawler, "extract_links", lambda base_url, html: [])

    crawler.crawl()

    # Verify DB has one page and one fetch_log row
    conn = sqlite3.connect(crawler.DB_PATH)
    cur = conn.execute("SELECT COUNT(*) FROM pages")
    assert cur.fetchone()[0] == 1
    cur = conn.execute("SELECT COUNT(*) FROM fetch_log")
    assert cur.fetchone()[0] == 1
    conn.close()

    # Raw HTML + meta file exist
    raw_files = list(Path(crawler.RAW_DIR).glob("*.html"))
    meta_files = list(Path(crawler.RAW_DIR).glob("*.meta.json"))
    assert len(raw_files) == 1
    assert len(meta_files) == 1
