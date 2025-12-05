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
# use the directory where run_pipeline.sh lives as the project root, as long as the script is inside the repo
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)" # <-- UPDATE THIS PATH FOR ANJSO'S MAC SYSTEM (Or your system if you're running it locally on a Mac environment to test).
LOG_DIR="$PROJECT_DIR/logs"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

mkdir -p "$LOG_DIR"

# PATH fix for cron environments where docker isn't on PATH
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"

echo "--- Run started at $TIMESTAMP ---" >> "$LOG_DIR/automation.log"

cd "$PROJECT_DIR"

# helper to run a step, log exit code, and stop if it fails
run_step() {
  local LABEL="$1"; shift
  echo "[$LABEL] starting..." >> "$LOG_DIR/automation.log"
  # CHANGE B1: let run_step handle redirection itself (no extra >> in caller)
  "$@" >> "$LOG_DIR/automation.log" 2>&1
  local RC=$?
  echo "[$LABEL] exit code=$RC" >> "$LOG_DIR/automation.log"
  if [ $RC -ne 0 ]; then
    echo "[ERROR] $LABEL failed (exit $RC), aborting pipeline." >> "$LOG_DIR/automation.log"
    echo "--- Run aborted at $(date '+%Y-%m-%d %H:%M:%S') ---" >> "$LOG_DIR/automation.log"
    echo "" >> "$LOG_DIR/automation.log"
    exit $RC
  fi
}

# 1. Pull latest main
echo "[1] Updating repo…" >> "$LOG_DIR/automation.log"
git fetch --all >> "$LOG_DIR/automation.log" 2>&1
git reset --hard origin/main >> "$LOG_DIR/automation.log" 2>&1

# 2. Rebuild and restart docker, build images but do NOT leave long-running services behind
echo "[2] Rebuilding Docker…" >> "$LOG_DIR/automation.log"
docker compose down >> "$LOG_DIR/automation.log" 2>&1
docker compose pull >> "$LOG_DIR/automation.log" 2>&1
docker compose build >> "$LOG_DIR/automation.log" 2>&1

# IMPORTANT:
# - We do NOT use `docker compose down -v`, so the ./data:/data volume
#   stays intact (wiki.sqlite + raw/clean/summarized JSON + content_hash).

# start only the long-lived dependencies we need (brain + db)
echo "[3] Starting brain + db in background…" >> "$LOG_DIR/automation.log"
docker compose up -d db brain >> "$LOG_DIR/automation.log" 2>&1

sleep 5  # give services time to stabilize

# 3. Run each service, RUN_ONCE=1 makes each step run once and then exit cleanly
echo "[3] Running crawler…" >> "$LOG_DIR/automation.log"
run_step "crawler" docker compose run --rm -e RUN_ONCE=1 crawler python app.py >> "$LOG_DIR/automation.log" 2>&1

echo "[4] Running extractor…" >> "$LOG_DIR/automation.log"
run_step "extractor" docker compose run --rm -e RUN_ONCE=1 extractor python app.py >> "$LOG_DIR/automation.log" 2>&1

echo "[5] Running summarizer…" >> "$LOG_DIR/automation.log"
run_step "summarizer" docker compose run --rm -e RUN_ONCE=1 summarizer python app.py >> "$LOG_DIR/automation.log" 2>&1

echo "[6] Running publisher…" >> "$LOG_DIR/automation.log"
run_step "publisher" docker compose run --rm publisher python app.py >> "$LOG_DIR/automation.log" 2>&1

# cleanup of background services (brain + db) after run
echo "[8] Stopping background services…" >> "$LOG_DIR/automation.log"
docker compose stop brain db >> "$LOG_DIR/automation.log" 2>&1

# ===== CHANGE GIT1: auto-commit + push updated site/ if there are changes =====
echo "[9] Checking for changes under site/ before git commit/push…" >> "$LOG_DIR/automation.log"

CHANGES=$(git status --porcelain site || true)

if [ -n "$CHANGES" ]; then
  echo "[9] Changes detected in site/; preparing to commit and push…" >> "$LOG_DIR/automation.log"

  # Stage the generated site (force in case of ignored files)
  git add -f site >> "$LOG_DIR/automation.log" 2>&1

  COMMIT_MSG="Automated wiki rebuild $(date '+%Y-%m-%d %H:%M:%S')"

  # Try to commit; if there's nothing to commit (race/edge), don't fail the script
  if git commit -m "$COMMIT_MSG" >> "$LOG_DIR/automation.log" 2>&1; then
    echo "[9] git commit succeeded: $COMMIT_MSG" >> "$LOG_DIR/automation.log"
  else
    echo "[9] git commit: nothing to commit (possibly only metadata/timestamps changed)" >> "$LOG_DIR/automation.log"
  fi

  # Push to origin/main (this will still fail the pipeline on real network/auth errors)
  echo "[9] Pushing to origin/main…" >> "$LOG_DIR/automation.log"
  git push origin main >> "$LOG_DIR/automation.log" 2>&1
  echo "[9] git push completed." >> "$LOG_DIR/automation.log"
else
  echo "[9] No changes under site/; skipping git commit and push." >> "$LOG_DIR/automation.log"
fi
# ===== END CHANGE GIT1 =====

echo "--- Run completed at $(date '+%Y-%m-%d %H:%M:%S') ---" >> "$LOG_DIR/automation.log"
echo "" >> "$LOG_DIR/automation.log"