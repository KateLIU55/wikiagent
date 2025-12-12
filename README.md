WikiAgent
Automated crawling → extraction → summarization → publishing of a multilingual TiddlyWiki site.
WikiAgent collects Wikipedia pages related to Nanjing, processes them across four services, generates multilingual summaries (English, Simplified Chinese, Traditional Chinese), and publishes a complete static wiki site.
All components are fully containerized, restart-safe, and designed for unattended scheduled operation.

1. Features Overview
Automated Web Crawling
Collects English and Chinese (zh-Hans, zh-Hant, zh-HK, others) Wikipedia articles
Detects updated pages using caching, etag, and last-modified
Revisits previously crawled pages safely and incrementally
Automatically filters out irrelevant, off-topic pages
Structured Content Extraction
Converts raw HTML into clean, standardized JSON
Extracts title, metadata, categories, language variants
Normalizes Chinese variants into a single canonical zh-article identity
Ensures stable identifiers for deduplication
LLM Summarization (English / Simplified Chinese / Traditional Chinese)
Uses your Local LLM or any Remote LLM (OpenAI-compatible API)
Strict multilingual logic:
English Summary
Prefer the English Wikipedia article
If missing → translate from Simplified Chinese
If Simplified missing → translate from Traditional Chinese
Simplified Chinese Summary
Prefer the actual Chinese Wikipedia article
If missing → translate from the English summary
Traditional Chinese Summary
If a Simplified summary exists → convert Hans → Hant
If no Chinese article exists → translate English directly into Hant
Deduplication (important for the client)
WikiAgent automatically removes duplicate variants caused by:
zh-Hans / zh-Hant / zh-HK / zh-SG / regional pages
Chinese redirects
Same topic appearing under multiple wiki URLs
Deduplication happens at:
Crawler level (URL canonicalization, database uniqueness)
Extractor level (canonical zh_url)
Summarizer level (skip summarizing duplicates)
Publisher level (dedupe by topic ID before creating tiddlers)
This guarantees one summary per topic, even if Wikipedia provides many regional variants.
Publisher → Static Website Generator
Converts summaries to TiddlyWiki tiddlers
Generates a full HTML wiki site
Clients can browse English/Chinese versions easily
Search, tags, linking, and categories are automatically generated
Automated Pipeline (Manual or Cron-Scheduled)
Complete sequence:
crawl → extract → summarize → publish
Can be run manually (./run_pipeline.sh)
Can run automatically via cron (1st & 15th of every month or customizable)
Fully restart-safe (safe after power loss, network interruption)

2. Project Architecture

The WikiAgent system is organized into four core micro-services plus top-level automation scripts. Each service runs inside its own Docker container and communicates through shared /data and /site volumes.
wikiagent/
│
├── run_pipeline.sh          # Full end-to-end automation script
├── install_cron.sh          # Installs scheduled automation (cron)
├── README.md
│
├── brain/                   # Local LLM Gateway (OpenAI-compatible API)
│   ├── app.py               # Routes requests to local/remote LLM
│   ├── Dockerfile
│   └── requirements.txt
│
├── config/
│   └── whitelist.yml        # Controls which topics/pages are allowed
│
├── crawler/                 # Wikipedia crawler
│   ├── app.py               # Fetch raw HTML + metadata
│   ├── Dockerfile
│   └── requirements.txt
│
├── extractor/               # Cleans + normalizes crawled pages
│   ├── app.py
│   ├── Dockerfile
│   └── requirements.txt
│
├── summarizer/              # Generates multilingual summaries
│   ├── app.py               # Uses the LLM via brain service
│   ├── Dockerfile
│   └── requirements.txt
│
├── publisher/               # Static TiddlyWiki generator
│   ├── app.py               # Build tiddlers, search, homepage
│   ├── Dockerfile
│   └── requirements.txt
│
├── data/                    # Shared volume for all services
│   ├── raw/                 # Raw HTML files
│   ├── clean/               # Extracted structured JSON
│   ├── summarized/          # Final multilingual summary JSON
│   └── wiki.sqlite          # Crawl metadata + link graph
│
└── site/                    # Final static wiki output
    ├── NKHW_logo.png         # Logo
    ├── index.html           # Homepage wrapper
    └── output/              # Published wiki (Tiddlers + HTML)



3. Getting Started
System Requirements
Docker 24+
Docker Compose v2+
macOS / Linux / WSL
Python 3.10+ only if you run internal tools manually
Project Dependencies (installed inside containers)
BeautifulSoup
requests
sqlite3
pydantic
TiddlyWiki
LLM client libraries
cron
\

4. Installation
Step 1 — Clone the Repository
git clone --branch main https://github.com/anjso/wikiagent.git wikiagent
cd wikiagent

Step 2 — Configure LLM Settings (brain service)
Edit in docker-compose.yml:
LLM_BASE_URL: "http://your-llm-server/v1"
LLM_API_KEY: "your-api-key"
LLM_MODEL: "your-model-id"

