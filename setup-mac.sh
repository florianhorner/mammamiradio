#!/bin/bash
# One-time setup: creates a clickable Mamma Mi Radio.app launcher and bookmark files.
# After running this, drag the app to your Dock. Never touch the terminal again.
set -e
cd "$(dirname "$0")"

fail_setup() {
    echo "FATAL: $1" >&2
    exit 1
}

command -v osacompile >/dev/null 2>&1 || fail_setup "osacompile not found. Run this script on macOS with AppleScript tools installed."
[ -f .env ] || fail_setup ".env not found. Copy .env.example to .env and fill in the required settings first."
[ -x .venv/bin/python ] || fail_setup ".venv/bin/python not found. Create the virtualenv and install dependencies before running setup-mac.sh."

# Read port from .env if it exists
set -a
[ -f .env ] && source .env
set +a
PORT="${MAMMAMIRADIO_PORT:-8000}"

echo "Building Mamma Mi Radio.app..."

osacompile -o "Mamma Mi Radio.app" <<'APPLESCRIPT'
on run
    set appPath to POSIX path of (path to me)
    set projectPath to do shell script "dirname " & quoted form of appPath

    -- Read port from .env
    set portNum to do shell script "cd " & quoted form of projectPath & " && (. .env 2>/dev/null; echo ${MAMMAMIRADIO_PORT:-8000})"
    set baseURL to "http://localhost:" & portNum

    -- Check if already running
    set isRunning to false
    try
        do shell script "pgrep -f 'uvicorn mammamiradio'"
        set isRunning to true
    end try

    if not isRunning then
        -- Start the radio (start.sh handles go-librespot, FIFO, uvicorn)
        do shell script "cd " & quoted form of projectPath & " && ./start.sh > tmp/radio.log 2>&1 &"

        -- Wait for the server to be ready (up to 30 seconds)
        set ready to false
        repeat 30 times
            try
                do shell script "curl -sf -o /dev/null " & baseURL & "/listen"
                set ready to true
                exit repeat
            end try
            delay 1
        end repeat

        if not ready then
            display dialog "Mamma Mi Radio failed to start. Check tmp/radio.log for details." buttons {"OK"} default button "OK" with icon caution
            return
        end if
    end if

    -- Open dashboard
    open location baseURL & "/"
end run
APPLESCRIPT

echo "  Done."
echo ""

echo "Creating bookmark files..."

cat > "Dashboard.webloc" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>URL</key>
	<string>http://localhost:${PORT}/</string>
</dict>
</plist>
EOF

cat > "Listener.webloc" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>URL</key>
	<string>http://localhost:${PORT}/listen</string>
</dict>
</plist>
EOF

echo "  Done."
echo ""
echo "=== Setup complete ==="
echo ""
echo "  Mamma Mi Radio.app  → Drag to your Dock. One click starts the radio + opens dashboard."
echo "  Dashboard.webloc  → Drag to browser bookmarks bar (admin controls)."
echo "  Listener.webloc   → Drag to browser bookmarks bar (the player you share)."
echo ""
echo "  To share with friends on your network:"
echo "    1. Set MAMMAMIRADIO_BIND_HOST=0.0.0.0 and ADMIN_PASSWORD=... in .env"
echo "    2. Re-run ./setup-mac.sh"
echo "    3. Share http://$(ipconfig getifaddr en0 2>/dev/null || echo '<your-ip>'):${PORT}/listen"
echo ""
echo "  To stop the radio: double-click 'Stop Radio.command'"
