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
