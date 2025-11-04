#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, time, hashlib, queue, threading, sqlite3, urllib.parse, urllib.robotparser
from datetime import datetime, timezone
import yaml, requests
from bs4 import BeautifulSoup
import json

DATA_DIR = os.environ.get("DATA_DIR", "/data")
RAW_DIR = os.path.join(DATA_DIR, "raw")
DB_PATH = os.path.join(DATA_DIR, "wiki.sqlite")
CFG_PATH = os.environ.get("CRAWL_CFG", "/config/whitelist.yml")
UA = "ANJSO-WikiCrawler/1.0 (+https://anjso.org/wiki)"

os.makedirs(RAW_DIR, exist_ok=True)

def _atomic_write_bytes(path: str, data: bytes):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # atomic on same filesystem

def _atomic_write_text(path: str, text: str):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def write_raw(page_id, html: bytes):
    path = os.path.join(RAW_DIR, f"{page_id}.html")
    _atomic_write_bytes(path, html)
    return path

def write_meta(page_id, meta: dict):
    path = os.path.join(RAW_DIR, f"{page_id}.meta.json")
    _atomic_write_text(path, json.dumps(meta, ensure_ascii=False))


def load_cfg(path=CFG_PATH):
    with open(path, "r") as f:
        cfg = yaml.safe_load(f) or {}
    for k in ("include_patterns","exclude_patterns","seeds"):
        v = cfg.get(k, [])
        if isinstance(v, list) and v and isinstance(v[0], list):
            cfg[k] = [item for sub in v for item in sub]
    return cfg

