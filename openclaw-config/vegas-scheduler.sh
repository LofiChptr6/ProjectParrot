#!/bin/bash
# Daily Vegas reminder scheduler
# This script checks every minute if it's 1:10 PM UTC and sends reminder

TELEGRAM_BOT_TOKEN="8512752813:AAHCihhbJD3nd8FDoU6lABj4mztsPQbZRNg"
TELEGRAM_CHAT_ID="6744082525"
REMINDER_MESSAGE="🚗 Time to start driving to Vegas! Let's go! 🎰"

LOG_FILE="/home/node/.openclaw/workspace/vegas-reminder.log"

# Function to check and send reminder
check_reminder() {
    local current_hour=$(date +%H)
    local current_minute=$(date +%M)

    # Check if it's 13:10 (1:10 PM UTC)
    if [ "$current_hour" = "13" ] && [ "$current_minute" = "10" ]; then
        echo "[$(date)] Reminder time reached. Sending Vegas reminder..." >> "$LOG_FILE"

        curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
            -d "chat_id=$TELEGRAM_CHAT_ID" \
            -d "text=$REMINDER_MESSAGE" > /dev/null || echo "Failed to send message" >> "$LOG_FILE"

        # After sending, mark the day so we don't send multiple times today
        echo "13:10:00" > /home/node/.openclaw/workspace/vegas-last-sent.txt
    fi
}

# Main loop - check every minute
echo "[$(date)] Vegas reminder scheduler started" >> "$LOG_FILE"

# Check if already sent today
LAST_SENT="/home/node/.openclaw/workspace/vegas-last-sent.txt"
if [ -f "$LAST_SENT" ]; then
    LAST_SENT_TIME=$(cat "$LAST_SENT" 2>/dev/null || echo "")
    CURRENT_TIME=$(date +%H:%M:%S)

    # If already sent before 13:10, skip checking
    if [ "$LAST_SENT_TIME" != "" ] && [ "$CURRENT_TIME" \< "$LAST_SENT_TIME" ]; then
        echo "[$(date)] Reminder already sent today at $LAST_SENT_TIME. Exiting." >> "$LOG_FILE"
        exit 0
    fi
fi

while true; do
    check_reminder
    sleep 60  # Check every minute
done