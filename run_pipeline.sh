#!/bin/bash
# =============================================================================
# WikiAgent Automation Pipeline
# -----------------------------------------------------------------------------
# This script rebuilds and runs the full WikiAgent data pipeline:
#   1. Pull latest changes from GitHub
#   2. Rebuilds all Docker services (crawler, extractor, summarizer, publisher)
#   3. Run each service and refresh all wiki data.
#
# HOW TO USE (Ubuntu/Linux/MacOS):
#
# 1. Make the script executable:
#       chmod +x run_pipeline.sh
#
# 2. Run manually at any time:
#       ./run_pipeline.sh
#
# 3. To install the scheduled cron job (runs on 1st & 15th @ 6 AM):
#       chmod +x install_cron.sh
#       ./install_cron.sh
#
# 4. Logs are written to:
#       <project>/logs/automation.log
#
# NOTE (TESTING STATUS):
#   This automation pipeline has been fully tested on a Linux environment
#   (verified on Ubuntu 22.0 on Dec 1st, 2025) and confirmed to perform the complete rebuild + execution
#   sequence successfully.
#
# NOTE FOR MacOS DEPLOYMENT (ANJSO):
#   The project must be located at one of the following paths on macOS:
#
#       /Users/<username>/wikiagent
#             OR
#       /Users/<username>/Documents/wikiagent
#
#   For ANJSO specifically, assuming the macOS username is "anjso", the expected
#   project directory path should be:
#
#       /Users/anjso/wikiagent
#
#   Update the PROJECT_DIR variable in this script to match the actual path
#   where the wikiagent repository is cloned on the Mac.
#
# IMPORTANT:
# - Ensure Docker Desktop is running (macOS or Linux).
# - Ensure the project directory path in this script is correct for the system.
# - Cron will execute this automatically on schedule once installed.
#
# =============================================================================f

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