# DB setup used only during init 
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS pages(
      id INTEGER PRIMARY KEY,
      url TEXT UNIQUE NOT NULL,
      first_seen TEXT,
      last_seen TEXT,
      last_status INTEGER,
      etag TEXT,
      last_modified TEXT,
      content_hash TEXT,
      depth INTEGER
    );
    CREATE TABLE IF NOT EXISTS fetch_log(
      id INTEGER PRIMARY KEY,
      page_id INTEGER,
      fetched_at TEXT,
      status INTEGER,
      bytes INTEGER,
      error TEXT,
      FOREIGN KEY(page_id) REFERENCES pages(id)
    );
    CREATE TABLE IF NOT EXISTS links(
      from_page INTEGER,
      to_url TEXT,
      anchor TEXT,
      PRIMARY KEY(from_page, to_url),
      FOREIGN KEY(from_page) REFERENCES pages(id)
    );
    """)
    conn.commit(); conn.close()

def canon_url(u):
    u = urllib.parse.urldefrag(u)[0]
    p = urllib.parse.urlsplit(u)
    if p.netloc.endswith("wikipedia.org") and "action=edit" in (p.query or ""):
        return None
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))

def compile_patterns(pats):
    if not pats: return []
    return [re.compile(p) for p in pats]

def allowed_by_patterns(url, include_res, exclude_res):
    if include_res and not any(r.search(url) for r in include_res):
        return False
    if exclude_res and any(r.search(url) for r in exclude_res):
        return False
    return True

_rp_cache = {}
def robots_ok(url, agent=UA):
    host = urllib.parse.urlsplit(url).netloc
    rp = _rp_cache.get(host)
    if not rp:
        rp = urllib.robotparser.RobotFileParser()
        for scheme in ("https","http"):
            try:
                r = requests.get(f"{scheme}://{host}/robots.txt",
                                 headers={"User-Agent": agent, "Accept":"text/plain"},
                                 timeout=10)
                if r.status_code == 200 and r.text.strip():
                    rp.parse(r.text.splitlines())
                    _rp_cache[host] = rp
                    break
            except Exception:
                pass
        if host not in _rp_cache:
            print(f"[warn] robots.txt unreadable; allowing by policy for {host}", flush=True)
            return True
    return _rp_cache[host].can_fetch("*", url)   # use default '*' rules

def upsert_page(conn, url, depth):
    row = conn.execute("SELECT id, etag, last_modified FROM pages WHERE url=?", (url,)).fetchone()
    if row: return row[0], row[1], row[2]
    conn.execute("INSERT INTO pages(url, first_seen, depth) VALUES(?,?,?)",
                 (url, datetime.now(timezone.utc).isoformat(), depth))
    conn.commit()
    return conn.execute("SELECT id, etag, last_modified FROM pages WHERE url=?", (url,)).fetchone()

def save_fetch_log(conn, page_id, status, nbytes, err=None):
    conn.execute("INSERT INTO fetch_log(page_id, fetched_at, status, bytes, error) VALUES(?,?,?,?,?)",
                 (page_id, datetime.now(timezone.utc).isoformat(), status, nbytes, err))
    conn.commit()

# Nanjing scoping 
TOPIC_RX = re.compile(
    r"^https://en\.wikipedia\.org/wiki/(?:"
    r"Nanjing($|_)|"
    r"[^:#]+_(?:in|of|from|at)_Nanjing($|_)|"
    r"Category:([^:]*Nanjing[^:]*)$"
    r")"
)

def is_topic_url(u: str) -> bool:
    return bool(TOPIC_RX.search(u))

def extract_links(base_url: str, html: bytes):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    out = []

    def abs_wiki(href: str) -> str | None:
        if not href or not href.startswith("/wiki/"):
            return None
        # skip non-article namespaces early
        if href.startswith((
            "/wiki/Talk:", "/wiki/Help:", "/wiki/File:",
            "/wiki/Special:", "/wiki/Template:", "/wiki/Portal:"
        )):
            return None
        u = urllib.parse.urljoin("https://en.wikipedia.org", href)
        return canon_url(u)

    # treat both /wiki/Category:* and pagination pages as "category"
    is_category = (
        "/wiki/Category:" in base_url
        or ("/w/index.php" in base_url and "title=Category:" in base_url)
    )

    if is_category:
        # 1) category members (namespace 0 only: titles without ':')
        for a in soup.select("#mw-pages a[href^='/wiki/']"):
            u = abs_wiki(a.get("href"))
            if not u:
                continue
            tail = urllib.parse.urlsplit(u).path[len("/wiki/"):]
            if ":" in tail:  # skip files/templates etc
                continue
            out.append((u, a.get_text(" ", strip=True)[:200]))

        # 2) Nanjing subcategories (kept tight)
        for a in soup.select("#mw-subcategories a[href^='/wiki/Category:']"):
            u = abs_wiki(a.get("href"))
            if u and "Nanjing" in (a.get_text() or ""):
                out.append((u, a.get_text(" ", strip=True)[:200]))

        # 3) category pagination (next/prev pages)
        for a in soup.select("#mw-pages a[href*='/w/index.php'][href*='title=Category:']"):
            href = a.get("href")
            if not href:
                continue
            u = urllib.parse.urljoin("https://en.wikipedia.org", href)
            u = canon_url(u)
            if u:
                out.append((u, "cat-page"))
        return out  # <-- important

    # Article pages: follow only Nanjing-scoped links
    for a in soup.select("a[href^='/wiki/']"):
        u = abs_wiki(a.get("href"))
        if u and is_topic_url(u):
            out.append((u, a.get_text(" ", strip=True)[:200]))

    return out

class RateLimiter:
    def __init__(self, rps_per_host=2.0):
        self.rps = float(rps_per_host)
        self.last = {}
        self.lock = threading.Lock()
    def wait(self, host):
        with self.lock:
            now = time.monotonic()
            period = 1.0 / max(self.rps, 0.1)
            next_ok = self.last.get(host, 0.0) + period
            sleep = max(0.0, next_ok - now)
            self.last[host] = now + sleep
        if sleep: time.sleep(sleep)

def crawl():
    cfg = load_cfg()
    print(f"[cfg] file={CFG_PATH} seeds={len(cfg.get('seeds', []))} "
          f"max_pages={cfg.get('limits',{}).get('max_pages')} "
          f"max_depth={cfg.get('limits',{}).get('max_depth')}", flush=True)
    include_res = compile_patterns(cfg.get("include_patterns"))
    exclude_res = compile_patterns(cfg.get("exclude_patterns"))
    max_pages = int(os.environ.get("CRAWL_MAX_PAGES",
                  cfg.get("limits", {}).get("max_pages", 900)))
    max_depth = int(os.environ.get("CRAWL_MAX_DEPTH",
                  cfg.get("limits", {}).get("max_depth", 3)))
    rps = float(cfg.get("rate_limit", {}).get("per_host_rps", 2))
    workers = int(cfg.get("rate_limit", {}).get("max_parallel", 8))
    honor_robots = bool(cfg.get("respect_robots", True))

    print(f"[cfg] max_pages={max_pages} depth={max_depth} workers={workers} rps/host={rps} robots={honor_robots}", flush=True)

    init_db()

    frontier = queue.Queue()
    seen = set()

    # stop flag so we don't nuke the frontier 
    stop_event = threading.Event()

    enq = 0
    for s in cfg.get("seeds", []):
        su = canon_url(s)
        if su and allowed_by_patterns(su, include_res, exclude_res):
            frontier.put((su, 0)); enq += 1
        else:
            print(f"[seed-skip] {s}", flush=True)
    print(f"[seed] enqueued={enq}", flush=True)

    limiter = RateLimiter(rps_per_host=rps)
    fetched = 0
    fetch_lock = threading.Lock()

    def worker():
        nonlocal fetched
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")

        session = requests.Session()
        headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}

        try:
            while True:
                try:
                    url, depth = frontier.get(timeout=2)
                except queue.Empty:
                    break

                # once quota reached, skip any remaining queued items quickly 
                if stop_event.is_set():
                    frontier.task_done()
                    continue

                try:
                    if url in seen:
                        frontier.task_done()  # balance join()
                        continue
                    seen.add(url)

                    if honor_robots and not robots_ok(url):
                        print(f"[skip] robots disallow: {url}", flush=True)
                        frontier.task_done()
                        continue

                    pid_etag_lm = upsert_page(conn, url, depth)
                    page_id, prev_etag, prev_lm = (
                        pid_etag_lm if isinstance(pid_etag_lm, tuple) else (pid_etag_lm, None, None)
                    )

                    host = urllib.parse.urlsplit(url).netloc
                    limiter.wait(host)

                    h = headers.copy()
                    if prev_etag: h["If-None-Match"] = prev_etag
                    if prev_lm:   h["If-Modified-Since"] = prev_lm

                    resp = session.get(url, timeout=25, headers=h)
                    status = resp.status_code
                    ctype = (resp.headers.get("Content-Type") or "").lower()

                    if status == 304:
                        save_fetch_log(conn, page_id, status, 0, None)
                        conn.execute(
                            "UPDATE pages SET last_seen=?, last_status=? WHERE id=?",
                            (datetime.now(timezone.utc).isoformat(), status, page_id)
                        )
                        conn.commit()
                        print(f"[not-modified] 304 id={page_id} {url}", flush=True)
                        frontier.task_done()
                        continue

                    if status != 200 or "text/html" not in ctype:
                        save_fetch_log(conn, page_id, status, 0, f"ctype={ctype}")
                        conn.execute(
                            "UPDATE pages SET last_seen=?, last_status=? WHERE id=?",
                            (datetime.now(timezone.utc).isoformat(), status, page_id)
                        )
                        conn.commit()
                        print(f"[skip] status={status} ctype={ctype} id={page_id} {url}", flush=True)
                        frontier.task_done()
                        continue

                    # 200 OK HTML path 
                    html = resp.content
                    write_raw(page_id, html)
                    etag = resp.headers.get("ETag")
                    last_mod = resp.headers.get("Last-Modified")
                    chash = hashlib.md5(html).hexdigest()

                    write_meta(page_id, {
                        "url": url,
                        "depth": depth,
                        "status": 200,
                        "etag": etag,
                        "last_modified": last_mod,
                        "content_hash": chash,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    })

                    conn.execute(
                        "UPDATE pages SET last_seen=?, last_status=?, etag=?, last_modified=?, content_hash=? WHERE id=?",
                        (datetime.now(timezone.utc).isoformat(), status, etag, last_mod, chash, page_id)
                    )
                    save_fetch_log(conn, page_id, status, len(html), None)
                    print(f"[ok] 200 id={page_id} bytes={len(html)} depth={depth} {url}", flush=True)

                    # gate new enqueues once quota hit 
                    if depth + 1 <= max_depth and not stop_event.is_set():
                        try:
                            links = extract_links(url, html) or []
                        except Exception as e:
                            print(f"[warn] link-extract failed id={page_id} {url}: {e!r}", flush=True)
                            links = []
                        for to_url, anchor in (links or []):
                            if allowed_by_patterns(to_url, include_res, exclude_res) and to_url not in seen:
                                frontier.put((to_url, depth + 1))
                            try:
                                conn.execute(
                                    "INSERT OR IGNORE INTO links(from_page, to_url, anchor) VALUES(?,?,?)",
                                    (page_id, to_url, (anchor or "")[:200])
                                )
                            except Exception:
                                pass

                    with fetch_lock:
                        fetched += 1
                        if fetched % 25 == 0:
                            print(f"[prog] fetched={fetched} frontier={frontier.qsize()}", flush=True)
                        if fetched >= max_pages:
                            stop_event.set()  # hit quota; stop enqueuing/processing new items

                    conn.commit()
                    frontier.task_done()

                except Exception as e:
                    try:
                        if 'page_id' in locals():
                            save_fetch_log(conn, page_id, -1, 0, str(e))
                    except Exception:
                        pass
                    print(f"[err] {url if 'url' in locals() else 'no-url'}: {e!r}", flush=True)
                    frontier.task_done()

        finally:
            try: conn.close()
            except Exception: pass

    #  launch workers and wait for completion 
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()

    frontier.join()  # all queued tasks have been task_done()

    for t in threads:
        t.join(timeout=1.0)

    print(f"[done] fetched={fetched} raw_dir={RAW_DIR}", flush=True)


# keep-alive
import sys, signal
def _graceful(signum, frame):
    print("Crawler shutting down...", flush=True); sys.exit(0)
signal.signal(signal.SIGINT, _graceful)
signal.signal(signal.SIGTERM, _graceful)

if __name__ == "__main__":
    print("Crawler service running...", flush=True)
    crawl()
    while True: time.sleep(60)