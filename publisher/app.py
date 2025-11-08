# **Author: Marcelo Villalobos Diaz, Juan Quintana**
# **Date: November 2025**

# print("Publisher service running...")

# import os, time, sys, signal

# def _graceful_exit(signum, frame):
#     print("Shutting down...", flush=True)
#     sys.exit(0)

# signal.signal(signal.SIGINT, _graceful_exit)   # Ctrl+C / docker stop
# signal.signal(signal.SIGTERM, _graceful_exit)  # docker stop

# def keep_alive(name="service"):
#     interval = int(os.getenv("IDLE_INTERVAL", "60"))  # seconds
#     print(f"{name} idle loop started (interval={interval}s)", flush=True)
#     while True:
#         time.sleep(interval)

# if __name__ == "__main__":
#     print("Publisher service running...", flush=True)
#     keep_alive(name="publisher")

# Adding this for the publisher section

#!/usr/bin/env python3
import os, json, subprocess, hashlib, re
from pathlib import Path
from datetime import datetime

# Read environment variables for directories
DATA_DIR     = Path(os.getenv("DATA_DIR", "/data"))
SUMMARY_DIR  = Path(os.getenv("SUMMARY_DIR", str(DATA_DIR / "summarized")))
SITE_DIR     = Path(os.getenv("SITE_DIR", "/site"))
WIKI_WORKDIR = Path(os.getenv("WIKI_WORKDIR", "/tmp/wiki"))

# create url-friendly for filenames
_slug_re = re.compile(r"[^a-z0-9-_]")
def slugify(s: str) -> str:
    s = (s or "untitled").lower().strip().replace(" ", "-")
    s = _slug_re.sub("-", s)
    return re.sub(r"-{2,}", "-", s)[:80]

# ensure Tiddlywiki project structure exists, create a tiddlywiki.info file
def ensure_tw_project():
    (WIKI_WORKDIR / "tiddlers").mkdir(parents=True, exist_ok=True)
    info = {
        "description": "Auto-generated wiki",
        "plugins": [
            "tiddlywiki/tiddlyweb",
            "tiddlywiki/filesystem"
        ]
    }
    (WIKI_WORKDIR / "tiddlywiki.info").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print("[publisher] Created /tmp/wiki/tiddlywiki.info", flush=True)

# create tiddlers from JSON summaries, build .tid files
def create_tiddlers() -> int:
    tiddlers_dir = WIKI_WORKDIR / "tiddlers"
    tiddlers_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for json_path in Path(SUMMARY_DIR).glob("*.json"):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8-sig"))
            title   = data.get("title") or json_path.stem
            body    = body = (
                        f"!! English Summary\n{data.get('summary_en','')}\n\n"
                        f"!! 中文（简体）\n{data.get('summary_zh_hans','')}\n\n"
                        f"!! 中文（繁體）\n{data.get('summary_zh_hant','')}"
                        ) or data.get("text") or "No summary available."
            tags    = data.get("tags") or ["summary"]
            source  = data.get("url") or "unknown"
            created = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            sid     = hashlib.sha1(title.encode("utf-8")).hexdigest()[:8]
            fname   = f"{slugify(title)}-{sid}.tid"
            tagstr  = " ".join(tags if isinstance(tags, list) else [str(tags)])
            tid = (
                f"title: {title}\n"
                f"tags: {tagstr}\n"
                f"type: text/vnd.tiddlywiki\n"
                f"created: {created}\n"
                f"modified: {created}\n\n"
                f"{body}\n\n"
                f"source: [[{source}]]\n"
            )
            (tiddlers_dir / fname).write_text(tid, encoding="utf-8")
            count += 1
        except Exception as e:
            print(f"[WARN] failed {json_path.name}: {e}", flush=True)
    print(f"[publisher] Created {count} tiddlers from {SUMMARY_DIR}")
    return count

