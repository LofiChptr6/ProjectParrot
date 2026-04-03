#!/bin/bash
LOG_FILE="/home/node/.openclaw/workspace/wakeup.log"
BOT_TOKEN="8512752813:AAHCihhbJD3nd8FDoU6lABj4mztsPQbZRNg"
CHAT_ID="6744082525"
MESSAGE="Good morning! ☀️ Time to wake up!"

echo "[$(date)] Scheduler started" >> "$LOG_FILE"

while true; do
    if [ "$(date +%H)" = "08" ] && [ "$(date +%M)" = "00" ]; then
        echo "[$(date)] Sending wake-up reminder" >> "$LOG_FILE"
        curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            -d "chat_id=${CHAT_ID}" \
            -d "text=${MESSAGE}" > /dev/null 2>&1
    fi
    sleep 3600
done