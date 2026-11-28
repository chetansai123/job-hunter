#!/bin/bash
# One-time setup for the cold-email composer on the GitHub Pages dashboard. Run it yourself on your Mac:
#
#   ./setup_cloud_composer.sh          full setup: compose key, Gmail, Claude token, encrypted rules + resume
#   ./setup_cloud_composer.sh --kit    only re-pack ~/ClaudeProjects/ColdEmail (CLAUDE.md + resume) after you change them
#   ./setup_cloud_composer.sh --claude only redo the Claude token (it is checked before it is saved)
#   ./setup_cloud_composer.sh --gmail  only redo the Gmail address + App Password (checked before it is saved)
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

if [ "$1" != "--kit" ] && [ "$1" != "--claude" ]; then
  printf '%s' "$KEY" | gh secret set COMPOSE_KEY --repo "$REPO"
  echo
  echo "Gmail: turn on 2-Step Verification, then create an App Password at https://myaccount.google.com/apppasswords"
  read -r -p "Gmail address to send from: " GA
  while true; do
    read -r -s -p "Gmail App Password (16 letters; typing is hidden; paste ONCE, then Enter): " GP; echo
    GP=$(printf '%s' "$GP" | tr -d '[:space:]')
    if ! printf '%s' "$GP" | grep -Eq '^[A-Za-z]{16}$'; then
      echo "That was ${#GP} characters. An App Password is exactly 16 letters (e.g. 'abcd efgh ijkl mnop'). Try again."
      continue
    fi
    echo "Checking it with Gmail (logs in only, sends nothing)..."
    CHECK=$(GA="$GA" GP="$GP" python3 - <<'EOF'
import os, smtplib, ssl
try:
    import certifi; ctx = ssl.create_default_context(cafile=certifi.where())
except Exception:
    ctx = ssl.create_default_context()
import base64
try:
    s = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=25); s.ehlo()
    code, _ = s.docmd("AUTH", "PLAIN " + base64.b64encode(f"\0{os.environ['GA']}\0{os.environ['GP']}".encode()).decode())
    print("ok" if code == 235 else "bad")
except Exception:
    print("unreachable")
EOF
)
    [ "$CHECK" = "bad" ] && { echo "Gmail rejected it: this App Password does not belong to $GA. In the browser, switch to $GA (top-right picture), create a new App Password there, and paste that."; continue; }
    [ "$CHECK" = "unreachable" ] && echo "(couldn't reach Gmail from this network to check it; saving anyway)"
    [ "$CHECK" = "ok" ] && echo "Gmail accepted it."
    break
  done
  printf '%s' "$GA" | gh secret set GMAIL_ADDRESS --repo "$REPO"
  printf '%s' "$GP" | gh secret set GMAIL_APP_PASSWORD --repo "$REPO"
  set_env GMAIL_ADDRESS "$GA"; set_env GMAIL_APP_PASSWORD "$GP"     # the dashboard on your Mac uses them too
fi

if [ "$1" != "--kit" ] && [ "$1" != "--gmail" ]; then
  echo
  echo "Claude: 'claude setup-token' opens your browser to sign in to your Claude plan and then prints a long token."
  echo "The token is printed over TWO lines. Select from 'sk-ant-oat' to the end of the second line and copy both lines."
  claude setup-token
  while true; do
    echo "Paste the token (hidden; both lines are fine), then press Enter on an EMPTY line:"
    CT=""
    while IFS= read -r -s line; do [ -z "$line" ] && break; CT="$CT$line"; done
    # keep only the token itself: stray characters, spaces and line breaks around or inside the paste are dropped
    CT=$(printf '%s' "$CT" | LC_ALL=C tr -cd 'A-Za-z0-9_-' | LC_ALL=C grep -Eo 'sk-ant-oat[0-9]{2}-[A-Za-z0-9_-]{40,}' | head -1)
    if [ -n "$CT" ]; then
      printf '%s' "$CT" | gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo "$REPO"
      echo "Token saved (${#CT} characters, starts sk-ant-oat). Claude will confirm it works on the first cloud run."
      break
    fi
    echo "That paste doesn't contain a token starting with sk-ant-oat. Copy it again, both lines."
  done
fi

[ "$1" = "--claude" ] || [ "$1" = "--gmail" ] && { echo "Done."; exit 0; }
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
