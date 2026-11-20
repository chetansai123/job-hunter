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
