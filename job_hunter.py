#!/usr/bin/env python3
"""
Job Hunter v3 — Global 24/7 Discovery · Direct Apply Links Only · Cloud-Ready
Zero external pip dependencies (Python 3.9+ standard library).

JOB SOURCES (all free, no login required):
  RemoteOK · Remotive · Arbeitnow · Himalayas · Jobicy · Working Nomads
  We Work Remotely (RSS) · The Muse API · Otta (startup jobs) · YC Jobs
  Firecrawl /v2/search (India + global queries) · Career pages from rules.json

POST SOURCES (Founder / Recruiter / TA only — no personal login ever):
  HN Who's Hiring (Algolia API) · Reddit public feeds · LinkedIn/X via
  Firecrawl search (site: operator — no session cookies)

DIRECT-LINK GUARANTEE:
  Every job stored has either a direct company ATS URL (Greenhouse/Lever/Ashby/
  Workable/etc.) or a company career-page URL.  Aggregator-only links are
  discarded (no remoteok.com/N, remotive.com/N stored as the apply URL).

CONTINUOUS LOOP:
  Scans every 30 min inside `--serve` mode.  When hosted on Fly.io / Railway /
  Render the loop runs 24/7 even when your laptop is off.
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# ─────────────────────────── config & env ───────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DB_PATH   = Path(os.environ.get("DB_PATH",   str(BASE_DIR / "jobs.db")))
RULES_PATH = BASE_DIR / "rules.json"

FIRECRAWL_URL   = os.environ.get("FIRECRAWL_API_URL", "http://localhost:3002").rstrip("/")
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL    = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# Cloud: set HOST=0.0.0.0 and PORT=8080 via env
SERVE_HOST = os.environ.get("HOST", "127.0.0.1")
SERVE_PORT = int(os.environ.get("PORT", "8080"))

SCAN_INTERVAL   = int(os.environ.get("SCAN_INTERVAL_MINUTES", "30")) * 60   # default 30 min
BOOT_SCAN_BELOW = int(os.environ.get("BOOT_SCAN_BELOW", "20"))              # scan on startup if < N jobs

# Known aggregator domains — apply URLs landing here are REJECTED (no value to user)
AGGREGATOR_HOSTS: frozenset[str] = frozenset({
    "remoteok.com", "remotive.com", "arbeitnow.com", "himalayas.app",
    "jobicy.com", "workingnomads.com", "weworkremotely.com",
    "wellfound.com", "angel.co", "linkedin.com", "indeed.com",
    "glassdoor.com", "monster.com", "naukri.com", "simplyhired.com",
    "ziprecruiter.com", "dice.com", "themuse.com", "otta.com",
    "startup.jobs", "jobs.ycombinator.com",
})

# Well-known ATS / company career domains — always direct apply
ATS_HOSTS: frozenset[str] = frozenset({
    "boards.greenhouse.io", "job-boards.greenhouse.io",
    "jobs.lever.co", "apply.lever.co",
    "jobs.ashbyhq.com",
    "apply.workable.com",
    "jobs.smartrecruiters.com", "careers.smartrecruiters.com",
    "app.bamboohr.com",
    "recruitee.com",
    "breezy.hr",
    "jobs.gem.com",
    "app.trinethire.com",
    "hire.withgoogle.com",
    "jobs.jobvite.com",
    "icims.com",
    "taleo.net",
    "myworkdayjobs.com",
    "greenhouse.io",
})

POSTER_ROLE_KEYWORDS = [
    "founder", "co-founder", "cofounder", "ceo", "cto", "cpo", "coo",
    "vp engineering", "vp of engineering", "head of engineering",
    "head of talent", "hiring manager", "recruiter", "talent acquisition",
    "talent partner", "hr", "people ops", "technical recruiter",
    "engineering manager",
]

# ─────────────────────────── DB helpers ─────────────────────────────────────

def load_rules() -> dict:
    with open(RULES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            job_hash              TEXT UNIQUE NOT NULL,
            company               TEXT NOT NULL,
            title                 TEXT NOT NULL,
            direct_apply_url      TEXT NOT NULL,
            discovery_source      TEXT DEFAULT '',
            role_category         TEXT DEFAULT 'Full Stack',
            location              TEXT DEFAULT '',
            remote_eligible_india INTEGER DEFAULT 1,
            experience_req        TEXT DEFAULT '',
            salary                TEXT DEFAULT '',
            posted_date           TEXT DEFAULT '',
            match_score           INTEGER DEFAULT 0,
            match_reason          TEXT DEFAULT '',
            founder_name          TEXT DEFAULT '',
            founder_linkedin      TEXT DEFAULT '',
            founder_email         TEXT DEFAULT '',
            hiring_post_url       TEXT DEFAULT '',
            status                TEXT DEFAULT 'new',
            notes                 TEXT DEFAULT '',
            created_at            TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS posts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            post_hash    TEXT UNIQUE NOT NULL,
            source       TEXT NOT NULL,
            poster_name  TEXT DEFAULT '',
            poster_role  TEXT DEFAULT '',
            poster_url   TEXT DEFAULT '',
            post_url     TEXT NOT NULL,
            post_text    TEXT DEFAULT '',
            skills_found TEXT DEFAULT '',
            location     TEXT DEFAULT '',
            posted_at    TEXT DEFAULT '',
            status       TEXT DEFAULT 'new',
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS scan_runs (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at         TEXT NOT NULL,
            finished_at        TEXT,
            jobs_added         INTEGER DEFAULT 0,
            duplicates_skipped INTEGER DEFAULT 0,
            posts_added        INTEGER DEFAULT 0,
            notes              TEXT DEFAULT ''
        );
    """)
    conn.commit()
    conn.close()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_hash(*parts: str) -> str:
    key = "|".join(p.strip().lower() for p in parts)
    return hashlib.sha256(key.encode()).hexdigest()[:24]


# ─────────────────────────── URL resolution ─────────────────────────────────

def _netloc(url: str) -> str:
    return urllib.parse.urlparse(url.lower()).netloc.lstrip("www.")


def is_aggregator(url: str) -> bool:
    h = _netloc(url)
    return h in AGGREGATOR_HOSTS or any(h.endswith("." + a) for a in AGGREGATOR_HOSTS)


def is_ats(url: str) -> bool:
    h = _netloc(url)
    return h in ATS_HOSTS or any(h.endswith("." + a) for a in ATS_HOSTS)


