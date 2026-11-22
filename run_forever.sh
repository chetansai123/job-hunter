#!/bin/bash
# 24/7 runner: keeps the dashboard + discovery loop alive and restarts it within 10 s if it ever exits.
# Started by start.command (which is also a macOS Login Item, so this begins automatically after every login).
cd "$(dirname "$0")"
mkdir -p logs
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.cargo/bin"
while true; do
    if ! lsof -i :3002 -sTCP:LISTEN -t >/dev/null 2>&1 && [ -x "$HOME/.cargo/bin/firecrawl-local" ]; then
        nohup "$HOME/.cargo/bin/firecrawl-local" serve --port 3002 >> logs/firecrawl.log 2>&1 &
        sleep 3
    fi
    # caffeinate -i: the Mac won't idle-sleep while the hunter runs (closing the lid on battery still sleeps it)
    /usr/bin/caffeinate -i "/Library/Frameworks/Python.framework/Versions/3.15/bin/python3" -u job_hunter.py --serve --port 8080
    echo "[runner] job_hunter exited ($?) — restarting in 10 s" 
    sleep 10
done
