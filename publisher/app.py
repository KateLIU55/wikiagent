# Author: Marcelo Villalobos, Juan Quintana
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
        ]
    }
}
    (WIKI_WORKDIR / "tiddlywiki.info").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print("[publisher] Created /tmp/wiki/tiddlywiki.info", flush=True)

# Create a homepage that leads to the wiki site using a search bar.
# Includes inline CSS and JavaScript
def create_homepage():
    html = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<title>Nanjing Knowledge Hub</title>
<meta name="viewport" content="width=device-width,initial-scale=1" />
<style>
    /* ===== COLOR THEME (matches TiddlyWiki SnowWhite) ===== */
    :root {
        --bg: #eef1f7;           /* soft light gray */
        --card: #ffffff;         /* white card */
        --accent: #182955;       /* updated dark blue */
        --accent-light: #e7ecff; /* soft blue hover */
        --text-muted: #666;
        --border: #d9dce3;
        --shadow: rgba(0,0,0,0.12);
    }

    body {
        margin: 0;
        font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica Neue, Arial;
        background: #f4f4f4;
        color: #222;

        /* Center everything vertically + horizontally */
        min-height: 100vh;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        padding: 20px;
        box-sizing: border-box;
    }

    h1 {
        margin: 0 0 20px;
        font-weight:var(--accent-light);
        text-align: center;
        font-size: 48px;
        color: var(--accent);
        letter-spacing: 0.3px;
    }

    hr {
        width: 100%;
        max-width: 650px;
        height: 3px;
        background: var(--accent);
        border: none;
        margin: 8px 0 0;
        border-radius: 2px;
    }

    .wrapper {
        background: var(--card);
        width: 100%;
        max-width: 650px;
        padding: 36px 32px 48px;
        border-radius: 18px;
        box-shadow: 0 4px 20px var(--shadow);
        animation: fadeIn 0.4s ease;
    }

    @keyframes fadeIn {
        from { opacity: 0; transform: translateY(10px); }
        to   { opacity: 1; transform: translateY(0); }
    }

    p.lead {
        margin: 0 0 22px;
        text-align: center;
        color: var(--text-muted);
        font-size: 15px;
    }

    /* ===== SEARCH AREA ===== */
    .search-container {
        position: relative;
        width: 100%;
    }

    #search {
        width: 100%;
        padding: 14px 130px 14px 16px;
        font-size: 16px;
        border-radius: 10px;
        border: 1px solid var(--border);
        background: var(--card);
        box-shadow: 0 2px 4px rgba(0,0,0,0.06);
        box-sizing: border-box;
        transition: all 0.2s ease;
    }

    #search:focus {
        outline: none;
        border-color: var(--accent);
        box-shadow: 0 0 0 3px var(--accent-light);
    }

    /* ===== OPEN BUTTON ===== */
    #openBtn {
        position: absolute;
        right: 6px;
        top: 6px;
        height: calc(100% - 12px);
        padding: 0 18px;
        border-radius: 8px;
        background: var(--accent);
        color: white;
        border: none;
        font-size: 15px;
        display: flex;
        align-items: center;
        justify-content: center;
        cursor: pointer;
        transition: background-color 0.15s ease;
    }

    #openBtn:hover {
        background: #0f1b38;
    }

    #openBtn.hidden {
        opacity: 0;
        pointer-events: none;
    }

    /* ===== DROPDOWN RESULTS ===== */
    #results {
        position: absolute;
        left: 0;
        right: 0;
        top: 54px;
        max-height: 260px;
        overflow-y: auto;
        background: var(--card);
        border-radius: 10px;
        border: 1px solid var(--border);
        box-shadow: 0 6px 16px var(--shadow);
        display: none;
        z-index: 60;
    }

    .result-item {
        padding: 12px 14px;
        cursor: pointer;
        transition: background-color 0.12s ease;
        border-bottom: 1px solid var(--border);
    }

    .result-item:last-child {
        border-bottom: none;
    }

    .result-item:hover,
    .result-item.active {
        background: var(--accent-light);
    }

    .no-results {
        padding: 12px 14px;
        color: var(--text-muted);
        font-style: italic;
    }
</style>
</head>

<body>
<h1>Nanjing Knowledge Hub Wiki</h1>
<hr>
<br><br/>
<div class="wrapper">
    <p class="lead">Search articles and open them directly in the wiki.</p>

    <div class="search-container">
        <input id="search" autocomplete="off" placeholder="Start typing a topic…">
        <button id="openBtn" class="hidden">Open</button>
        <div id="results"></div>
    </div>
</div>

<script>
// Load index.json
async function loadIndex() {
    const res = await fetch("output/tiddlers/index.json");
    return res.json();
}

let TIDDLERS = [];
let filtered = [];
let activeIndex = -1;

const input = document.getElementById("search");
const results = document.getElementById("results");
const openBtn = document.getElementById("openBtn");

function fuzzyScore(str, query){
    str = str.toLowerCase();
    query = query.toLowerCase();
    let score = 0, i = 0;

    for(const q of query){
        const pos = str.indexOf(q, i);
        if(pos === -1) return 0;
        score += Math.max(0, 12 - (pos - i));
        i = pos + 1;
    }
    if(str.includes(query)) score += 10;
    return score;
}

