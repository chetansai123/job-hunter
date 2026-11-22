#!/bin/bash
# Makes Job Hunter start automatically after every login (macOS Login Item → start.command → 24/7 runner).
# Remove with:  ./install_runner.sh --uninstall
cd "$(dirname "$0")"
CMD="$(pwd)/start.command"
if [ "$1" = "--uninstall" ]; then
    osascript -e 'tell application "System Events" to delete (every login item whose path contains "job_hunter/start.command")' && echo "Login item removed."
    pkill -f "^/bin/bash ./run_forever.sh"; kill $(lsof -i :8080 -sTCP:LISTEN -t) 2>/dev/null
    exit 0
fi
osascript -e "tell application \"System Events\" to make login item at end with properties {path:\"$CMD\", hidden:true}" \
  && echo "Login item added: Job Hunter starts automatically after you log in."
