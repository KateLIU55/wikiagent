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
from enum import auto
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
        "tiddlywiki/filesystem",
        "tiddlywiki/highlight"
    ],
    "themes": [
        "tiddlywiki/vanilla",
        "tiddlywiki/snowwhite"
    ],
    "languages": [
        "es-ES",
        "fr-FR",
        "en-US",
        "zh-Hans",
        "zh-Hant"
    ],
    "build": {
        "index": [
            "--render",
            "$:/plugins/tiddlywiki/tiddlyweb/save/offline",
            "index.html",
            "text/plain"
        ],
        "static": [
            "--render",
            "$:/core/templates/static.template.html",
            "static.html",
            "text/plain",
            "--render",
            "$:/core/templates/alltiddlers.template.html",
            "alltiddlers.html",
            "text/plain",
            "--render",
            "[!is[system]]",
            "[encodeuricomponent[]addprefix[static/]addsuffix[.html]]",
            "text/plain",
            "$:/core/templates/static.tiddler.html",
            "--render",
            "$:/core/templates/static.template.css",
            "static/static.css",
            "text/plain"
        ]
    }
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

            title = data.get("title") or json_path.stem

            en_summary   = (data.get("summary_en") or "").strip()
            hans_summary = (data.get("summary_zh_hans") or "").strip()
            hant_summary = (data.get("summary_zh_hant") or "").strip()

            body = (
                f"!! English Summary\n{en_summary}\n\n"
                f"!! 中文（简体）\n{hans_summary}\n\n"
                f"!! 中文（繁體）\n{hant_summary}"
            )
            if not body.strip():
                body = data.get("text") or "No summary available."

            tags = data.get("tags") or ["summary"]

            # English source (always present for crawled pages)
            en_source = (data.get("url") or "").strip()
            # Chinese source URL, if the article had a zh page
            zh_source = (data.get("zh_url") or "").strip()

            created = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            sid     = hashlib.sha1(title.encode("utf-8")).hexdigest()[:8]
            fname   = f"{slugify(title)}-{sid}.tid"
            tagstr  = " ".join(tags if isinstance(tags, list) else [str(tags)])

            # Build source line:
            #  always include English URL if available
            #  include Chinese URL iff there is a zh page *and* some Chinese summary text
            source_parts = []
            if en_source:
                source_parts.append(f"[[{en_source}]]")
            if zh_source and (hans_summary or hant_summary):
                source_parts.append(f"[[{zh_source}]]")

            if source_parts:
                source_line = "source: " + " ; ".join(source_parts)
            else:
                source_line = "source: unknown"

            tid = (
                f"title: {title}\n"
                f"tags: {tagstr}\n"
                f"type: text/vnd.tiddlywiki\n"
                f"created: {created}\n"
                f"modified: {created}\n\n"
                f"{body}\n\n"
                f"{source_line}\n"
            )

            (tiddlers_dir / fname).write_text(tid, encoding="utf-8")
            count += 1

        except Exception as e:
            print(f"[WARN] failed {json_path.name}: {e}", flush=True)

    print(f"[publisher] Created {count} tiddlers from {SUMMARY_DIR}")
    return count


def inject_tiddlers():
  # Create $:/SiteTitle and $:/SiteSubtitle tiddlers for branding
    tiddlers_dir = WIKI_WORKDIR / "tiddlers"
    tiddlers_dir.mkdir(parents=True, exist_ok=True)

    site_title = (
        "title: $:/SiteTitle\n"
        "type: text/vnd.tiddlywiki\n\n"
        "Nanjing Knowledge Hub Wiki\n"
    )
    site_subtitle = (
        "title: $:/SiteSubtitle\n"
        "type: text/vnd.tiddlywiki\n\n"
        "Nanjing Encyclopedia, with a strong duck flavor\n"
    )

    (tiddlers_dir / "__site-title.tid").write_text(site_title, encoding="utf-8")
    (tiddlers_dir / "__site-subtitle.tid").write_text(site_subtitle, encoding="utf-8")

# Creates the wiki by invoking TiddlyWiki CLI
def build_wiki():
    print("[publisher] Building editable wiki...", flush=True)
    ensure_tw_project()
    inject_tiddlers()

    # Create the tiddlers
    created = create_tiddlers()
    if created == 0:
        print("[publisher] No summaries found; nothing to publish.", flush=True)
        return

    outdir = WIKI_WORKDIR / "output"
    outdir.mkdir(parents=True, exist_ok=True)

    # Build the full folder-based wiki (not a static HTML)
    cmd = [
        "tiddlywiki", str(WIKI_WORKDIR),
        "--build", "index"
    ]

    print(f"[publisher] Running: {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True)

    # Copy to SITE_DIR
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "-r", str(outdir) + "/", str(SITE_DIR)], check=True)
    print(f"[publisher] Copied wiki folder to {SITE_DIR}", flush=True)


def main():
    print(f"[publisher] SUMMARY_DIR={SUMMARY_DIR} SITE_DIR={SITE_DIR}", flush=True)
    build_wiki()
   


if __name__ == "__main__":
    main()