# inject theme, style, and site title before building
def inject_theme_tiddlers():

    tiddlers_dir = WIKI_WORKDIR / "tiddlers"
    tiddlers_dir.mkdir(parents=True, exist_ok=True)

    # Theme selection
    theme_tid = tiddlers_dir / "$__theme.tid"
    theme_tid.write_text(
        "title: $:/theme\n"
         "type: text/vnd.tiddlywiki\n\n"
        "$:/themes/tiddlywiki/vanilla\n",
        encoding="utf-8"
    )

    # Custom stylesheet
    style_tid = tiddlers_dir / "$__themes__custom__anjso__style.tid"
    style_css = """title: $:/themes/custom/anjso/style.css
tags: [[$:/tags/Stylesheet]]
type: text/css

/* Base Page Styles */
body {
  font-family: "Merriweather", "Georgia", serif;
  background-color: #f9f4ef; /* warm cream */
  color: #3b2f2f; /* soft brown */
  max-width: 960px;
  margin: 0 auto;
  line-height: 1.7;
  padding: 2em;
  background-image: linear-gradient(to bottom, #f9f4ef, #f1e8dc);
}

/* Wiki body background */
html body.tc-body {
  background: linear-gradient(180deg, #f8f1e7, #f3e3d3);
}

/* Tiddler Frames */
.tc-tiddler-frame {
  background: #fff8f2;
  border: 1px solid #e6cbb0;
  border-radius: 12px;
  padding: 1.6em;
  margin-top: 1.4em;
  box-shadow: 0 3px 10px rgba(139, 69, 19, 0.15);
  transition: transform 0.1s ease, box-shadow 0.2s ease;
}
.tc-tiddler-frame:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 14px rgba(139, 69, 19, 0.2);
}

/* Headings */
h1, h2, h3 {
  color: #7c3f2c; /* terracotta */
  font-family: "Playfair Display", "Georgia", serif;
}
.tc-subtitle {
  color: #9c6b4e;
}

/* Links */
a {
  color: #b15e36; /* warm orange-brown */
  text-decoration: none;
  font-weight: 500;
}
a:hover {
  color: #e06e3c;
  text-decoration: underline;
}

/* Toolbar Buttons */
.tc-btn {
  background-color: #e8c7a1;
  color: #3b2f2f;
  border-radius: 6px;
  border: none;
  padding: 0.3em 0.7em;
  font-weight: 500;
}
.tc-btn:hover {
  background-color: #d8b58f;
  color: #2b1f1f;
}

/* Sidebar */
.tc-sidebar {
  background-color: #f5ede3;
  border-left: 1px solid #e0c9af;
  padding: 1em;
}
.tc-sidebar-tabs .tc-tab-label {
  color: #7c3f2c;
}
.tc-sidebar-tabs .tc-tab-label.tc-selected {
  border-bottom: 2px solid #b15e36;
  font-weight: bold;
}

/* Search box and filters */
.tc-search-input, .tc-filter-input {
  background: #fff8f2;
  border: 1px solid #e6cbb0;
  border-radius: 6px;
  padding: 0.4em;
  color: #3b2f2f;
}

/* Page Title */
.tc-site-title {
  font-family: "Playfair Display", "Georgia", serif;
  color: #7c3f2c;
  font-size: 2.2em;
  text-align: center;
  margin-top: 0.4em;
}
.tc-site-subtitle {
  text-align: center;
  color: #a88363;
  font-style: italic;
}

/* Footer / credits */
.tc-footer {
  text-align: center;
  font-size: 0.8em;
  color: #8b7765;
  margin-top: 2em;
  padding-top: 1em;
  border-top: 1px solid #e0c9af;
}

/* Misc */
blockquote {
  border-left: 4px solid #e0b59b;
  padding-left: 1em;
  color: #5a4636;
  background: #fffaf5;
}
code {
  background: #f3e3d3;
  padding: 0.15em 0.4em;
  border-radius: 4px;
}
""" 
    style_tid.write_text(style_css, encoding="utf-8")
    print("[publisher] Injected theme and stylesheet tiddlers", flush=True)

    # Custom site title
    site_title_tid = tiddlers_dir / "$__SiteTitle.tid"
    site_title_tid.write_text(
        "title: $:/SiteTitle\n"
        "type: text/vnd.tiddlywiki\n\n"
        "Nanjing Knowledge Hub Wiki",
        encoding="utf-8"
    )

    # Custom site subtitle
    site_subtitle_tid = tiddlers_dir / "$__SiteSubtitle.tid"
    site_subtitle_tid.write_text(
        "title: $:/SiteSubtitle\n"
        "type: text/vnd.tiddlywiki\n\n"
        "Nanjing Encyclopedia, with a strong duck flavor",
        encoding="utf-8"
    )

# Creates the wiki by invoking TiddlyWiki CLI
def build_wiki():
    print("[publisher] Building wiki…", flush=True)
    ensure_tw_project()

    outdir = WIKI_WORKDIR / "output"
    outdir.mkdir(parents=True, exist_ok=True)

    # Directly render the full single-file wiki
    cmd = [
        "tiddlywiki", str(WIKI_WORKDIR),
        "--rendertiddler", "$:/core/save/all",
        str(outdir / "index.html"), "text/plain"
    ]

    print(f"[publisher] Running: {' '.join(cmd)}", flush=True)

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[publisher] ERROR: TiddlyWiki render failed: {e}", flush=True)
        return

    built_html = outdir / "index.html"
    SITE_DIR.mkdir(parents=True, exist_ok=True)

    if built_html.exists():
        (SITE_DIR / "index.html").write_text(built_html.read_text(encoding="utf-8"))
        print(f"[publisher] Wrote wiki to {SITE_DIR / 'index.html'}", flush=True)
    else:
        print("[publisher] ERROR: index.html was not generated or is empty", flush=True)

# Main function prints current dirs, creates tiddlers, and builds the wiki
def main():
    print(f"[publisher] SUMMARY_DIR={SUMMARY_DIR} SITE_DIR={SITE_DIR}", flush=True)
    created = create_tiddlers()
    if created == 0:
        print("[publisher] No summaries found; nothing to publish.", flush=True)
        return
    inject_theme_tiddlers()  # add custom theme before building wiki.
    build_wiki()

if __name__ == "__main__":
    main()