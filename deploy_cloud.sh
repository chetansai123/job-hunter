#!/bin/bash
# One-time cloud deploy to Fly.io — the hunter then runs 24/7 even with your Mac off.
#   ./deploy_cloud.sh                 first deploy (creates app + volume, sets secrets from .env, deploys)
#   ./deploy_cloud.sh --update        redeploy after code changes
set -e
cd "$(dirname "$0")"
APP=$(grep -E '^app\s*=' fly.toml | sed -E 's/.*"(.*)".*/\1/')
command -v fly >/dev/null || { echo "Install flyctl first: brew install flyctl"; exit 1; }
fly auth whoami >/dev/null 2>&1 || { echo "Not logged in. Run:  fly auth login   then re-run this script."; exit 1; }

if [ "$1" != "--update" ]; then
    fly apps list 2>/dev/null | grep -qw "$APP" || fly apps create "$APP"
    fly volumes list -a "$APP" 2>/dev/null | grep -q job_hunter_data || fly volumes create job_hunter_data --region sin --size 1 -y -a "$APP"
    PW=$(python3 -c "import secrets;print(secrets.token_urlsafe(12))")
    SECRETS="DASH_PASSWORD=$PW"
    for K in APIFY_TOKEN FIRECRAWL_API_KEY GEMINI_API_KEY; do
        V=$(grep -E "^$K=" .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'")
        [ -n "$V" ] && SECRETS="$SECRETS $K=$V"
    done
    fly secrets set -a "$APP" --stage $SECRETS >/dev/null
    echo "Dashboard password (save it!):  $PW"
fi
fly deploy -a "$APP" --ha=false
echo "Live at: https://$APP.fly.dev   (user name: anything, password: the one printed above)"
