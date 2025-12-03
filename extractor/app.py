
#!/usr/bin/env python3
import os, json, glob, time, sqlite3, urllib.parse, re, sys
from bs4 import BeautifulSoup
from sqlite3 import DatabaseError
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


DATA_DIR = os.getenv("DATA_DIR", "/data")
RAW_DIR  = os.path.join(DATA_DIR, "raw")
OUT_DIR  = os.path.join(DATA_DIR, "clean")
DB_PATH  = os.getenv("DB_PATH", "/data/wiki.sqlite")
INTERVAL = int(os.getenv("IDLE_INTERVAL", "30"))  # seconds

def ensure_out_dir() -> None:
    """Create OUT_DIR if possible, but don't crash on read-only filesystems."""
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
    except OSError as e:
        # On macOS running tests, /data may be read-only (Errno 30).
        # In that case, skip creating it instead of crashing import.
        if getattr(e, "errno", None) == 30:
            return
        raise
        
EXTRACTOR_SKIP_CATEGORIES = os.getenv("EXTRACTOR_SKIP_CATEGORIES", "0")
EXTRACTOR_SKIP_LISTS      = os.getenv("EXTRACTOR_SKIP_LISTS", "1")
EXTRACTOR_MIN_CHARS       = int(os.getenv("EXTRACTOR_MIN_CHARS", "180"))

UA = "ANJSO-Extractor/1.0 (+https://anjso.org/wiki)"
HTTP_TIMEOUT = 20

def load_meta(page_id: int):
    p = os.path.join(RAW_DIR, f"{page_id}.meta.json")
    if os.path.exists(p):
        try:
            return json.loads(open(p, "r", encoding="utf-8").read())
        except Exception:
            return None
    return None

def url_from_raw_html(raw: bytes) -> str | None:
    soup = BeautifulSoup(raw, "lxml")
    link = soup.find("link", rel="canonical")
    href = (link.get("href") if link else None) or ""
    return href if href.startswith("http") else None

def doc_type_from_url(url: str) -> str:
    if not url:
        return "unknown"
    path = urllib.parse.urlsplit(url).path
    title = path.split("/wiki/", 1)[-1] if "/wiki/" in path else path
    return "category" if title.startswith("Category:") else "article"

def classify_doc(title: str, url: str | None, soup: BeautifulSoup) -> str:
    base = doc_type_from_url(url)
    if base == "category":
        return "category"

    t = (title or "").strip().lower()
    if t.startswith("list of "):
        return "list"

    if soup.select_one(".mw-disambig, #disambigbox"):
        return "disambiguation"

    cats = [a.get_text(" ", strip=True).lower()
            for a in soup.select("#mw-normal-catlinks a")]
    if any("disambiguation" in c for c in cats):
        return "disambiguation"
    if any(c.startswith("lists of ") or c.endswith(" lists") for c in cats):
        return "list"

    return "article" if url else "unknown"

def db():
    try:
        return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    except Exception:
        return sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)

def load_raw_html_by_url(url: str) -> bytes | None:
    """
    Try to load the HTML for a page from the local SQLite DB + raw/ directory,
    using the URL as key.
    """
    try:
        conn = db()
        try:
            row = conn.execute("SELECT id FROM pages WHERE url=?", (url,)).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        page_id = row[0]
        path = os.path.join(RAW_DIR, f"{page_id}.html")
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
    except Exception as e:
        print(f"[extractor] load_raw_html_by_url error for {url}: {e!r}", flush=True)
    return None


def html_for_url(url: str) -> bytes | None:
    """
    Prefer local copy from crawler (raw/*.html). If we don't have it,
    fall back to a direct HTTP fetch.
    """
    html = load_raw_html_by_url(url)
    if html:
        print(f"[extractor] html_for_url using local raw copy for {url}", flush=True)
        return html

    print(f"[extractor] html_for_url falling back to HTTP for {url}", flush=True)
    return fetch_html(url)


def url_and_last_ok(page_id: int):
    conn = db()
    try:
        url = conn.execute("SELECT url FROM pages WHERE id=?", (page_id,)).fetchone()
        url = url[0] if url else None
        ts  = conn.execute(
            "SELECT MAX(fetched_at) FROM fetch_log WHERE page_id=? AND status=200",
            (page_id,)
        ).fetchone()
        return url, (ts[0] if ts else None)
    finally:
        conn.close()

