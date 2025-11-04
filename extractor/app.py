
#!/usr/bin/env python3
import os, json, glob, time, sqlite3, urllib.parse
from bs4 import BeautifulSoup
from sqlite3 import DatabaseError


DATA_DIR = os.getenv("DATA_DIR", "/data")
RAW_DIR  = os.path.join(DATA_DIR, "raw")     # <- matches crawler
OUT_DIR  = os.path.join(DATA_DIR, "clean")   # <- standard output dir
DB_PATH  = os.getenv("DB_PATH", "/data/wiki.sqlite")
INTERVAL = int(os.getenv("IDLE_INTERVAL", "30"))  # seconds

os.makedirs(OUT_DIR, exist_ok=True)

EXTRACTOR_SKIP_CATEGORIES = os.getenv("EXTRACTOR_SKIP_CATEGORIES", "0")  # 1: skip; 0: keep
EXTRACTOR_SKIP_LISTS      = os.getenv("EXTRACTOR_SKIP_LISTS", "1")
EXTRACTOR_MIN_CHARS       = int(os.getenv("EXTRACTOR_MIN_CHARS", "180"))

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

def classify_doc(title: str, url: str | None, soup: BeautifulSoup) -> str:
    base = doc_type_from_url(url)  # "category" or "article"/"unknown"
    if base == "category":
        return "category"

    t = (title or "").strip().lower()

    # Detect "List of ..." pages
    if t.startswith("list of "):
        return "list"

    # Disambiguation: common markers in enwiki
    if soup.select_one(".mw-disambig, #disambigbox"):
        return "disambiguation"

    # (Optional) look at page categories for extra signals
    cats = [a.get_text(" ", strip=True).lower()
            for a in soup.select("#mw-normal-catlinks a")]
    if any("disambiguation" in c for c in cats):
        return "disambiguation"
    if any(c.startswith("lists of ") or c.endswith(" lists") for c in cats):
        return "list"

    return "article" if url else "unknown"


def doc_type_from_url(url: str) -> str:
    if not url:
        return "unknown"
    path = urllib.parse.urlsplit(url).path
    # title is the bit after /wiki/
    title = path.split("/wiki/", 1)[-1] if "/wiki/" in path else path
    return "category" if title.startswith("Category:") else "article"

def db():
    # open read-only; safer alongside crawler writes
    try:
        return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    except Exception:
        # fallback if RO fails (e.g., file missing)
        return sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)


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

def extract_text(html_bytes: bytes) -> str:
    soup = BeautifulSoup(html_bytes, "lxml")  # faster/more robust than html.parser
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    node = soup.select_one("main, article") or soup.body or soup
    parts = [p.get_text(" ", strip=True) for p in node.find_all("p")]
    return "\n\n".join(p for p in parts if p)

def process_once() -> int:
    wrote = 0
    for html_path in sorted(glob.glob(os.path.join(RAW_DIR, "*.html"))):
        stem = os.path.splitext(os.path.basename(html_path))[0]
        out_path = os.path.join(OUT_DIR, f"{stem}.json")
        if os.path.exists(out_path):
            continue  # already processed

        # file might have been rotated/deleted between list and open
        try:
            with open(html_path, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            print(f"[extractor] raw disappeared: {html_path} (skipping)", flush=True)
            continue

        #  nicer title from the page 
        soup = BeautifulSoup(raw, "lxml")
        h1 = soup.select_one("#firstHeading") or soup.find("h1")
        fallback = (soup.title.string if soup.title and soup.title.string else "").strip()
        if fallback.endswith(" - Wikipedia"):
            fallback = fallback[:-len(" - Wikipedia")]
        title = (h1.get_text(" ", strip=True) if h1 else (fallback or stem))

        text = extract_text(raw)

        # page_id from filename
        try:
            page_id = int(stem)
        except ValueError:
            page_id = None

        #  DB lookup with safe fallback to canonical link 
        url, retrieved_at = None, None

        meta = load_meta(page_id) if page_id is not None else None
        if meta:
            url = meta.get("url")
            retrieved_at = meta.get("fetched_at")
        else:
            # fall back to DB (read-only) and then canonical link in HTML
            try:
                if page_id is not None:
                    url, retrieved_at = url_and_last_ok(page_id)
            except DatabaseError as e:
                print(f"[extractor] DB malformed, deriving URL from HTML (page_id={page_id}): {e}", flush=True)
                url = url_from_raw_html(raw)
            except Exception as e:
                print(f"[extractor] DB lookup failed (page_id={page_id}): {e}", flush=True)
                url = url_from_raw_html(raw)


        doc_type = classify_doc(title, url, soup)

        # Skip low-value pages
        too_short = len((text or "").strip()) < EXTRACTOR_MIN_CHARS
        if ((EXTRACTOR_SKIP_CATEGORIES == "1" and doc_type == "category")
            or (EXTRACTOR_SKIP_LISTS == "1" and doc_type == "list")
            or doc_type == "disambiguation"
            or not url                         # missing URL
            or too_short):                     # tiny boilerplate
            print(f"[extractor] skip {doc_type} page_id={page_id} url={url} chars={len(text or '')}", flush=True)
            continue

        out = {
            "page_id": page_id,
            "url": url,
            "title": title,
            "content": text,
            "retrieved_at": retrieved_at,
            "doc_type": doc_type,
        }

        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as o:
            json.dump(out, o, ensure_ascii=False, indent=2)
        print(f"[extractor] wrote {out_path}", flush=True)
        wrote += 1
    return wrote


if __name__ == "__main__":
    print("Extractor service running...", flush=True)
    while True:
        n = process_once()
        if n == 0:
            time.sleep(INTERVAL)