def follow_redirect(url: str, timeout: int = 10) -> str:
    """Follow HTTP redirects and return the final URL."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.url
    except Exception:
        return url


def best_apply_url(
    platform_url: str,
    api_direct: str = "",
    page_links: list[str] | None = None,
) -> str | None:
    """
    Return the best direct apply URL, or None if we can only resolve to an aggregator.

    Priority:
      1. api_direct (explicit field like apply_url / applicationLink from API) if not aggregator
      2. platform_url itself if not aggregator
      3. Follow redirect on platform_url
      4. Follow redirect on api_direct
      5. Scan page_links for ATS / career-page URLs
    """
    page_links = page_links or []

    def _ok(u: str) -> bool:
        return bool(u) and not is_aggregator(u)

    # 1
    if _ok(api_direct):
        return api_direct
    # 2
    if _ok(platform_url):
        return platform_url
    # 3
    r3 = follow_redirect(platform_url)
    if _ok(r3):
        return r3
    # 4
    if api_direct:
        r4 = follow_redirect(api_direct)
        if _ok(r4):
            return r4
    # 5 — look for ATS or career links in page
    ats_kws = ["greenhouse", "lever", "ashby", "workable", "smartrecruit",
               "bamboohr", "recruitee", "breezy", "taleo", "icims", "workday",
               "jobvite", "/careers/", "/jobs/", "/apply", "/position"]
    for link in page_links:
        if not isinstance(link, str) or is_aggregator(link):
            continue
        ll = link.lower()
        if any(k in ll for k in ats_kws):
            return link
    # 6 — give up
    return None


# ─────────────────────────── HTTP helpers ───────────────────────────────────

_HEADERS = {"User-Agent": "JobHunterBot/3.0 (personal automation; not for commercial use)"}


def http_get(url: str, timeout: int = 20) -> dict | list:
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  [GET ✗] {url[:80]} → {e}")
        return {}


def http_get_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [GET-text ✗] {url[:80]} → {e}")
        return ""


def http_post(url: str, payload: dict, timeout: int = 45) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={**_HEADERS, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  [POST ✗] {url[:80]} → {e}")
        return {}


# ─────────────────────────── Firecrawl helpers ──────────────────────────────

def fc_scrape(url: str) -> dict:
    r = http_post(f"{FIRECRAWL_URL}/v2/scrape",
                  {"url": url, "formats": ["markdown", "links"]}, timeout=60)
    return r.get("data", r)


def fc_search(query: str, limit: int = 10) -> list[dict]:
    r = http_post(f"{FIRECRAWL_URL}/v2/search",
                  {"query": query, "limit": limit,
                   "scrapeOptions": {"formats": ["markdown", "links"]}}, timeout=60)
    return r.get("data", []) or []


def fc_map(url: str, limit: int = 50) -> list[str]:
    r = http_post(f"{FIRECRAWL_URL}/v2/map", {"url": url, "limit": limit})
    links = r.get("links") or r.get("data", {}).get("links") or []
    return [u for u in links if isinstance(u, str)]


# ─────────────────────────── filter engine ──────────────────────────────────

def days_ago(dt_str: str) -> int | None:
    if not dt_str:
        return None
    clean = dt_str[:19].rstrip("Z").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = datetime.strptime(clean[:len(fmt)], fmt[:len(clean)])
            return (datetime.utcnow() - d).days
        except ValueError:
            continue
    return None


def passes_hard_filters(title: str, company: str, url: str, body: str, rules: dict) -> tuple[bool, str]:
    t, c, u, b = title.lower(), company.lower(), url.lower(), body.lower()

    for excl in rules.get("excluded_companies", []):
        if excl.lower() in c:
            return False, f"Excluded company: {excl}"
    for plat in rules.get("excluded_platforms", []):
        if plat.lower() in u:
            return False, f"Excluded platform: {plat}"
    for kw in rules.get("experience", {}).get("reject_title_keywords", []):
        if re.search(r'\b' + re.escape(kw.lower()), t):
            return False, f"Reject title kw: {kw}"
    for phrase in rules.get("locations", {}).get("reject_region_phrases", []):
        if phrase.lower() in b:
            return False, f"Reject region: {phrase}"

    stack_kws = [k.lower() for k in rules.get("keywords", [])]
    if stack_kws and not any(k in (t + " " + b) for k in stack_kws):
        return False, "No stack match"

    return True, "ok"


def _guess_cat(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ["frontend", "front-end", "react", "vue", "angular", "ui "]):
        return "Frontend"
    if any(k in t for k in ["backend", "back-end", "node", "api ", "server", "django", "rails"]):
        return "Backend"
    return "Full Stack"


def _score(title: str, body: str, rules: dict) -> int:
    s = 5
    t, b = title.lower(), body.lower()
    for i, role in enumerate(rules.get("role_priority", [])):
        if role.lower() in t:
            s += max(3 - i, 1)
            break
    hits = sum(1 for k in rules.get("keywords", []) if k.lower() in b)
    s += min(hits // 2, 2)
    return min(s, 10)


def _extract_skills(text: str, rules: dict) -> str:
    found = [k for k in rules.get("keywords", []) if k.lower() in text.lower()]
    return ", ".join(found[:8])


def _is_valid_poster(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in POSTER_ROLE_KEYWORDS)


# ─────────────────────────── DB insert ──────────────────────────────────────

def insert_job(conn: sqlite3.Connection, job: dict) -> bool:
    h = make_hash(job["company"], job["title"], job["direct_apply_url"])
    if conn.execute("SELECT id FROM jobs WHERE job_hash=?", (h,)).fetchone():
        return False
    conn.execute("""
        INSERT INTO jobs (
            job_hash,company,title,direct_apply_url,discovery_source,
            role_category,location,remote_eligible_india,experience_req,
            salary,posted_date,match_score,match_reason,
            founder_name,founder_linkedin,founder_email,hiring_post_url,
            status,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (h, job["company"], job["title"], job["direct_apply_url"],
         job.get("discovery_source", ""),
         job.get("role_category", "Full Stack"),
         job.get("location", ""),
         1 if job.get("remote_eligible_india", True) else 0,
         job.get("experience_req", ""),
         job.get("salary", ""),
         job.get("posted_date", ""),
         int(job.get("match_score", 7)),
         job.get("match_reason", ""),
         job.get("founder_name", ""),
         job.get("founder_linkedin", ""),
         job.get("founder_email", ""),
         job.get("hiring_post_url", ""),
         "new", now_utc()))
    conn.commit()
    return True