def extract_text_from_soup(soup: BeautifulSoup) -> str:
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    node = soup.select_one("main, article") or soup.body or soup
    parts = [p.get_text(" ", strip=True) for p in node.find_all("p")]
    return "\n\n".join(p for p in parts if p)

def extract_text(html_bytes: bytes) -> str:
    return extract_text_from_soup(BeautifulSoup(html_bytes, "lxml"))

# interlanguage & Chinese fetching 

def find_interlanguage_links(soup: BeautifulSoup) -> dict:
    """
    Return possible Chinese links:
    - zh (generic), zh-Hans (simplified), zh-Hant (traditional)
    Prefer explicit zh-Hans/zh-Hant when present.
    """
    out = {"zh": None, "zh_hans": None, "zh_hant": None}
    # Sidebar language links
    for a in soup.select("#p-lang a[hreflang], #p-lang a[lang]"):
        lang = (a.get("hreflang") or a.get("lang") or "").lower()
        href = a.get("href")
        if not href or not href.startswith("http"):
            continue
        if lang == "zh-hans":
            out["zh_hans"] = href
        elif lang == "zh-hant":
            out["zh_hant"] = href
        elif lang == "zh":
            out["zh"] = href

    # Fallback: languages dropdown sometimes outside #p-lang on mobile
    if not any(out.values()):
        for a in soup.select("a[hreflang^='zh'], a[lang^='zh']"):
            lang = (a.get("hreflang") or a.get("lang") or "").lower()
            href = a.get("href")
            if not href or not href.startswith("http"):
                continue
            if "hant" in lang:
                out["zh_hant"] = href
            elif "hans" in lang:
                out["zh_hans"] = href
            else:
                out["zh"] = href

    return out

def fetch_html(url: str) -> bytes | None:
    """
    Simple HTML fetcher using stdlib urllib; used as a fallback
    if we don't have the page stored locally.
    """
    try:
        req = Request(url, headers={"User-Agent": UA})
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" not in ctype:
                print(f"[extractor] fetch_html non-html ctype={ctype} url={url}", flush=True)
                return None
            return resp.read()
    except (HTTPError, URLError) as e:
        print(f"[extractor] fetch_html error for {url}: {e!r}", flush=True)
    except Exception as e:
        print(f"[extractor] fetch_html unexpected error for {url}: {e!r}", flush=True)
    return None


def chinese_variants_from_en_html(en_html: bytes) -> tuple[str | None, str | None, str | None, str | None]:
    """
    From an English page html, try to locate and fetch Chinese variants.
    Returns: (zh_url, hans_title, hans_text, hant_text)
    """
    soup = BeautifulSoup(en_html, "lxml")
    links = find_interlanguage_links(soup)

    zh_url = links.get("zh_hans") or links.get("zh") or links.get("zh_hant")
    zh_hant_url = links.get("zh_hant")

    hans_title = None
    hans_text  = None
    hant_text  = None

    # Fetch Simplified
    if zh_url:
        zh_html = html_for_url(zh_url)
        if zh_html:
            zhsoup = BeautifulSoup(zh_html, "lxml")
            title_node = zhsoup.select_one("#firstHeading") or zhsoup.find("h1")
            hans_title = (title_node.get_text(" ", strip=True) if title_node else None)
            hans_text  = extract_text_from_soup(zhsoup)
        else:
            print(f"[extractor] no HTML for zh_url={zh_url}", flush=True)

    # Fetch Traditional (true variant) if available
    if zh_hant_url:
        hant_html = html_for_url(zh_hant_url)
        if hant_html:
            htsoup = BeautifulSoup(hant_html, "lxml")
            hant_text = extract_text_from_soup(htsoup)
        else:
            print(f"[extractor] no HTML for zh_hant_url={zh_hant_url}", flush=True)

    return zh_url, hans_title, hans_text, hant_text


