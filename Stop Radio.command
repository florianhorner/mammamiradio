#!/bin/bash
# Stop Malamie Radio
cd "$(dirname "$0")"

pkill -f "uvicorn mammamiradio" 2>/dev/null && echo "Uvicorn stopped." || echo "Uvicorn was not running."

# Clean up FIFO drain
if [ -f tmp/fifo-drain.pid ]; then
    kill "$(cat tmp/fifo-drain.pid)" 2>/dev/null || true
    rm -f tmp/fifo-drain.pid
    echo "FIFO drain stopped."
fi

echo ""
echo "Malamie Radio stopped. go-librespot left running (reusable on next start)."
echo ""
echo "Press any key to close."
read -n 1
