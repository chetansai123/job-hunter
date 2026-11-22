#!/bin/bash
# ─────────────────────────────────────────────────────────────────
#  Job Hunter — 1-Click Launcher for macOS
#  Double-click this file in Finder to start everything.
# ─────────────────────────────────────────────────────────────────

# Always run from the script's own directory
cd "$(dirname "$0")"

# ── 0. Silently sync latest 24/7 jobs discovered by GitHub Actions ─
echo "☁️  Syncing latest jobs from cloud..."
git pull --quiet origin main 2>/dev/null || true

FIRECRAWL_PORT=3002
DASHBOARD_PORT=8080
LOG_DIR="$(pwd)/logs"
mkdir -p "$LOG_DIR"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║          🎯  Job Hunter v2 — Starting Up             ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── 1. Start firecrawl-local if not already running ──────────────
if lsof -i :$FIRECRAWL_PORT -sTCP:LISTEN -t &>/dev/null; then
    echo "✅  firecrawl-local already running on port $FIRECRAWL_PORT"
else
    echo "🚀  Starting firecrawl-local on port $FIRECRAWL_PORT..."
    FIRECRAWL_BIN="$HOME/.cargo/bin/firecrawl-local"
    if [ ! -x "$FIRECRAWL_BIN" ]; then
        echo "❌  firecrawl-local not found at $FIRECRAWL_BIN"
        echo "    Install it with:  cargo install firecrawl-local"
        read -n 1 -s -r -p "    Press any key to exit..."
        exit 1
    fi
    "$FIRECRAWL_BIN" serve \
        --port $FIRECRAWL_PORT \
        >> "$LOG_DIR/firecrawl.log" 2>&1 &
    FIRECRAWL_PID=$!
    echo "    PID $FIRECRAWL_PID — log: logs/firecrawl.log"

    # Wait up to 10 s for it to bind
    for i in $(seq 1 10); do
        sleep 1
        if lsof -i :$FIRECRAWL_PORT -sTCP:LISTEN -t &>/dev/null; then
            echo "✅  firecrawl-local is ready"
            break
        fi
        if [ $i -eq 10 ]; then
            echo "⚠️   firecrawl-local did not start in time — continuing anyway"
        fi
    done
fi

# ── 2. Stop any running copy (runner loop + dashboard) ─────────────
pkill -f "^/bin/bash ./run_forever.sh" 2>/dev/null
OLD_PID=$(lsof -i :$DASHBOARD_PORT -sTCP:LISTEN -t 2>/dev/null | head -1)
if [ -n "$OLD_PID" ]; then
    echo "🔄  Stopping old dashboard (PID $OLD_PID)..."
    kill "$OLD_PID" 2>/dev/null
    sleep 1
fi

# ── 3. Start the 24/7 runner (auto-restarts the hunter if it ever exits) ─
echo "🐍  Starting the 24/7 runner on port $DASHBOARD_PORT..."
nohup ./run_forever.sh >> "$LOG_DIR/job_hunter.log" 2>&1 &
echo "    runner PID $! — log: logs/job_hunter.log"

# Wait up to 8 s for the dashboard to bind
for i in $(seq 1 8); do
    sleep 1
    if lsof -i :$DASHBOARD_PORT -sTCP:LISTEN -t &>/dev/null; then
        echo "✅  Dashboard is ready"
        break
    fi
    if [ $i -eq 8 ]; then
        echo "⚠️   Dashboard did not start in time — check logs/job_hunter.log"
    fi
done

# ── 4. Open the browser ───────────────────────────────────────────
echo ""
echo "🌐  Opening http://127.0.0.1:$DASHBOARD_PORT ..."
open "http://127.0.0.1:$DASHBOARD_PORT"

echo ""
echo "────────────────────────────────────────────────────────"
echo "  Dashboard : http://127.0.0.1:$DASHBOARD_PORT"
echo "  Firecrawl : http://localhost:$FIRECRAWL_PORT"
echo "  Logs      : $(pwd)/logs/"
echo ""
echo "  Job Hunter v3 is running in the background."
echo "  Auto-discovery fires every 30 minutes automatically."
echo "  Close this window anytime — it won't stop the servers."
echo "────────────────────────────────────────────────────────"
echo ""
