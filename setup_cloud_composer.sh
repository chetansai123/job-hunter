#!/bin/bash
# One-time setup for the cold-email composer on the GitHub Pages dashboard. Run it yourself on your Mac:
#
#   ./setup_cloud_composer.sh          full setup: compose key, Gmail, Claude token, encrypted rules + resume
#   ./setup_cloud_composer.sh --kit    only re-pack ~/ClaudeProjects/ColdEmail (CLAUDE.md + resume) after you change them
#
# Secrets go straight from your keyboard into GitHub repo secrets (gh secret set) and your git-ignored .env.
set -e
cd "$(dirname "$0")"
command -v gh >/dev/null || { echo "Install the GitHub CLI first: brew install gh && gh auth login"; exit 1; }
REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)
python3 -c "import cryptography" 2>/dev/null || python3 -m pip install --user --quiet cryptography

set_env() {   # set_env KEY VALUE → replace or add the line in .env
  python3 - "$1" "$2" <<'EOF'
import sys, pathlib
k, v = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env"); lines = p.read_text().splitlines() if p.exists() else []
lines = [l for l in lines if not l.startswith(k + "=")] + [f"{k}={v}"]
p.write_text("\n".join(lines) + "\n")
EOF
}

if ! grep -q '^COMPOSE_KEY=' .env 2>/dev/null; then
  set_env COMPOSE_KEY "$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
fi
KEY=$(grep '^COMPOSE_KEY=' .env | tail -1 | cut -d= -f2-)

if [ "$1" != "--kit" ]; then
  printf '%s' "$KEY" | gh secret set COMPOSE_KEY --repo "$REPO"
  echo
  echo "Gmail: turn on 2-Step Verification, then create an App Password at https://myaccount.google.com/apppasswords"
  read -r -p "Gmail address to send from: " GA
  read -r -s -p "Gmail App Password (16 letters; typing is hidden): " GP; echo
  GP=${GP// /}
  printf '%s' "$GA" | gh secret set GMAIL_ADDRESS --repo "$REPO"
  printf '%s' "$GP" | gh secret set GMAIL_APP_PASSWORD --repo "$REPO"
  set_env GMAIL_ADDRESS "$GA"; set_env GMAIL_APP_PASSWORD "$GP"     # the dashboard on your Mac uses them too
  echo
  echo "Claude: 'claude setup-token' opens your browser to sign in to your Claude plan and then prints a long token."
  claude setup-token
  read -r -s -p "Paste that token here (hidden): " CT; echo
  printf '%s' "$CT" | gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo "$REPO"
fi

COMPOSE_KEY="$KEY" python3 job_hunter.py --pack-compose-kit
git add compose/kit.enc
git commit -q -m "Update the encrypted cold-email kit (rules + resume)" -- compose/kit.enc || echo "(kit unchanged)"
git push -q origin HEAD:main || echo "Push failed: run 'git pull --rebase && git push' and try again."

echo
echo "Done. In the dashboard (https://$(echo "$REPO" | cut -d/ -f1).github.io/$(echo "$REPO" | cut -d/ -f2)/), open any"
echo "✉ Cold reach-out → 'Set it up', and paste:"
echo "  • a GitHub fine-grained token: only $REPO, Actions: Read and write, Contents: Read-only"
echo "    (create it at https://github.com/settings/personal-access-tokens/new)"
echo "  • this compose key: $KEY"
