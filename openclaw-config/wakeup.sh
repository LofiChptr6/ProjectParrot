#!/bin/bash
# Wake-up reminder script

TELEGRAM_BOT_TOKEN="8512752813:AAHCihhbJD3nd8FDoU6lABj4mztsPQbZRNg"
TELEGRAM_CHAT_ID="6744082525"
MESSAGE="Good morning! ☀️ Time to wake up!"

curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
    -d "chat_id=$TELEGRAM_CHAT_ID" \
    -d "text=$MESSAGE" > /dev/null || echo "Failed to send Telegram message"

echo "[$(date)] Wake-up reminder sent"