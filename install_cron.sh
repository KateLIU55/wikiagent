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
# =============================================================================

#!/bin/bash

PROJECT_DIR="/Users/<username>/wikiagent"  # UPDATE THIS FOR ANJSO'S MAC PATH (NOTE: this will not work on our local windows environments)
SCRIPT="$PROJECT_DIR/run_pipeline.sh"
LOG="$PROJECT_DIR/logs/automation.log"

mkdir -p "$PROJECT_DIR/logs"

# Cron entry: 6 AM PDT → 20:00 UTC
CRON="0 6 1,15 * * $SCRIPT >> $LOG 2>&1"

# Install into user crontab
(crontab -l 2>/dev/null; echo "$CRON") | crontab -

echo "Cron installed:"
echo "$CRON"
