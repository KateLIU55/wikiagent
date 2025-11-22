#!/bin/bash
set -e

# === CONFIG ===
PROJECT_DIR="/Users/<username>/wikiagent"  # <-- UPDATE THIS PATH FOR ANJSO'S MAC SYSTEM (Or your system if you're running it locally on a Mac environment to test).
LOG_DIR="$PROJECT_DIR/logs"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

mkdir -p "$LOG_DIR"

echo "--- Run started at $TIMESTAMP ---" >> "$LOG_DIR/automation.log"

cd "$PROJECT_DIR"

# 1. Pull latest main
echo "[1] Updating repo…" >> "$LOG_DIR/automation.log"
git fetch --all >> "$LOG_DIR/automation.log" 2>&1
git reset --hard origin/main >> "$LOG_DIR/automation.log" 2>&1

# 2. Rebuild and restart docker
echo "[2] Rebuilding Docker…" >> "$LOG_DIR/automation.log"
docker compose down >> "$LOG_DIR/automation.log" 2>&1
docker compose pull >> "$LOG_DIR/automation.log" 2>&1
docker compose up --build -d >> "$LOG_DIR/automation.log" 2>&1

sleep 5  # give services time to stabilize

# 3. Run each service
echo "[3] Running crawler…" >> "$LOG_DIR/automation.log"
docker compose exec crawler python app.py >> "$LOG_DIR/automation.log" 2>&1

echo "[4] Running extractor…" >> "$LOG_DIR/automation.log"
docker compose exec extractor python app.py >> "$LOG_DIR/automation.log" 2>&1

echo "[5] Running summarizer…" >> "$LOG_DIR/automation.log"
docker compose exec summarizer python app.py >> "$LOG_DIR/automation.log" 2>&1

echo "[6] Running publisher…" >> "$LOG_DIR/automation.log"
docker compose exec publisher python app.py >> "$LOG_DIR/automation.log" 2>&1

echo "--- Run completed at $(date '+%Y-%m-%d %H:%M:%S') ---" >> "$LOG_DIR/automation.log"
echo "" >> "$LOG_DIR/automation.log"