This is the only configuration required to switch between local and remote LLMs.
Step 3 — Run the Full Pipeline
./run_pipeline.sh

This performs:
Pull latest code
Rebuild Docker containers
Run each stage from scratch
Deduplicate summaries
Build the wiki site

5. Scheduled Automation (Cron)
Enable:
./install_cron.sh

Default behavior:
Runs automatically on the 1st and 15th at 6 AM (local time).
To modify the schedule:
Edit the CRON= line inside install_cron.sh
Re-install:
chmod +x install_cron.sh
./install_cron.sh
Verify:
crontab -l
Cron logs appear in:
logs/automation.log


6. Output Directory Structure
Stage
Directory
Description
Crawler
data/raw/
Raw HTML + metadata
Extracto r 
data/clean/
Canonical JSON extracted from HTML
Summarizer
data/summarized/
Final English + Chinese JSON summaries
Publisher
site/output/
Full wiki site (html, tiddlers)
Public Site
site/index.html
Homepage of the generated wiki

Production site is available at:
https://anjso.org/wikiagent/



7. FAQ (Client-Friendly)
1. When should I see the first tiddler?
With the current whitelist, you should expect to see the updated wiki after the whole pipeline has completed, not immediately when summarization begins. The first run is the slowest; later runs are faster because the system only processes pages that changed. You can monitor progress by looking at the log file on the deployment machine. 
It will show which step the pipeline is on (crawler, extractor, summarizer, publisher) and when it finishes. 
Once the publisher completes, your updated wiki becomes available under site/
2. How do I access the wiki on the machine?
The pipeline writes the final static wiki into:
site/

There are two ways to view it locally on the deployment machine:
Option 1 — Open the HTML file directly
double-click or open:
site/index.html  in any browser.
Option 2 — Serve it with a lightweight web server
From a terminal:
cd site/
python3 -m http.server 8000
Then open:
http://localhost:8000    in your browser.
This method is recommended because it mimics how GitHub Pages or any static host would serve the site.
Access wiki remotely — Use Github Pages
Current code uses gh-pages (contains static website files) for Github Pages, you can access wiki through: http://anjso.org/wikiagent/

3. What happens if a container stops? Is it safe to continue?
Yes — WikiAgent is fully restart-safe.
Crawler uses atomic writes and SQLite checks
Extractor can be re-run anytime without losing work
Summarizer skips already-summarized files using content_hash
Publisher can rebuild the site repeatedly with no issues
You do NOT need a cleaning job unless you intentionally want a full reset.
4. What if power is lost during summarization?
Just restart the pipeline:
./run_pipeline.sh

All incomplete summaries will be regenerated automatically.

8. Configuration
LLM Settings (docker-compose.yml)
LLM_BASE_URL
LLM_API_KEY
LLM_MODEL

Whitelist for Crawler
config/whitelist.yml controls:
seed URLs
maximum pages
depth
rate limits
language rules
include/exclude patterns

9. Development Notes (for maintainers)
Each service has its own Dockerfile
Logs and backups are stored under /data
Publisher supports branded logos, CSS overrides, custom homepage
Duplicate cleanup happens automatically at summarizer startup

10. Contributing
1. Fork the Repository
2. Create a Feature Branch
git checkout -b feature/my-change

3. Commit
git commit -m "Add: new feature"

4. Push
git push origin feature/my-change

5. Open Pull Request

11. Bug Reporting
Use GitHub Issues with the following template:
Title
Short description
Steps to Reproduce
1.
2.
3.
Expected Behavior
Actual Behavior
Logs / Screenshots
Include:
Your docker-compose.yml (omit secrets)
Which commit/branch you are on
Relevant pipeline logs

12. License
MIT License — free to use, modify, distribute.

13. Support
For customization, troubleshooting, or new features:
Please open a GitHub Issue.


14. Maintainer Notes
The following notes are intended for long-term maintainers of the WikiAgent system. These points do not affect normal usage but are helpful when diagnosing issues or updating the pipeline.
Safe to Restart at Any Time
All four services (crawler, extractor, summarizer, publisher) are idempotent.
If a container stops due to power loss or network issues, you can simply rerun ./run_pipeline.sh.
No special cleanup is required unless you intentionally want to clear old data.
Duplicate Prevention Logic
The crawler tracks visited URLs.
The extractor merges regional Chinese variants.
The summarizer deduplicates based on content_hash and zh_url.
The publisher merges by canonical title.
If duplicates appear, running the pipeline again after clearing problematic JSONs usually resolves it.
Updating LLM or Pipeline Logic
You may safely modify environment variables such as LLM_BASE_URL, LLM_MODEL, and summarizer rules.
Any updated logic will apply automatically the next time the pipeline runs.
Production Deployment & GitHub Pages
Site output is located in /site/output/.
If the repo uses GitHub Pages or a deployment workflow, the final site is updated whenever the pipeline commits new output into the designated branch (e.g., gh-pages or main).

