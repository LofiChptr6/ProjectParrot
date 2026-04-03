#!/bin/bash
# Wake-up reminder script
# This script is designed to be run autonomously

TELEGRAM_BOT_TOKEN="8512752813:AAHCihhbJD3nd8FDoU6lABj4mztsPQbZRNg"
TELEGRAM_CHAT_ID="6744082525"
MESSAGE="Good morning! ☀️ Time to wake up!"

# Send wake-up message
curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
    -d "chat_id=$TELEGRAM_CHAT_ID" \
    -d "text=$MESSAGE" > /dev/null

echo "[$(date)] Wake-up reminder executed at 8:00 AM UTC" >> /home/node/.openclaw/workspace/wakeup.log