def process_once() -> int:
    wrote = 0
    for html_path in sorted(glob.glob(os.path.join(RAW_DIR, "*.html"))):
        stem = os.path.splitext(os.path.basename(html_path))[0]
        out_path = os.path.join(OUT_DIR, f"{stem}.json")

        # determine page_id & load meta early so we can see content_hash
        try:  # compute page_id from filename
            page_id = int(stem)
        except ValueError:
            page_id = None

        # Load meta once here and derive current_hash
        meta = load_meta(page_id) if page_id is not None else None
        current_hash = meta.get("content_hash") if meta else None

        # incremental extraction – skip if content_hash unchanged
        if os.path.exists(out_path):
            try:
                existing = json.loads(open(out_path, "r", encoding="utf-8").read())
                existing_hash = existing.get("content_hash")
            except Exception:
                existing_hash = None

            if current_hash and existing_hash and existing_hash == current_hash:
                print(
                    f"[extractor] unchanged content_hash for page_id={page_id} "
                    f"({stem}); skipping re-extract",
                    flush=True,
                )
                continue





        try:
            with open(html_path, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            print(f"[extractor] raw disappeared: {html_path} (skipping)", flush=True)
            continue

        soup = BeautifulSoup(raw, "lxml")
        h1 = soup.select_one("#firstHeading") or soup.find("h1")
        fallback = (soup.title.string if soup.title and soup.title.string else "").strip()
        if fallback.endswith(" - Wikipedia"):
            fallback = fallback[:-len(" - Wikipedia")]
        title = (h1.get_text(" ", strip=True) if h1 else (fallback or stem))

        text = extract_text_from_soup(soup)

        # id & metadata
        # meta already loaded earlier; reuse it here
        url, retrieved_at = None, None
        content_hash = current_hash  # track content_hash from meta

        if meta:
            url = meta.get("url")
            retrieved_at = meta.get("fetched_at")
        else:
            try:
                if page_id is not None:
                    url, retrieved_at = url_and_last_ok(page_id)
            except DatabaseError:
                url = url_from_raw_html(raw)
            except Exception:
                url = url_from_raw_html(raw)

        doc_type = classify_doc(title, url, soup)

        too_short = len((text or "").strip()) < EXTRACTOR_MIN_CHARS
        if ((EXTRACTOR_SKIP_CATEGORIES == "1" and doc_type == "category")
            or (EXTRACTOR_SKIP_LISTS == "1" and doc_type == "list")
            or doc_type == "disambiguation"
            or not url
            or too_short):
            print(f"[extractor] skip {doc_type} page_id={page_id} url={url} chars={len(text or '')}", flush=True)
            continue

        # NEW: pull Chinese variants (if they exist)
        # Basic language flag from domain
        lang = "zh" if ("zh.wikipedia.org" in (url or "")) else "en"

        zh_url = zh_title_hans = content_zh_hans = content_zh_hant = None

        if "en.wikipedia.org" in url:
            # We’re on the English page; try to locate and fetch Chinese siblings
            zh_url, zh_title_hans, content_zh_hans, content_zh_hant = chinese_variants_from_en_html(raw)
        elif "zh.wikipedia.org" in url:
            # We’re already on the Chinese page; treat this content as Simplified Chinese by default
            content_zh_hans = text
            zh_url = url
            zh_title_hans = title


        # Collect raw Wikipedia categories (for tagging later)
        raw_categories = [
            a.get_text(" ", strip=True)
            for a in soup.select("#mw-normal-catlinks a")
        ]

        out = {
            "page_id": page_id,
            "url": url,
            "title": title,
            "lang": lang,
            "content": text,
            "retrieved_at": retrieved_at,
            "doc_type": doc_type,

            # NEW fields
            "zh_url": zh_url,
            "zh_title_hans": zh_title_hans,
            "content_zh_hans": content_zh_hans,
            "content_zh_hant": content_zh_hant,
            "categories": raw_categories,
            "content_hash": content_hash,
        }

        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as o:
            json.dump(out, o, ensure_ascii=False, indent=2)
        print(f"[extractor] wrote {out_path}", flush=True)
        wrote += 1
    return wrote

# to make automation + services play nicely together
RUN_ONCE = os.getenv("RUN_ONCE") == "1"

if __name__ == "__main__":
    ensure_out_dir()
    print("Extractor service running...", flush=True)
    if RUN_ONCE:
        process_once()
        sys.exit(0)
    else:
        while True:
            n = process_once()
            if n == 0:
                time.sleep(INTERVAL)