function render(){
    results.innerHTML = "";

    if(!input.value.trim()){
        results.style.display = "none";
        openBtn.classList.add("hidden");
        return;
    }

    if(filtered.length === 0){
        results.innerHTML = "<div class='no-results'>No results found</div>";
        results.style.display = "block";
        openBtn.classList.add("hidden");
        return;
    }

    filtered.forEach((t, idx) => {
        const div = document.createElement("div");
        div.className = "result-item" + (idx === activeIndex ? " active" : "");
        div.textContent = t;
        div.addEventListener("click", () => select(idx));
        results.appendChild(div);
    });

    results.style.display = "block";
    openBtn.classList.remove("hidden");
}

function select(idx){
    if(idx < 0 || idx >= filtered.length) return;

    const title = filtered[idx];
    input.value = title;

    const frag = encodeURIComponent(title);

    window.location.href = `output/index.html#${frag}`;
}

input.addEventListener("input", () => {
    const q = input.value.trim();
    activeIndex = 0;

    if(!q){
        filtered = [];
        render();
        return;
    }

    const scored = TIDDLERS.map(t => ({t, score: fuzzyScore(t, q)}))
        .filter(x => x.score > 0)
        .sort((a,b) => b.score - a.score);

    filtered = scored.map(x => x.t);
    render();
});

input.addEventListener("keydown", e => {
    if(!["ArrowDown","ArrowUp","Enter","Escape"].includes(e.key)) return;

    if(e.key === "ArrowDown"){
        e.preventDefault();
        if(filtered.length === 0) return;
        activeIndex = Math.min(activeIndex + 1, filtered.length - 1);
        highlightScroll();
    }
    if(e.key === "ArrowUp"){
        e.preventDefault();
        if(filtered.length === 0) return;
        activeIndex = Math.max(activeIndex - 1, 0);
        highlightScroll();
    }
    if(e.key === "Enter"){
        e.preventDefault();
        if(activeIndex >= 0 && filtered.length) select(activeIndex);
    }
    if(e.key === "Escape"){
        results.style.display = "none";
    }
});

function highlightScroll(){
    const items = results.children;
    for(let i = 0; i < items.length; i++){
        items[i].classList.toggle("active", i === activeIndex);
    }
    const el = items[activeIndex];
    if(el) el.scrollIntoView({block: "nearest"});
}

openBtn.addEventListener("click", () => {
    if(filtered.length && activeIndex >= 0) select(activeIndex);
});

document.addEventListener("click", ev => {
    if(!ev.target.closest(".search-container")){
        results.style.display = "none";
    }
});

(async () => {
    TIDDLERS = await loadIndex();
})();
</script>

</body>
</html>
"""
    (SITE_DIR / "index.html").write_text(html, encoding="utf-8")
    print("[publisher] Created homepage with search function.")

# Plugin for use with homepage, opens advance search to find tiddler.
def inject_search_handler():

    plugin_dir = WIKI_WORKDIR / "tiddlers" / "plugins" / "external-search"
    plugin_dir.mkdir(parents=True, exist_ok=True)

    tiddler_path = plugin_dir / "startup.tid"
    content = """
title: $:/plugins/external-search/startup
type: application/javascript
module-type: startup

(function(){

exports.name = "external-search-startup";
exports.after = ["startup"];
exports.platforms = ["browser"];

exports.startup = function() {

    // TW only reads the hash AFTER '#' — no '?', no '&'
    var hash = window.location.hash || "";

    // look for pattern #extsearch:Topic
    if(!hash.startsWith("#extsearch:")) return;

    var query = decodeURIComponent(hash.substring("#extsearch:".length)).trim();
    if(!query) return;

    console.log("[ExternalSearch] Searching:", query);

    // Open AdvancedSearch panel, "search" tab
    $tw.wiki.setText("$:/temp/AdvancedSearch/Tab","text",null,"search");

    // Set search query
    $tw.wiki.setText("$:/temp/AdvancedSearch/Input","text",null,query);

    // Get matching tiddlers
    var results = $tw.wiki.filterTiddlers("[search[" + query + "]]");

    // If exactly 1 → auto-open
    if(results.length === 1) {
        $tw.rootWidget.invokeActionString(
            "<$action-navigate $to='" + results[0] + "' />"
        );
    }
};
})();
"""
    tiddler_path.write_text(content.strip(), encoding="utf-8")
    print("[publisher] Injected external search handler", flush=True)

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

# Scan all .tid files to create index.json to use with homepage.
def generate_tiddler_index():
    
    tiddlers_dir = WIKI_WORKDIR / "tiddlers"
    titles = []

    for tid in tiddlers_dir.glob("*.tid"):
        try:
            text = tid.read_text(encoding="utf-8")
            match = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
            if match:
                titles.append(match.group(1).strip())
        except:
            pass

    # Sort titles for consistency
    titles.sort()

    output_dir = SITE_DIR / "output" / "tiddlers"
    output_dir.mkdir(parents=True, exist_ok=True)

    index_path = output_dir / "index.json"
    index_path.write_text(json.dumps(titles, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[publisher] Created tiddler index with {len(titles)} titles")

# Create $:/SiteTitle and $:/SiteSubtitle tiddlers for Headings
def inject_tiddlers():
  
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
    print("[publisher] Building wiki...", flush=True)
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
    print(f"[publisher] Copied wiki to {SITE_DIR}/output", flush=True)


def main():
    print(f"[publisher] SUMMARY_DIR={SUMMARY_DIR} SITE_DIR={SITE_DIR}", flush=True)
    build_wiki()
    generate_tiddler_index()
    create_homepage()
    inject_search_handler()
    print("[publisher] Done.", flush=True)


if __name__ == "__main__":
    main()