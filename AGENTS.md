# Project Context: Job Hunter (MacBook Local Automation)

## Environment Already Set Up
- Machine: MacBook Air (macOS) with Xcode, Homebrew, and Rust (`cargo`) installed.
- `firecrawl-local` v0.1.0 is installed in `~/.cargo/bin` and serves the Firecrawl v2 API on `http://localhost:3002`.
- Project files in this folder: `job_hunter.py` (Python 3 stdlib, zero pip dependencies), `rules.json`, `jobs.db` (SQLite), `README.md`.
- Dashboard runs locally at `http://127.0.0.1:8080`.

## User Profile & Hard Job Filters (Full details in `rules.json`)
- Experience: 2.2 years (ex-Oracle, laid off, immediate joiner, previous CTC 21 LPA).
- Target roles (priority order): Full Stack > Backend (Node.js) > Frontend (React/Next.js) > SWE / Product Engineer.
- Stack: JavaScript, TypeScript (include explicitly), React.js, Node.js, Express.js, Next.js, MongoDB, SQL, PostgreSQL, MySQL, REST APIs.
- Experience bounds: Ideal 2–4 yrs, acceptable 1–3 yrs. Reject 4+ min YOE, Senior/Staff/Lead/Principal/Architect/Manager, and Intern/Fresher/0–1 yr roles.
- Locations: Hyderabad, Bengaluru, Pune, Remote India, or Remote Worldwide / APAC that explicitly allow India. Reject US/EU/UK/LATAM-only roles.
- Compensation: >= 15 LPA in India (if disclosed); include all qualifying international USD remote roles.
- Excluded companies: Oracle, Amazon, Razorpay, FactSet, Postman.
- Excluded platforms: Upwork, Turing, Micro1, proposal-bidding marketplaces.
- Freshness: Prioritize <= 14 days old; max 40 days old.

## Architecture & Workflow Decisions
1. **Deterministic discovery, not LLM loops:** Python drives the loop across job sources so it never stops after 5–10 companies.
2. **Zero duplicates:** Deduplicate every job in SQLite (`jobs.db`) before scoring or displaying.
3. **Direct links & outreach:** Resolve aggregator links to direct company/ATS apply URLs (Greenhouse, Lever, Ashby, Workable, etc.) and extract founder/recruiter LinkedIn, email, or hiring post URLs when available.
4. **Automatic company discovery:** Do not require the user to manually paste company career URLs—use public job APIs, startup boards, and search automatically.
5. **Truthful resume alignment:** When tailoring resumes to a JD, only rephrase/highlight real matching skills (never invent fake skills).
6. **Minimal Terminal commands:** Keep UX simple (prefer a single double-clickable `start.command` on macOS).
