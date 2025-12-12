# WikiAgent

Automated crawling → extraction → summarization → publishing of a multilingual TiddlyWiki site.
WikiAgent collects Wikipedia pages related to Nanjing, processes them across four services, generates multilingual summaries (English, Simplified Chinese, Traditional Chinese), and publishes a complete static wiki site.
All components are fully containerized, restart-safe, and designed for unattended scheduled operation.

# 1. Features Overview
   
## Automated Web Crawling

Collects English and Chinese (zh-Hans, zh-Hant, zh-HK, others) Wikipedia articles

Detects updated pages using caching, etag, and last-modified

Revisits previously crawled pages safely and incrementally

Automatically filters out irrelevant or off-topic pages

## Structured Content Extraction

Converts raw HTML into clean, standardized JSON

Extracts title, metadata, categories, language variants

Normalizes Chinese variants into a single canonical zh-article identity

Ensures stable identifiers for deduplication

## LLM Summarization (English / Simplified Chinese / Traditional Chinese)

Uses your Local LLM or any Remote LLM (OpenAI-compatible API).

Strict multilingual logic:

### English Summary

Prefer the English Wikipedia article

If missing → translate from Simplified Chinese

If Simplified missing → translate from Traditional Chinese

### Simplified Chinese Summary

Prefer the actual Chinese Wikipedia article

If missing → translate from the English summary

### Traditional Chinese Summary

If a Simplified summary exists → convert Hans → Hant

If no Chinese article exists → translate English directly into Hant

## Deduplication (critical for clients)

WikiAgent automatically removes duplicates caused by:

zh-Hans / zh-Hant / zh-HK / zh-SG variants

Chinese redirects

Same topic under multiple URLs

Deduplication occurs in:

Crawler: URL canonicalization + SQLite uniqueness

Extractor: canonical zh_url

Summarizer: skip summarizing duplicates

Publisher: merges by canonical topic ID

This guarantees one summary per topic.

## Publisher → Static Website Generator

Converts summaries to TiddlyWiki tiddlers

Generates full multilingual HTML wiki

Tags, search, linking, categories auto-generated

## Automated Pipeline (Manual or Cron-Scheduled)

The complete pipeline:
crawl → extract → summarize → publish

Supports:

Manual run (./run_pipeline.sh)

Scheduled cron automation

Fully restart-safe

# 3. Project Architecture
wikiagent/
```
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
    ├── NKHW_logo.png
    ├── index.html
    └── output/              # Published wiki (Tiddlers + HTML)
```
# 4. Getting Started
## System Requirements

Docker 24+

Docker Compose v2+

macOS / Linux / WSL

Python 3.10+ only if running tools manually

## Project Dependencies (installed inside containers)

BeautifulSoup

requests

sqlite3

pydantic

TiddlyWiki

LLM client libraries

cron

# 5. Installation
## Step 1 — Clone the Repository

git clone --branch main https://github.com/anjso/wikiagent.git wikiagent

cd wikiagent

## Step 2 — Configure LLM Settings (brain service)
Edit in docker-compose.yml:

LLM_BASE_URL: "http://your-llm-server/v1"

LLM_API_KEY: "your-api-key"

LLM_MODEL: "your-model-id"

This switches between local or remote LLMs.

## Step 3 — Run the Full Pipeline

./run_pipeline.sh

This performs:

Pull latest code

Rebuild all containers

Run each pipeline stage

Deduplicate summaries

Publish the wiki

# 6. Scheduled Automation (Cron)
#### Enable:

./install_cron.sh

#### Default behavior:

Runs automatically on 1st and 15th at 6 AM

#### Modify the schedule:

Edit the CRON= line inside install_cron.sh

#### Then reinstall:

chmod +x install_cron.sh

./install_cron.sh

#### Verify:

crontab -l

#### Cron logs appear in:

logs/automation.log

# 7. Output Directory Structure
```
Stage	        Directory    	  Description
Crawler	       data/raw/	       Raw HTML + metadata
Extractor	   data/clean/	      Canonical JSON extracted from HTML
Summarizer	   data/summarized/	Final English + Chinese JSON summaries
Publisher	   site/output/	   Full wiki site (HTML + tiddlers)
Public site	   site/index.html	Homepage of the generated wiki
```
Production site: 
https://anjso.org/wikiagent/
# 8. FAQ (Client-Friendly)
## 1. When should I see the first tiddler?
You will see results only after the publisher stage completes, not when summarization starts.
The first full run is the slowest; subsequent runs skip unchanged pages and are much faster.
Monitor progress via the log file on the deployment machine.
## 2. How do I access the wiki locally?
### Option 1 — Open directly
Open:
site/index.html

### Option 2 — Serve locally

cd site/

python3 -m http.server 8000

Then go to:
http://localhost:8000

### Remote Access (GitHub Pages)
If using github pages, go to: 
https://anjso.org/wikiagent/
## 3. What happens if a container stops? Is it safe to continue?
Yes — WikiAgent is fully restart-safe. 
Crawler uses atomic writes + SQLite
Extractor re-runs cleanly
Summarizer skips already summarized pages via content_hash
Publisher can rebuild infinitely
No cleanup needed unless you want a fresh run.
## 4. What if power is lost during summarization?
Just run:

./run_pipeline.sh

Incomplete summaries will be regenerated automatically.
# 8. Configuration

## LLM Settings (docker-compose.yml)

LLM_BASE_URL

LLM_API_KEY

LLM_MODEL

## Crawler Whitelist (config/whitelist.yml)

Controls:

Seed URLs

Maximum pages

Depth

Rate limits

Language rules

Include/exclude patterns

# 9. Development Notes (Maintainers)

Each service has its own Dockerfile

Logs and backups stored under /data

Publisher supports branding, CSS overrides, custom homepage

Duplicate cleanup occurs automatically at summarizer startup

# 10. Contributing

### Create feature branch
git checkout -b feature/my-change

#### Commit
git commit -m "Add: new feature"

### Push
git push origin feature/my-change

Then open a Pull Request.

# 11. Bug Reporting

Include:

Title

Short description

Steps to reproduce

Expected vs actual behavior

Logs or screenshots

Pipeline logs

# 12. License
MIT License.

# 13. Support
For custom features or troubleshooting, please open a GitHub Issue.
# 14. Maintainer Notes

#### Fully Restart-Safe

All services are idempotent; rerunning the pipeline is always safe.

#### Duplicate Prevention Logic

Crawler: URL normalization

Extractor: merges regional zh variants

Summarizer: dedupe by content_hash + canonical URL

Publisher: dedupe by canonical title

If duplicates appear, removing the problematic summary JSON and rerunning the pipeline usually resolves it.

#### Updating LLM or Pipeline Logic

Update environment variables such as:

LLM_BASE_URL

LLM_MODEL

Changes apply automatically on next run.

Production Deployment

Output lives in:

/site/output/