def insert_post(conn: sqlite3.Connection, post: dict) -> bool:
    h = make_hash(post.get("post_url", ""), post.get("post_text", "")[:80])
    if conn.execute("SELECT id FROM posts WHERE post_hash=?", (h,)).fetchone():
        return False
    conn.execute("""
        INSERT INTO posts (
            post_hash,source,poster_name,poster_role,poster_url,
            post_url,post_text,skills_found,location,posted_at,
            status,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (h, post.get("source",""), post.get("poster_name",""),
         post.get("poster_role",""), post.get("poster_url",""),
         post.get("post_url",""), post.get("post_text","")[:2000],
         post.get("skills_found",""), post.get("location",""),
         post.get("posted_at",""), "new", now_utc()))
    conn.commit()
    return True


# ═══════════════════════════════════════════════════════════════════════════
#  JOB COLLECTORS  (all enforce direct-link guarantee before returning)
# ═══════════════════════════════════════════════════════════════════════════

def _job(company, title, apply_url, source, rules, **extra) -> dict:
    """Build a normalised job dict."""
    return {
        "company": company or "Unknown",
        "title": title[:140],
        "direct_apply_url": apply_url,
        "discovery_source": source,
        "role_category": _guess_cat(title),
        "location": extra.get("location", "Remote"),
        "remote_eligible_india": extra.get("remote_india", True),
        "experience_req": extra.get("exp", ""),
        "salary": extra.get("salary", ""),
        "posted_date": extra.get("date", ""),
        "match_score": _score(title, extra.get("body", ""), rules),
        "match_reason": extra.get("reason", source),
        "founder_name": extra.get("founder_name", ""),
        "founder_linkedin": extra.get("founder_linkedin", ""),
        "founder_email": extra.get("founder_email", ""),
        "hiring_post_url": extra.get("hiring_post_url", ""),
    }


# ── 1. RemoteOK ─────────────────────────────────────────────────────────────
def collect_remoteok(rules: dict) -> list[dict]:
    print("[remoteok] fetching...")
    raw = http_get("https://remoteok.com/api", timeout=30)
    if not isinstance(raw, list):
        return []
    stack_kws = [k.lower() for k in rules.get("keywords", [])]
    max_days   = rules.get("freshness", {}).get("max_age_days", 40)
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title   = item.get("position", "")
        company = item.get("company", "")
        # RemoteOK gives both their page URL and an apply_url (direct ATS)
        p_url   = item.get("url", "")
        a_url   = item.get("apply_url", "")
        date_s  = item.get("date", "")
        tags    = " ".join(item.get("tags", [])).lower()

        if not title:
            continue
        age = days_ago(date_s)
        if age is not None and age > max_days:
            continue
        combined = (title + " " + tags).lower()
        if not any(k in combined for k in stack_kws):
            continue
        ok, reason = passes_hard_filters(title, company, p_url, combined, rules)
        if not ok:
            continue

        direct = best_apply_url(p_url, a_url)
        if not direct:
            continue   # can't get a non-aggregator URL — skip

        out.append(_job(company, title, direct, "remoteok",
                        rules, body=combined,
                        salary=item.get("salary", ""),
                        date=date_s[:10] if date_s else "",
                        reason=f"RemoteOK · {', '.join(item.get('tags',[])[:5])}"))
    print(f"  → {len(out)} jobs (direct links only)")
    return out


# ── 2. Remotive ─────────────────────────────────────────────────────────────
def collect_remotive(rules: dict) -> list[dict]:
    """Remotive doesn't expose direct apply URL in its API, so we Firecrawl-scrape
       each job page to extract the actual apply button URL."""
    print("[remotive] fetching API...")
    raw = http_get("https://remotive.com/api/remote-jobs?category=software-dev&limit=50", timeout=30)
    if not isinstance(raw, dict):
        return []
    jobs     = raw.get("jobs", [])
    stack_kws= [k.lower() for k in rules.get("keywords", [])]
    max_days = rules.get("freshness", {}).get("max_age_days", 40)
    out = []
    scraped_count = 0

    for item in jobs:
        if scraped_count >= 30:   # cap Firecrawl calls per cycle
            break
        title   = item.get("title", "")
        company = item.get("company_name", "")
        p_url   = item.get("url", "")          # remotive.com page
        date_s  = item.get("publication_date", "")
        tags    = " ".join(item.get("tags", [])).lower()
        cand_loc= item.get("candidate_required_location", "").lower()

        if not title or not p_url:
            continue
        age = days_ago(date_s)
        if age is not None and age > max_days:
            continue
        combined = (title + " " + tags + " " + item.get("description", "")[:300]).lower()
        if not any(k in combined for k in stack_kws):
            continue
        # Quick region pre-check
        if any(x in cand_loc for x in ["us only","usa only","uk only","eu only",
                                        "europe only","canada only"]):
            continue
        ok, _ = passes_hard_filters(title, company, p_url, combined, rules)
        if not ok:
            continue

        # Remotive hides the apply URL — scrape the page for ATS/company links
        scraped = fc_scrape(p_url)
        scraped_count += 1
        page_links = scraped.get("links", [])

        direct = best_apply_url(p_url, "", page_links)
        if not direct:
            continue

        out.append(_job(company, title, direct, "remotive",
                        rules, body=combined,
                        location=item.get("candidate_required_location", "Remote"),
                        salary=item.get("salary",""),
                        date=date_s[:10] if date_s else "",
                        reason=f"Remotive · {', '.join(item.get('tags',[])[:4])}"))
    print(f"  → {len(out)} jobs (scraped {scraped_count} pages for direct links)")
    return out


# ── 3. Arbeitnow ────────────────────────────────────────────────────────────
def collect_arbeitnow(rules: dict) -> list[dict]:
    print("[arbeitnow] fetching...")
    raw = http_get("https://www.arbeitnow.com/api/job-board-api", timeout=30)
    if not isinstance(raw, dict):
        return []
    stack_kws = [k.lower() for k in rules.get("keywords", [])]
    max_days  = rules.get("freshness", {}).get("max_age_days", 40)
    out = []
    for item in raw.get("data", []):
        title   = item.get("title", "")
        company = item.get("company_name", "")
        p_url   = item.get("url", "")
        epoch   = item.get("created_at", 0)
        tags    = " ".join(item.get("tags", [])).lower()
        desc    = item.get("description", "")[:400].lower()

        if not title or not p_url:
            continue
        if epoch and (time.time() - epoch) / 86400 > max_days:
            continue
        combined = (title + " " + tags + " " + desc).lower()
        if not any(k in combined for k in stack_kws):
            continue
        ok, _ = passes_hard_filters(title, company, p_url, combined, rules)
        if not ok:
            continue

        # Arbeitnow URLs often redirect directly to company ATS
        direct = best_apply_url(p_url)
        if not direct:
            continue

        date_s = datetime.utcfromtimestamp(epoch).strftime("%Y-%m-%d") if epoch else ""
        out.append(_job(company, title, direct, "arbeitnow",
                        rules, body=combined,
                        location=item.get("location","Remote"),
                        remote_india=item.get("remote", False),
                        date=date_s,
                        reason=f"Arbeitnow · {', '.join(item.get('tags',[])[:4])}"))
    print(f"  → {len(out)} jobs")
    return out


# ── 4. Himalayas ────────────────────────────────────────────────────────────
def collect_himalayas(rules: dict) -> list[dict]:
    print("[himalayas] fetching...")
    stack_kws = [k.lower() for k in rules.get("keywords", [])]
    max_days  = rules.get("freshness", {}).get("max_age_days", 40)
    queries   = ["javascript", "typescript", "reactjs", "nodejs", "fullstack"]
    seen_urls: set[str] = set()
    out = []
    for q in queries:
        raw = http_get(f"https://himalayas.app/jobs/api?q={q}&limit=50", timeout=25)
        if not isinstance(raw, dict):
            continue
        for item in raw.get("jobs", []):
            title   = item.get("title", "")
            company = item.get("companyName", "")
            a_url   = item.get("applicationLink", "")   # usually direct ATS ✓
            p_url   = item.get("url", a_url)
            date_s  = item.get("createdAt", "")
            skills  = " ".join(item.get("skills", [])).lower()

            if not title or a_url in seen_urls:
                continue
            seen_urls.add(a_url or p_url)
            age = days_ago(date_s)
            if age is not None and age > max_days:
                continue
            combined = (title + " " + skills).lower()
            if not any(k in combined for k in stack_kws):
                continue
            ok, _ = passes_hard_filters(title, company, a_url or p_url, combined, rules)
            if not ok:
                continue

            direct = best_apply_url(p_url, a_url)
            if not direct:
                continue

            out.append(_job(company, title, direct, "himalayas",
                            rules, body=combined,
                            location=item.get("location","Remote"),
                            salary=item.get("salary",""),
                            date=date_s[:10] if date_s else "",
                            reason=f"Himalayas · {', '.join(item.get('skills',[])[:4])}"))
        time.sleep(0.4)
    print(f"  → {len(out)} jobs")
    return out


# ── 5. Jobicy ───────────────────────────────────────────────────────────────
def collect_jobicy(rules: dict) -> list[dict]:
    """Jobicy free API — scrapes each job page for the direct apply URL."""
    print("[jobicy] fetching...")
    stack_kws = [k.lower() for k in rules.get("keywords", [])]
    max_days  = rules.get("freshness", {}).get("max_age_days", 40)
    out = []
    scraped_count = 0
    # Jobicy allows filtering by tag
    for tag in ["javascript", "typescript", "react", "node"]:
        raw = http_get(f"https://jobicy.com/api/v2/remote-jobs?count=30&tag={tag}", timeout=25)
        if not isinstance(raw, dict):
            continue
        for item in raw.get("jobs", []):
            if scraped_count >= 25:
                break
            title   = item.get("jobTitle","")
            company = item.get("companyName","")
            p_url   = item.get("jobPostURL","")
            date_s  = item.get("pubDate","")
            skills  = item.get("jobIndustry","")

            if not title or not p_url:
                continue
            age = days_ago(date_s)
            if age is not None and age > max_days:
                continue
            combined = (title + " " + skills).lower()
            if not any(k in combined for k in stack_kws):
                continue
            ok, _ = passes_hard_filters(title, company, p_url, combined, rules)
            if not ok:
                continue

            # Jobicy only gives their own page — scrape for apply link
            scraped = fc_scrape(p_url)
            scraped_count += 1
            direct = best_apply_url(p_url, "", scraped.get("links", []))
            if not direct:
                continue

            out.append(_job(company, title, direct, "jobicy",
                            rules, body=combined,
                            date=date_s[:10] if date_s else "",
                            reason=f"Jobicy · {tag}"))
        time.sleep(0.3)
    print(f"  → {len(out)} jobs (scraped {scraped_count} pages)")
    return out


# ── 6. Working Nomads ───────────────────────────────────────────────────────
def collect_working_nomads(rules: dict) -> list[dict]:
    print("[workingnomads] fetching...")
    raw = http_get("https://www.workingnomads.com/api/exposed_jobs/?category=development", timeout=25)
    if not isinstance(raw, list):
        return []
    stack_kws = [k.lower() for k in rules.get("keywords", [])]
    max_days  = rules.get("freshness", {}).get("max_age_days", 40)
    out = []
    for item in raw:
        title   = item.get("title","")
        company = item.get("company","")
        p_url   = item.get("url","")
        date_s  = item.get("pub_date","")

        if not title or not p_url:
            continue
        age = days_ago(date_s)
        if age is not None and age > max_days:
            continue
        combined = title.lower()
        if not any(k in combined for k in stack_kws):
            continue
        ok, _ = passes_hard_filters(title, company, p_url, combined, rules)
        if not ok:
            continue

        # Working Nomads often link directly to company site
        direct = best_apply_url(p_url)
        if not direct:
            continue

        out.append(_job(company, title, direct, "workingnomads",
                        rules, body=combined,
                        date=date_s[:10] if date_s else "",
                        reason=f"WorkingNomads · {title[:40]}"))
    print(f"  → {len(out)} jobs")
    return out


# ── 7. We Work Remotely (RSS) ────────────────────────────────────────────────
def collect_weworkremotely(rules: dict) -> list[dict]:
    print("[weworkremotely] fetching RSS...")
    stack_kws = [k.lower() for k in rules.get("keywords", [])]
    max_days  = rules.get("freshness", {}).get("max_age_days", 40)
    feed_urls = [
        "https://weworkremotely.com/remote-jobs.rss",
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
    ]
    out = []
    seen: set[str] = set()
    for feed_url in feed_urls:
        xml_text = http_get_text(feed_url, timeout=20)
        if not xml_text:
            continue
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            continue
        ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
        for item in root.findall(".//item"):
            title   = (item.findtext("title") or "").strip()
            link    = (item.findtext("link") or "").strip()
            company = ""
            # WWR puts "Company: Title" format in title
            if " — " in title:
                parts   = title.split(" — ", 1)
                company = parts[0].strip()
                title   = parts[1].strip()
            pub_date = (item.findtext("pubDate") or "")

            if not title or link in seen:
                continue
            seen.add(link)
            combined = title.lower()
            if not any(k in combined for k in stack_kws):
                continue
            ok, _ = passes_hard_filters(title, company, link, combined, rules)
            if not ok:
                continue

            # WWR links often redirect to company ATS
            direct = best_apply_url(link)
            if not direct:
                continue

            # Parse date roughly
            date_s = ""
            try:
                from email.utils import parsedate_to_datetime
                date_s = parsedate_to_datetime(pub_date).strftime("%Y-%m-%d")
            except Exception:
                pass

            out.append(_job(company or "WWR", title, direct, "weworkremotely",
                            rules, body=combined, date=date_s,
                            reason=f"WeWorkRemotely RSS · {title[:40]}"))
    print(f"  → {len(out)} jobs")
    return out


# ── 8. The Muse ─────────────────────────────────────────────────────────────
def collect_the_muse(rules: dict) -> list[dict]:
    """The Muse public API — no key needed for basic endpoint."""
    print("[themuse] fetching...")
    stack_kws = [k.lower() for k in rules.get("keywords", [])]
    out = []
    for page in range(0, 3):   # 3 pages × 20 = 60 jobs
        raw = http_get(
            f"https://www.themuse.com/api/public/jobs?category=Engineering&level=Entry+Level"
            f"&level=Mid+Level&page={page}&descending=true", timeout=20)
        if not isinstance(raw, dict):
            break
        for item in raw.get("results", []):
            title   = item.get("name","")
            company = item.get("company",{}).get("name","")
            refs    = item.get("refs",{})
            p_url   = refs.get("landing_page","") or item.get("refs",{}).get("canonical_url","")
            if not title or not p_url:
                continue
            combined = title.lower()
            if not any(k in combined for k in stack_kws):
                continue
            ok, _ = passes_hard_filters(title, company, p_url, combined, rules)
            if not ok:
                continue

            direct = best_apply_url(p_url)
            if not direct:
                continue

            out.append(_job(company, title, direct, "themuse",
                            rules, body=combined,
                            reason=f"TheMuse · {title[:40]}"))
    print(f"  → {len(out)} jobs")
    return out


# ── 9. Firecrawl search — global + India queries ─────────────────────────────
# These use Firecrawl /v2/search which acts as a Bing/Google wrapper.
# If FIRECRAWL_URL is pointing at firecrawl-local (port 3002), cloud search
# may not be available — the calls fail silently and are skipped.

GLOBAL_JOB_QUERIES = [
    # India-specific
    '"Full Stack" "TypeScript" "Node.js" remote India hiring 2024 OR 2025',
    '"Backend Engineer" "Node.js" OR "TypeScript" Hyderabad OR Bengaluru OR Pune hiring',
    '"Frontend Engineer" "React" "Next.js" remote India 2-4 years experience',
    '"Software Engineer" "TypeScript" "MongoDB" OR "PostgreSQL" India startup hiring',
    '"Full Stack Developer" "Node" "React" Hyderabad OR Bangalore salary LPA',
    # Global remote that includes India
    '"Full Stack Engineer" "TypeScript" "React" "Node" remote worldwide hiring',
    '"Backend Developer" "Node.js" "PostgreSQL" remote APAC OR worldwide 2-4 years',
    '"React Developer" "TypeScript" "REST API" remote worldwide "India eligible"',
    # Startup / YC / Wellfound style
    'site:wellfound.com "Full Stack" OR "Backend" "TypeScript" "Node.js" "2-4 years"',
    'site:ycombinator.com/companies "Full Stack Engineer" "TypeScript" remote',
    # ATS direct job boards
    'site:boards.greenhouse.io "Full Stack" "TypeScript" "Node.js" "2" OR "3" years',
    'site:jobs.lever.co "Full Stack" OR "Backend" "TypeScript" "React" "Node" remote',
    'site:jobs.ashbyhq.com "Full Stack" "Node.js" OR "TypeScript" remote worldwide',
]

def collect_firecrawl_global(rules: dict) -> list[dict]:
    print(f"[firecrawl-search] running {len(GLOBAL_JOB_QUERIES)} queries...")
    stack_kws = [k.lower() for k in rules.get("keywords", [])]
    out = []
    seen_urls: set[str] = set()
    for query in GLOBAL_JOB_QUERIES:
        for item in fc_search(query, limit=8):
            url      = item.get("url","")
            markdown = item.get("markdown","") or ""
            title    = item.get("title","Software Engineer")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            company = _netloc(url).split(".")[0].title()
            combined= (title + " " + markdown[:800]).lower()
            if not any(k in combined for k in stack_kws):
                continue
            ok, _ = passes_hard_filters(title, company, url, combined, rules)
            if not ok:
                continue
            direct = best_apply_url(url, "", item.get("links",[]))
            if not direct:
                continue
            out.append(_job(company, title, direct, "firecrawl-search",
                            rules, body=combined,
                            reason="Firecrawl search — verify details on page"))
    print(f"  → {len(out)} candidates")
    return out


# ── 10. Career pages from rules.json ─────────────────────────────────────────
def collect_career_pages(rules: dict) -> list[dict]:
    source_urls = rules.get("sources", {}).get("career_urls", [])
    stack_kws   = [k.lower() for k in rules.get("keywords", [])]
    print(f"[career-pages] mapping {len(source_urls)} URLs...")
    out = []
    for base_url in source_urls:
        mapped = fc_map(base_url, limit=50)
        candidates = [l for l in mapped[:20]
                      if any(k in l.lower() for k in
                             ("job","career","role","position","engineer","developer"))]
        if not candidates:
            candidates = [base_url]
        for link in candidates:
            scraped  = fc_scrape(link)
            markdown = scraped.get("markdown","")
            if not markdown:
                continue
            title    = scraped.get("metadata",{}).get("title","Software Engineer")
            company  = _netloc(link).split(".")[0].title()
            combined = (title + " " + markdown[:1500]).lower()
            if not any(k in combined for k in stack_kws):
                continue
            ok, _ = passes_hard_filters(title, company, link, combined, rules)
            if not ok:
                continue
            page_links = scraped.get("links",[])
            direct = best_apply_url(link, "", page_links)
            if not direct:
                continue
            if GEMINI_API_KEY:
                ai = _gemini_score(direct, markdown, rules)
                if not ai or not ai.get("is_match"):
                    continue
                out.append(_job(ai.get("company") or company,
                                ai.get("title") or title,
                                direct, f"career-page:{base_url[:35]}",
                                rules, body=combined,
                                location=ai.get("location",""),
                                exp=ai.get("experience_req",""),
                                salary=ai.get("salary",""),
                                date=ai.get("posted_date",""),
                                match_score=ai.get("match_score",7),
                                reason=ai.get("match_reason",""),
                                founder_name=ai.get("founder_name",""),
                                founder_linkedin=ai.get("founder_linkedin",""),
                                founder_email=ai.get("founder_email",""),
                                hiring_post_url=ai.get("hiring_post_url","")))
            else:
                out.append(_job(company, title[:120], direct,
                                f"career-page:{base_url[:35]}",
                                rules, body=combined,
                                reason="Career-page match — set GEMINI_API_KEY for AI scoring"))
    print(f"  → {len(out)} jobs")
    return out


def _gemini_score(url: str, markdown: str, rules: dict) -> dict | None:
    if not GEMINI_API_KEY:
        return None
    prompt = (
        "You are a strict job-matching verifier. Evaluate this job posting.\n"
        f"Candidate Rules JSON:\n{json.dumps(rules, indent=2)}\n\n"
        f"Job URL: {url}\nJob Page Markdown (truncated):\n{markdown[:12000]}\n\n"
        'Return ONLY valid JSON: {"is_match":true,"reject_reason":"","company":"","title":"",'
        '"role_category":"Full Stack","location":"","remote_eligible_india":true,'
        '"experience_req":"","salary":"","posted_date":"YYYY-MM-DD",'
        '"match_score":8,"match_reason":"1 sentence",'
        '"founder_name":"","founder_linkedin":"","founder_email":"","hiring_post_url":""}\n'
        "Reject if: min YOE >= 4, India ineligible, India salary < 15 LPA, "
        "post > 40 days old, or stack unrelated to JS/TS/React/Node/Next/SQL/MongoDB."
    )
    endpoint = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{GEMINI_MODEL}:generateContent?key={urllib.parse.quote(GEMINI_API_KEY)}")
    res = http_post(endpoint,
                    {"contents":[{"parts":[{"text":prompt}]}],
                     "generationConfig":{"responseMimeType":"application/json","temperature":0.1}},
                    timeout=45)
    try:
        return json.loads(res["candidates"][0]["content"]["parts"][0]["text"])
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  POST COLLECTORS  (Founders · Recruiters · TA only)
# ═══════════════════════════════════════════════════════════════════════════

# ── HN Who's Hiring ──────────────────────────────────────────────────────────
def collect_hn_hiring(rules: dict) -> list[dict]:
    print("[hn-hiring] querying Algolia...")
    stack_kws = [k.lower() for k in rules.get("keywords", [])]
    max_days  = rules.get("freshness", {}).get("max_age_days", 40)
    top = http_get(
        "https://hn.algolia.com/api/v1/search_by_date"
        "?tags=ask_hn&query=Ask+HN+Who+is+Hiring&hitsPerPage=3", timeout=20)
    out = []
    for thread in (top.get("hits", []) if isinstance(top, dict) else [])[:1]:
        tid = thread.get("objectID","")
        if not tid:
            continue
        data = http_get(
            f"https://hn.algolia.com/api/v1/search_by_date"
            f"?tags=comment,story_{tid}&hitsPerPage=200", timeout=25)
        for c in (data.get("hits",[]) if isinstance(data,dict) else []):
            text_raw = c.get("comment_text","") or ""
            text     = re.sub(r"<[^>]+>"," ", text_raw).strip()
            if not text:
                continue
            combined = text.lower()
            if not any(k in combined for k in stack_kws):
                continue
            created = c.get("created_at","")
            age = days_ago(created)
            if age is not None and age > max_days:
                continue
            out.append({
                "source":      "HN Who's Hiring",
                "poster_name": c.get("author",""),
                "poster_role": "Company / Founder (HN thread)",
                "poster_url":  f"https://news.ycombinator.com/user?id={c.get('author','')}",
                "post_url":    f"https://news.ycombinator.com/item?id={c.get('objectID','')}",
                "post_text":   text[:1500],
                "skills_found":_extract_skills(text, rules),
                "location":    "",
                "posted_at":   created[:10] if created else "",
            })
    print(f"  → {len(out)} HN posts")
    return out


# ── Reddit public feeds ───────────────────────────────────────────────────────
def collect_reddit_posts(rules: dict) -> list[dict]:
    subreddits = ["forhire", "remotejs", "developersIndia", "IndiaJobSearch"]
    stack_kws  = [k.lower() for k in rules.get("keywords", [])]
    max_days   = rules.get("freshness", {}).get("max_age_days", 40)
    out = []
    for sub in subreddits:
        print(f"[reddit] r/{sub}...")
        raw = http_get(f"https://www.reddit.com/r/{sub}/new.json?limit=50", timeout=20)
        if not isinstance(raw, dict):
            continue
        for p in raw.get("data",{}).get("children",[]):
            d       = p.get("data",{})
            title   = d.get("title","")
            body    = d.get("selftext","")
            purl    = "https://reddit.com" + d.get("permalink","")
            author  = d.get("author","")
            created = d.get("created_utc",0)
            if created:
                age = (time.time() - created) / 86400
                if age > max_days:
                    continue
            combined = (title + " " + body).lower()
            if not any(k in combined for k in stack_kws):
                continue
            if sub == "forhire" and "[hiring]" not in title.lower():
                continue
            if "looking for work" in combined or "[for hire]" in title.lower():
                continue
            if sub not in ("forhire",) and not _is_valid_poster(combined):
                continue
            date_s = datetime.utcfromtimestamp(created).strftime("%Y-%m-%d") if created else ""
            out.append({
                "source":      f"Reddit r/{sub}",
                "poster_name": author,
                "poster_role": "Recruiter / Hiring Manager (Reddit post)",
                "poster_url":  f"https://reddit.com/user/{author}",
                "post_url":    purl,
                "post_text":   (title + "\n\n" + body)[:1500],
                "skills_found":_extract_skills(combined, rules),
                "location":    "",
                "posted_at":   date_s,
            })
        time.sleep(1)
    print(f"  → {len(out)} Reddit posts total")
    return out


# ── LinkedIn / X hiring posts via Firecrawl search ───────────────────────────
# Uses public search engine indexing — no login, no cookies, no session required.

LINKEDIN_POST_QUERIES = [
    # India-focused
    'site:linkedin.com/posts "hiring" "full stack" "node.js" OR "react" "typescript" Bangalore OR Hyderabad OR Pune OR "remote india"',
    'site:linkedin.com/posts "we are hiring" "software engineer" "typescript" OR "javascript" "2-4 years" india',
    'site:linkedin.com/posts "looking for" "full stack" OR "backend engineer" "node" OR "react" india founder OR recruiter',
    'site:linkedin.com/posts "open role" OR "open position" "engineer" "typescript" "react" OR "node" bangalore OR hyderabad',
    'site:linkedin.com/posts founder "hiring" "full stack" OR "backend" "typescript" india "immediate joiner" OR "2-4 years"',
    'site:linkedin.com/posts recruiter "actively hiring" "javascript" OR "typescript" "react" OR "node" "remote" india',
    # Global remote
    'site:linkedin.com/posts "hiring" "full stack engineer" "typescript" "node.js" "react" remote worldwide',
    'site:linkedin.com/posts "join our team" "backend engineer" "node.js" OR "typescript" remote worldwide india eligible',
    'site:linkedin.com/posts "we\'re hiring" "software engineer" "typescript" remote APAC OR worldwide',
    # X/Twitter
    'site:x.com "hiring" "full stack" OR "backend engineer" "node" OR "react" "typescript" india OR remote',
    'site:twitter.com "we are hiring" "full stack" OR "backend" "typescript" india OR remote founder OR cto',
    # Startup / founder posts
    '"founder" OR "cto" "hiring" "full stack engineer" "typescript" OR "javascript" "react" "node" india 2024 OR 2025',
    '"talent acquisition" OR "recruiter" "looking for" "full stack" OR "backend" "node.js" "typescript" india remote',
]

def collect_linkedin_posts(rules: dict) -> list[dict]:
    print(f"[linkedin-posts] running {len(LINKEDIN_POST_QUERIES)} queries via Firecrawl search...")
    stack_kws = [k.lower() for k in rules.get("keywords", [])]
    out = []
    seen_urls: set[str] = set()
    for query in LINKEDIN_POST_QUERIES:
        for item in fc_search(query, limit=6):
            url      = item.get("url","")
            markdown = item.get("markdown","") or item.get("description","")
            title    = item.get("title","")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            combined = (title + " " + markdown).lower()
            if not any(k in combined for k in stack_kws):
                continue
            if not _is_valid_poster(combined):
                continue
            source = ("LinkedIn" if "linkedin.com" in url
                      else "X/Twitter" if "x.com" in url or "twitter.com" in url
                      else "Web")
            out.append({
                "source":      source,
                "poster_name": title[:80],
                "poster_role": "Founder / Recruiter / TA (public post)",
                "poster_url":  url,
                "post_url":    url,
                "post_text":   markdown[:1500],
                "skills_found":_extract_skills(combined, rules),
                "location":    "",
                "posted_at":   "",
            })
    print(f"  → {len(out)} LinkedIn/X posts")
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN SCAN ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════

def run_full_scan(custom_urls: list[str] | None = None) -> dict:
    rules = load_rules()
    conn  = get_db()
    cur   = conn.execute("INSERT INTO scan_runs (started_at) VALUES (?)", (now_utc(),))
    run_id= cur.lastrowid
    conn.commit()

    jobs_added = dupes = posts_added = 0

    if custom_urls:
        # Manual URL scan
        all_jobs: list[dict] = []
        for base_url in custom_urls:
            mapped = fc_map(base_url, limit=50)
            candidates = mapped[:20] or [base_url]
            for link in candidates:
                scraped  = fc_scrape(link)
                markdown = scraped.get("markdown","")
                if not markdown:
                    continue
                title    = scraped.get("metadata",{}).get("title","Software Engineer")
                company  = _netloc(link).split(".")[0].title()
                combined = (title + " " + markdown[:1500]).lower()
                ok, _    = passes_hard_filters(title, company, link, combined, rules)
                if not ok:
                    continue
                direct = best_apply_url(link, "", scraped.get("links",[]))
                if not direct:
                    continue
                all_jobs.append(_job(company, title[:120], direct,
                                     f"manual:{base_url[:35]}", rules,
                                     body=combined,
                                     reason="Manual scan — verify details on page."))
    else:
        # Full automatic discovery from all sources
        collectors = [
            collect_remoteok,
            collect_himalayas,
            collect_working_nomads,
            collect_weworkremotely,
            collect_the_muse,
            collect_arbeitnow,
            collect_firecrawl_global,
            collect_career_pages,
            collect_remotive,    # slow (scrapes pages) — run last
            collect_jobicy,      # slow (scrapes pages) — run last
        ]
        all_jobs = []
        for fn in collectors:
            try:
                all_jobs.extend(fn(rules))
            except Exception as exc:
                print(f"  [!] {fn.__name__} failed: {exc}")

    for job in all_jobs:
        try:
            if insert_job(conn, job):
                jobs_added += 1
            else:
                dupes += 1
        except Exception:
            pass

    # Posts
    post_collectors = [collect_hn_hiring, collect_reddit_posts, collect_linkedin_posts]
    for fn in post_collectors:
        try:
            for post in fn(rules):
                try:
                    if insert_post(conn, post):
                        posts_added += 1
                except Exception:
                    pass
        except Exception as exc:
            print(f"  [!] {fn.__name__} failed: {exc}")

    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    conn.execute(
        "UPDATE scan_runs SET finished_at=?,jobs_added=?,duplicates_skipped=?,posts_added=? WHERE id=?",
        (now_utc(), jobs_added, dupes, posts_added, run_id))
    conn.commit()
    conn.close()
    summary = {"jobs_added": jobs_added, "duplicates_skipped": dupes,
               "posts_added": posts_added, "total_jobs_in_db": total}
    print(f"[scan complete] {summary}")
    return summary


# ─────────────────────────── background discovery loop ──────────────────────

def _background_loop() -> None:
    """Daemon thread: boot scan if DB sparse, then scans every SCAN_INTERVAL seconds."""
    time.sleep(4)  # let the HTTP server bind first
    conn = get_db()
    job_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    conn.close()
    if job_count < BOOT_SCAN_BELOW:
        print(f"[loop] only {job_count} jobs in DB → running immediate boot scan...")
        run_full_scan()
    else:
        print(f"[loop] {job_count} jobs found → skipping boot scan")
    while True:
        print(f"[loop] sleeping {SCAN_INTERVAL//60} min until next scan...")
        time.sleep(SCAN_INTERVAL)
        print("[loop] starting scheduled scan...")
        run_full_scan()


# ═══════════════════════════════════════════════════════════════════════════
#  DASHBOARD HTML  (Jobs tab + Posts tab)
# ═══════════════════════════════════════════════════════════════════════════

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>Job Hunter 🎯</title>
<style>
:root{--bg:#07090f;--card:#0f1520;--card2:#111827;--border:#1e2d42;--text:#e2eaf5;
      --muted:#7a8fa8;--accent:#3b82f6;--green:#22c55e;--amber:#f59e0b;--red:#ef4444;
      --purple:#a78bfa;}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:var(--bg);color:var(--text);min-height:100vh;}
.wrap{max-width:1380px;margin:0 auto;padding:24px 18px 80px;}
header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;
       gap:14px;margin-bottom:16px;}
h1{font-size:22px;letter-spacing:-.03em;}
.sub{color:var(--muted);font-size:12px;margin-top:3px;}
/* tabs */
.tabs{display:flex;border-bottom:2px solid var(--border);margin-bottom:18px;gap:0;}
.tab{padding:9px 20px;font-size:13px;font-weight:600;cursor:pointer;background:0;
     border:0;color:var(--muted);border-bottom:3px solid transparent;margin-bottom:-2px;transition:.15s;}
.tab.active{color:var(--accent);border-bottom-color:var(--accent);}
/* stats */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:16px;}
.stat{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 14px;}
.stat .num{font-size:22px;font-weight:700;}
.stat .lbl{font-size:11px;color:var(--muted);margin-top:2px;}
/* controls */
.controls{display:flex;flex-wrap:wrap;gap:9px;margin-bottom:14px;align-items:center;}
input,select,textarea{background:var(--card);color:var(--text);border:1px solid var(--border);
                      border-radius:7px;padding:8px 11px;font-size:13px;outline:none;}
input[type=text]{flex:1;min-width:200px;}
select:focus,input:focus{border-color:var(--accent);}
button{background:var(--accent);color:#fff;border:0;border-radius:7px;padding:8px 13px;
       font-size:13px;font-weight:600;cursor:pointer;transition:.15s;}
button:hover{opacity:.85;}
button.sec{background:var(--card2);color:var(--text);border:1px solid var(--border);}
button.ok{background:#15803d;} button.warn{background:#92400e;} button.bad{background:#991b1b;}
button.purple{background:#6d28d9;}
/* grid & job card */
.grid{display:grid;gap:11px;}
.job{background:var(--card);border:1px solid var(--border);border-radius:11px;
     padding:15px 17px;display:grid;gap:9px;transition:.15s;}
.job:hover{border-color:#2d4a6e;}
.jt{display:flex;flex-wrap:wrap;justify-content:space-between;gap:9px;align-items:flex-start;}
.job-title{font-size:16px;font-weight:700;line-height:1.3;}
.company{color:var(--accent);font-weight:600;font-size:13px;margin-top:2px;}
.founder-row{font-size:12px;color:#93c5fd;margin-top:3px;display:flex;flex-wrap:wrap;gap:6px;}
.badges{display:flex;flex-wrap:wrap;gap:5px;align-items:center;}
.badge{font-size:11px;padding:2px 8px;border-radius:999px;background:#1a2740;
       color:var(--muted);border:1px solid var(--border);}
.badge.score{color:#86efac;border-color:#166534;background:#052e16;}
.badge.sn{color:#93c5fd;border-color:#1d4ed8;background:#0c1f4a;}
.badge.sa{color:#86efac;border-color:#15803d;background:#052e16;}
.badge.ss{color:#fcd34d;border-color:#92400e;background:#2d1a04;}
.badge.sr{color:#fca5a5;border-color:#991b1b;background:#2d0808;}
.meta{font-size:12px;color:var(--muted);display:flex;flex-wrap:wrap;gap:12px;}
.reason{font-size:13px;line-height:1.5;color:#c8d8ea;}
.actions{display:flex;flex-wrap:wrap;gap:7px;align-items:center;}
a.apply-btn{background:var(--accent);color:#fff;text-decoration:none;padding:7px 13px;
            border-radius:7px;font-size:13px;font-weight:600;}
a.apply-btn:hover{opacity:.85;}
/* post card */
.post{background:var(--card2);border:1px solid var(--border);border-radius:11px;
      padding:14px 17px;display:grid;gap:7px;transition:.15s;}
.post:hover{border-color:#2d4a6e;}
.post-src{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--amber);font-weight:700;}
.post-name{font-size:15px;font-weight:700;}
.post-role{font-size:11px;color:var(--muted);}
.post-skills{font-size:11px;color:#86efac;}
.post-body{font-size:12px;line-height:1.6;color:#a8bdd4;
           max-height:160px;overflow:hidden;white-space:pre-wrap;position:relative;}
.post-body.exp{max-height:none;}
.pactions{display:flex;gap:7px;flex-wrap:wrap;align-items:center;}
a.view-btn{background:var(--card);color:var(--text);text-decoration:none;padding:6px 11px;
           border-radius:7px;font-size:12px;font-weight:600;border:1px solid var(--border);}
/* scan box */
.scan-box{background:var(--card);border:1px solid var(--border);border-radius:11px;
          padding:14px;margin-bottom:16px;display:none;}
.scan-box.open{display:block;}
.scan-box textarea{width:100%;min-height:68px;margin:9px 0;resize:vertical;}
/* header btns */
.hbtns{display:flex;gap:7px;flex-wrap:wrap;}
#scanAllBtn{background:#6d28d9;}
/* loader */
.loader{text-align:center;padding:40px;color:var(--muted);font-size:14px;}
/* source tag colours */
.src-remoteok{background:#0d2a1f;color:#34d399;border-color:#065f46;}
.src-remotive{background:#0d1f2a;color:#60a5fa;border-color:#1d4ed8;}
.src-arbeitnow{background:#1f1420;color:#c084fc;border-color:#7e22ce;}
.src-himalayas{background:#201a08;color:#fbbf24;border-color:#92400e;}
.src-jobicy{background:#08141f;color:#38bdf8;border-color:#075985;}
.src-wwr{background:#0f1f10;color:#4ade80;border-color:#166534;}
.src-muse{background:#1f0d13;color:#f9a8d4;border-color:#9d174d;}
.src-firecrawl{background:#0d1520;color:#a5b4fc;border-color:#4338ca;}
.src-career{background:#111820;color:#94a3b8;border-color:#334155;}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div>
    <h1>🎯 Job Hunter</h1>
    <div class="sub" id="lastScan">Auto-discovery every 30 min · Direct apply links only</div>
  </div>
  <div class="hbtns">
    <button class="sec" onclick="toggleScan()">+ Manual Scan</button>
    <button id="scanAllBtn" onclick="triggerAutoScan()">🔍 Scan Now</button>
    <button class="sec" onclick="refresh()">↻ Refresh</button>
  </div>
</header>

<div id="scanBox" class="scan-box">
  <strong>Scan specific career pages / job URLs (via firecrawl-local)</strong>
  <textarea id="customUrls" placeholder="One URL per line. Leave empty → full auto-scan of all sources."></textarea>
  <div style="display:flex;gap:9px;align-items:center;">
    <button onclick="triggerScan()">Start Scan</button>
    <span id="scanStatus" style="font-size:12px;color:var(--muted);"></span>
  </div>
</div>

<div class="tabs">
  <button class="tab active" id="tabJobs" onclick="switchTab('jobs')">💼 Jobs</button>
  <button class="tab" id="tabPosts" onclick="switchTab('posts')">📢 Posts (Founders & Recruiters)</button>
</div>

<!-- ─── JOBS PANEL ────────────────────────────────────────────── -->
<div id="panelJobs">
  <div class="stats" id="jobStats"></div>
  <div class="controls">
    <input type="text" id="q" placeholder="Search role, company, stack, location…" oninput="renderJobs()"/>
    <select id="stFilter" onchange="renderJobs()">
      <option value="all">All Statuses</option>
      <option value="new" selected>New</option>
      <option value="saved">Saved</option>
      <option value="applied">Applied</option>
      <option value="rejected">Rejected</option>
    </select>
    <select id="catFilter" onchange="renderJobs()">
      <option value="all">All Roles</option>
      <option value="Full Stack">Full Stack</option>
      <option value="Backend">Backend</option>
      <option value="Frontend">Frontend</option>
    </select>
    <select id="srcFilter" onchange="renderJobs()">
      <option value="all">All Sources</option>
      <option value="remoteok">RemoteOK</option>
      <option value="remotive">Remotive</option>
      <option value="arbeitnow">Arbeitnow</option>
      <option value="himalayas">Himalayas</option>
      <option value="jobicy">Jobicy</option>
      <option value="workingnomads">WorkingNomads</option>
      <option value="weworkremotely">WeWorkRemotely</option>
      <option value="themuse">TheMuse</option>
      <option value="firecrawl">Firecrawl</option>
      <option value="career">Career Pages</option>
      <option value="manual">Manual Scan</option>
    </select>
    <select id="scoreFilter" onchange="renderJobs()">
      <option value="0">Any Score</option>
      <option value="7">Score ≥ 7</option>
      <option value="8">Score ≥ 8</option>
    </select>
  </div>
  <div class="grid" id="jobs"><div class="loader">Loading jobs…</div></div>
</div>

<!-- ─── POSTS PANEL ───────────────────────────────────────────── -->
<div id="panelPosts" style="display:none">
  <div class="stats" id="postStats"></div>
  <div class="controls">
    <input type="text" id="pq" placeholder="Search poster, skill, keyword…" oninput="renderPosts()"/>
    <select id="pSrcFilter" onchange="renderPosts()">
      <option value="all">All Sources</option>
      <option value="HN">HN Who's Hiring</option>
      <option value="Reddit">Reddit</option>
      <option value="LinkedIn">LinkedIn</option>
      <option value="X/">X/Twitter</option>
    </select>
    <select id="pStFilter" onchange="renderPosts()">
      <option value="all">All Statuses</option>
      <option value="new" selected>New</option>
      <option value="saved">Saved</option>
      <option value="contacted">Contacted</option>
    </select>
  </div>
  <div class="grid" id="posts"><div class="loader">Loading posts…</div></div>
</div>
</div>

<script>
let allJobs=[], allPosts=[], tab='jobs';

const SRC_CLASS = {
  remoteok:'src-remoteok', remotive:'src-remotive', arbeitnow:'src-arbeitnow',
  himalayas:'src-himalayas', jobicy:'src-jobicy', weworkremotely:'src-wwr',
  themuse:'src-muse', firecrawl:'src-firecrawl',
};

function srcClass(src){
  for(const [k,v] of Object.entries(SRC_CLASS)) if(src.includes(k)) return v;
  if(src.includes('career')) return 'src-career';
  return '';
}

function switchTab(t){
  tab=t;
  ['jobs','posts'].forEach(x=>{
    document.getElementById('panel'+x[0].toUpperCase()+x.slice(1)).style.display = x===t?'':'none';
    document.getElementById('tab'+x[0].toUpperCase()+x.slice(1)).className='tab'+(x===t?' active':'');
  });
}

function toggleScan(){ document.getElementById('scanBox').classList.toggle('open'); }

async function refresh(){
  await Promise.all([loadJobs(), loadPosts()]);
  document.getElementById('lastScan').textContent =
    'Last refreshed: '+new Date().toLocaleTimeString()+' · Auto every 30 min';
}

async function loadJobs(){
  const r=await fetch('/api/jobs'); allJobs=await r.json();
  renderJobStats(); renderJobs();
}
async function loadPosts(){
  const r=await fetch('/api/posts'); allPosts=await r.json();
  renderPostStats(); renderPosts();
}

function renderJobStats(){
  const c={total:allJobs.length,new:0,saved:0,applied:0,rejected:0};
  allJobs.forEach(j=>{ if(c[j.status]!==undefined) c[j.status]++; });
  document.getElementById('jobStats').innerHTML=`
    <div class="stat"><div class="num">${c.total}</div><div class="lbl">Total Jobs</div></div>
    <div class="stat"><div class="num">${c.new}</div><div class="lbl">New</div></div>
    <div class="stat"><div class="num">${c.saved}</div><div class="lbl">Saved</div></div>
    <div class="stat"><div class="num">${c.applied}</div><div class="lbl">Applied</div></div>
    <div class="stat"><div class="num">${c.rejected}</div><div class="lbl">Rejected</div></div>`;
}
function renderPostStats(){
  const c={total:allPosts.length,new:0,saved:0,contacted:0};
  allPosts.forEach(p=>{ if(c[p.status]!==undefined) c[p.status]++; });
  document.getElementById('postStats').innerHTML=`
    <div class="stat"><div class="num">${c.total}</div><div class="lbl">Total Posts</div></div>
    <div class="stat"><div class="num">${c.new}</div><div class="lbl">New</div></div>
    <div class="stat"><div class="num">${c.saved}</div><div class="lbl">Saved</div></div>
    <div class="stat"><div class="num">${c.contacted}</div><div class="lbl">Contacted</div></div>`;
}

function esc(s){ return String(s||'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])); }
function statusBadge(s){ return `<span class="badge s${s[0]}">${esc(s.toUpperCase())}</span>`; }

function renderJobs(){
  const q   = document.getElementById('q').value.toLowerCase();
  const st  = document.getElementById('stFilter').value;
  const cat = document.getElementById('catFilter').value;
  const src = document.getElementById('srcFilter').value;
  const minScore = parseInt(document.getElementById('scoreFilter').value)||0;

  const f = allJobs.filter(j=>{
    if(st!=='all' && j.status!==st) return false;
    if(cat!=='all' && j.role_category!==cat) return false;
    if(src!=='all' && !j.discovery_source.toLowerCase().includes(src)) return false;
    if(minScore && j.match_score < minScore) return false;
    if(q){
      const hay=`${j.company} ${j.title} ${j.location} ${j.match_reason} ${j.discovery_source} ${j.salary}`.toLowerCase();
      if(!hay.includes(q)) return false;
    }
    return true;
  });

  const el=document.getElementById('jobs');
  if(!f.length){ el.innerHTML='<div class="job"><div class="sub">No jobs match current filter.</div></div>'; return; }

  el.innerHTML=f.map(j=>`
<div class="job">
  <div class="jt">
    <div>
      <div class="job-title">${esc(j.title)}</div>
      <div class="company">${esc(j.company)}</div>
      ${(j.founder_name||j.founder_linkedin||j.founder_email)?`
      <div class="founder-row">
        👤 ${esc(j.founder_name||'Hiring Contact')}
        ${j.founder_linkedin?`· <a href="${esc(j.founder_linkedin)}" target="_blank" style="color:#93c5fd">LinkedIn ↗</a>`:''}
        ${j.founder_email?`· <a href="mailto:${esc(j.founder_email)}" style="color:#93c5fd">${esc(j.founder_email)}</a>`:''}
        ${j.hiring_post_url?`· <a href="${esc(j.hiring_post_url)}" target="_blank" style="color:#93c5fd">Post ↗</a>`:''}
      </div>`:''}
    </div>
    <div class="badges">
      <span class="badge score">⭐ ${j.match_score}/10</span>
      <span class="badge">${esc(j.role_category)}</span>
      ${statusBadge(j.status)}
      <span class="badge ${srcClass(j.discovery_source)}" style="font-size:10px">${esc(j.discovery_source.split(':')[0])}</span>
    </div>
  </div>
  <div class="meta">
    <span>📍 ${esc(j.location||'Remote')}</span>
    <span>🧑‍💻 ${esc(j.experience_req||'2–4 yrs')}</span>
    ${j.salary?`<span>💰 ${esc(j.salary)}</span>`:''}
    ${j.posted_date?`<span>📅 ${esc(j.posted_date)}</span>`:''}
  </div>
  <div class="reason">${esc(j.match_reason)}</div>
  <div class="actions">
    <a class="apply-btn" href="${esc(j.direct_apply_url)}" target="_blank" rel="noopener">Direct Apply ↗</a>
    <button class="ok"   onclick="setJobStatus(${j.id},'applied')">✓ Applied</button>
    <button class="warn" onclick="setJobStatus(${j.id},'saved')">★ Save</button>
    <button class="bad"  onclick="setJobStatus(${j.id},'rejected')">✗ Reject</button>
  </div>
</div>`).join('');
}

function renderPosts(){
  const q   = document.getElementById('pq').value.toLowerCase();
  const src = document.getElementById('pSrcFilter').value;
  const st  = document.getElementById('pStFilter').value;

  const f = allPosts.filter(p=>{
    if(st!=='all' && p.status!==st) return false;
    if(src!=='all' && !p.source.includes(src)) return false;
    if(q){
      const hay=`${p.poster_name} ${p.poster_role} ${p.post_text} ${p.skills_found}`.toLowerCase();
      if(!hay.includes(q)) return false;
    }
    return true;
  });

  const el=document.getElementById('posts');
  if(!f.length){ el.innerHTML='<div class="post"><div class="sub">No posts match current filter.</div></div>'; return; }

  el.innerHTML=f.map(p=>`
<div class="post">
  <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px;">
    <div class="post-src">${esc(p.source)}${p.posted_at?' · '+esc(p.posted_at):''}</div>
    ${statusBadge(p.status)}
  </div>
  <div class="post-name">${esc(p.poster_name||'Anonymous')}</div>
  <div class="post-role">${esc(p.poster_role)}</div>
  ${p.skills_found?`<div class="post-skills">🛠 ${esc(p.skills_found)}</div>`:''}
  ${p.location?`<div style="font-size:11px;color:var(--muted)">📍 ${esc(p.location)}</div>`:''}
  <div class="post-body" id="pb${p.id}">${esc(p.post_text)}</div>
  <div class="pactions">
    <a class="view-btn" href="${esc(p.post_url)}" target="_blank" rel="noopener">View Post ↗</a>
    ${p.poster_url&&p.poster_url!==p.post_url?`<a class="view-btn" href="${esc(p.poster_url)}" target="_blank">Profile ↗</a>`:''}
    <button class="ok"   onclick="setPStatus(${p.id},'contacted')" style="font-size:12px">✉ Contacted</button>
    <button class="warn" onclick="setPStatus(${p.id},'saved')"     style="font-size:12px">★ Save</button>
    <button class="sec"  onclick="document.getElementById('pb${p.id}').classList.toggle('exp')" style="font-size:12px">▼ More</button>
  </div>
</div>`).join('');
}

async function setJobStatus(id,s){
  await fetch('/api/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,status:s,type:'job'})});
  await loadJobs();
}
async function setPStatus(id,s){
  await fetch('/api/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,status:s,type:'post'})});
  await loadPosts();
}

async function triggerScan(){
  const st=document.getElementById('scanStatus');
  st.textContent='⏳ Scanning…';
  const raw=document.getElementById('customUrls').value.trim();
  const urls=raw?raw.split('\n').map(s=>s.trim()).filter(Boolean):null;
  const r=await fetch('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({urls})});
  const d=await r.json();
  st.textContent=`✅ ${d.jobs_added} new jobs · ${d.posts_added} new posts · ${d.duplicates_skipped} dupes`;
  await refresh();
}

async function triggerAutoScan(){
  const btn=document.getElementById('scanAllBtn');
  btn.textContent='⏳ Scanning all sources…'; btn.disabled=true;
  const r=await fetch('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({urls:null})});
  const d=await r.json();
  btn.textContent='🔍 Scan Now'; btn.disabled=false;
  alert(`✅ Scan complete!\n• ${d.jobs_added} new jobs (direct links only)\n• ${d.posts_added} new posts\n• ${d.duplicates_skipped} duplicates skipped\n• Total in DB: ${d.total_jobs_in_db}`);
  await refresh();
}

refresh();
</script>
</body>
</html>
"""


# ─────────────────────────── HTTP server ────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):
    def _json(self, data: object, code: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]

        if path == "/api/jobs":
            conn = get_db()
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY match_score DESC, id DESC"
            ).fetchall()
            conn.close()
            self._json([dict(r) for r in rows])
            return

        if path == "/api/posts":
            conn = get_db()
            rows = conn.execute(
                "SELECT * FROM posts ORDER BY id DESC"
            ).fetchall()
            conn.close()
            self._json([dict(r) for r in rows])
            return

        if path == "/api/stats":
            conn = get_db()
            total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            last  = conn.execute("SELECT finished_at FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
            conn.close()
            self._json({"total_jobs": total, "last_scan": dict(last) if last else {}})
            return

        # Serve dashboard for all other GET paths
        body = DASHBOARD_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length  = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode() or "{}")

        if self.path == "/api/status":
            conn = get_db()
            rec_type = payload.get("type","job")
            if rec_type == "post":
                conn.execute("UPDATE posts SET status=? WHERE id=?",
                             (payload["status"], int(payload["id"])))
            else:
                conn.execute("UPDATE jobs SET status=? WHERE id=?",
                             (payload["status"], int(payload["id"])))
            conn.commit(); conn.close()
            self._json({"ok": True})
            return

        if self.path == "/api/scan":
            summary = run_full_scan(custom_urls=payload.get("urls"))
            self._json(summary)
            return

        self._json({"error": "Not found"}, 404)

    def log_message(self, *_) -> None:
        return   # silence access logs


# ─────────────────────────── entry point ────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Job Hunter v3")
    parser.add_argument("--init-db",    action="store_true")
    parser.add_argument("--scan",       action="store_true", help="Run one-off scan and exit")
    parser.add_argument("--serve",      action="store_true", help="Start dashboard + discovery loop")
    parser.add_argument("--port",       type=int, default=SERVE_PORT)
    parser.add_argument("--host",       default=SERVE_HOST,
                        help="Bind host (use 0.0.0.0 for cloud/public access)")
    args = parser.parse_args()

    init_db()

    if args.scan:
        summary = run_full_scan()
        print("Scan complete:", json.dumps(summary, indent=2))
        return

    if args.serve or (not args.init_db):
        t = threading.Thread(target=_background_loop, daemon=True)
        t.start()
        host = args.host
        port = args.port
        server = HTTPServer((host, port), DashboardHandler)
        url = f"http://{'127.0.0.1' if host=='0.0.0.0' else host}:{port}"
        print(f"✅  Job Hunter v3 Dashboard → {url}")
        print(f"📡  Background discovery loop: every {SCAN_INTERVAL//60} min")
        print(f"🗄   Database: {DB_PATH}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n👋  Shutting down.")


if __name__ == "__main__":
    main()
