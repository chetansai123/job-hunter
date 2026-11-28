# Job Hunter — Local Automation & Dashboard

Automated job discovery, deduplication, rule filtering, and tracking dashboard built for your MacBook using `firecrawl-local` and Python 3 (zero external `pip` packages required).

## Files
- `rules.json` — All your role priorities, stack keywords, 2.2 YOE limits, India/remote location rules, ₹15 LPA floor, excluded companies (`Oracle`, `Amazon`, `Razorpay`, `FactSet`, `Postman`), and excluded platforms (`Upwork`, `Turing`, `Micro1`).
- `job_hunter.py` — Runs scans via `firecrawl-local`, deduplicates jobs in `jobs.db` (SQLite), optionally scores & extracts founder/recruiter contacts via Gemini, and serves the local web dashboard.

## Quick Start on Your MacBook

1. **Start Firecrawl Local** (in Terminal tab 1):
   ```bash
   firecrawl-local serve
   ```
   *(Optional: run `export FIRECRAWL_CLOUD=off` beforehand if you only want local scraping/mapping.)*

2. **Start the Dashboard** (in Terminal tab 2, inside this folder):
   ```bash
   export GEMINI_API_KEY="your-gemini-api-key"   # optional, enables AI scoring & outreach extraction
   python3 job_hunter.py --serve
   ```
   Open **http://127.0.0.1:8080** in your browser.

3. **Run an automated scan from Terminal** (or use the **+ Scan URLs** button in the dashboard):
   ```bash
   python3 job_hunter.py --scan
   ```
   Add `--use-search` if you want it to also run the Firecrawl Cloud discovery queries in `rules.json`.

4. **Automate daily scans on macOS** (`crontab -e`):
   ```cron
   0 9,18 * * * cd /path/to/job_hunter && /usr/bin/python3 job_hunter.py --scan >> scan.log 2>&1
   ```

## Cold reach-out (Gmail-style composer)

Every job, post and funded-startup card has a **✉ Cold reach-out** button. It opens a compose window with the company, role, job link and (when we have one) the email address filled in. Pick who you're writing to (founder, recruiter/HR, or an employee for a referral), add their first name, and press **✨ Generate**.

- **Generate** runs the Claude Code CLI on your Mac (`claude -p`) inside `~/ClaudeProjects/ColdEmail`, so that project's `CLAUDE.md` rules and `Chetan_CV.pdf` apply exactly as they do there. It researches the company first, so it takes about 1–2 minutes and uses your Claude plan. "Before you send" flags and sources appear under the email.
- **Send** goes out from your Gmail with the resume attached. **Save as Gmail draft** puts it in Gmail Drafts instead, for you to send from Gmail.
- Every email is logged (`outreach` table). The cards show "✉ emailed" with the date, and emailing the same address twice asks first. Posts and startups move to "Contacted" when sent.

**One-time setup:** turn on 2-Step Verification for your Google account, create an App Password at myaccount.google.com/apppasswords, and add these to `.env`:

```
GMAIL_ADDRESS=you@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
```

Then restart the dashboard (`start.command`).

**Model:** Sonnet at medium effort by default. The two pickers next to Generate switch the model (Sonnet / Opus / Haiku) and effort (low → max); your choice is remembered.

### On the GitHub Pages dashboard

The public page can't hold secrets, so Generate and Send there start the **Cold email composer** GitHub Actions workflow (`.github/workflows/compose.yml`). It runs the same code on a GitHub runner with your Claude plan and your Gmail, and takes about 2–3 minutes. The request and the result are encrypted with your compose key, and your ColdEmail rules and resume sit in the repo only as `compose/kit.enc` (encrypted).

One-time setup:

1. On your Mac run `./setup_cloud_composer.sh`. It asks for your Gmail address and App Password and runs `claude setup-token` (sign in, paste the token). It saves them as repo secrets (`GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `CLAUDE_CODE_OAUTH_TOKEN`, `COMPOSE_KEY`), packs and pushes the encrypted kit, and prints your compose key.
2. Create a fine-grained GitHub token at github.com/settings/personal-access-tokens/new: only this repository, **Actions: Read and write**, **Contents: Read-only**.
3. In the dashboard, open any ✉ Cold reach-out → **Set it up**, paste the token and the compose key, and press Save. They stay in that browser only.

After you change your resume or `~/ClaudeProjects/ColdEmail/CLAUDE.md`, run `./setup_cloud_composer.sh --kit` to re-pack and push the kit. On the Pages copy, the "emailed" log lives in that browser.
