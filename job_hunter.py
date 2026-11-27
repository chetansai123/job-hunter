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
import html as htmllib
import json
import os
import re
import sqlite3
import sys
import ssl
import threading
import unicodedata
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ─────────────────────────── SSL fix ────────────────────────────────────────
def _setup_ssl() -> None:
    """python.org builds on macOS ship WITHOUT CA certificates, so every HTTPS call
    fails with CERTIFICATE_VERIFY_FAILED. Point Python at a real CA bundle."""
    cands = [os.environ.get("SSL_CERT_FILE")]
    try:
        import certifi  # type: ignore
        cands.append(certifi.where())
    except Exception:
        pass
    cands += ["/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt",
              "/opt/homebrew/etc/ca-certificates/cert.pem"]
    for c in cands:
        if c and Path(c).exists():
            ctx = ssl.create_default_context(cafile=c)
            ssl._create_default_https_context = lambda ctx=ctx: ctx  # type: ignore
            return


_setup_ssl()


def _load_dotenv() -> None:
    """Tiny .env reader (keys such as APIFY_TOKEN / FIRECRAWL_API_KEY live in the git-ignored .env file)."""
    f = Path(__file__).resolve().parent / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

# ─────────────────────────── config & env ───────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DB_PATH   = Path(os.environ.get("DB_PATH",   str(BASE_DIR / "jobs.db")))
RULES_PATH = BASE_DIR / "rules.json"

FIRECRAWL_URL      = os.environ.get("FIRECRAWL_API_URL", "http://localhost:3002").rstrip("/")
FIRECRAWL_API_KEY  = os.environ.get("FIRECRAWL_API_KEY", "")   # set for cloud Firecrawl
# /v2/search is cloud-only: with a key, search goes to Firecrawl cloud while page scraping stays on the free local server
FIRECRAWL_SEARCH_URL = "https://api.firecrawl.dev" if FIRECRAWL_API_KEY else FIRECRAWL_URL
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
    "startup.jobs", "jobs.ycombinator.com", "workatastartup.com",
    "justhireme.ai", "cutshort.io", "instahyre.com", "backdoor.app", "foundit.in",
    "shine.com", "timesjobs.com", "jooble.org", "talent.com", "careerjet.com",
    "jobrapido.com", "whatjobs.com", "flexjobs.com", "remote.co", "builtin.com",
    "dailyremote.com", "jobgether.com", "nodesk.co", "remoteok.io", "levels.fyi",
    "hirist.tech", "hirist.com", "iimjobs.com", "apna.co", "internshala.com",
    "freshersworld.com", "jobsora.com", "learn4good.com", "jobtensor.com", "careerbuilder.com",
    "adzuna.in", "adzuna.com", "neuvoo.com", "snagajob.com", "lensa.com", "jobleads.com",
    "wellfound.com", "weworkremotely.com", "workingnomads.com", "remoteworker.co",
    "huntyourtribe.com", "urbanodx.com", "matcha.fm", "jsgurujobs.com", "torre.ai", "torre.co",
    "jobsinthewild.tal.club", "tal.club", "joblelo.online", "remotive.io", "arbeitnow.nl", "linkedin.com",
    "jobs.workable.com", "lnkd.in", "glassdoor.co.in", "glassdoor.com", "jobs.polymer.co", "arc.dev", "uplers.com", "remote100k.com",
    "turing.com", "bit.ly", "simplify.jobs", "justremote.co", "builtin.com", "remote.co", "dailyremote.com", "remoteleaf.com",
})

# Non-job hosts that show up as links on job pages — never an apply link
JUNK_LINK_HOSTS: frozenset[str] = frozenset({
    "twitter.com", "x.com", "facebook.com", "instagram.com", "youtube.com", "google.com",
    "apple.com", "play.google.com", "github.com", "medium.com", "t.me", "wa.me",
    "cloudflare.com", "w3.org", "schema.org", "mailto", "tiktok.com", "reddit.com",
})

# Well-known ATS / company career domains — always direct apply
ATS_HOSTS: frozenset[str] = frozenset({
    "boards.greenhouse.io", "job-boards.greenhouse.io",
    "jobs.lever.co", "apply.lever.co",
    "jobs.ashbyhq.com",
    "apply.workable.com",
    "jobs.smartrecruiters.com", "careers.smartrecruiters.com",
    "app.bamboohr.com", "keka.com", "zohorecruit.in", "zohorecruit.com",
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
        rules = json.load(f)
    extra = [c.strip() for c in os.environ.get("EXCLUDE_COMPANIES", "").split(",") if c.strip()]   # keeps private choices out of a public repo
    if extra:
        rules["excluded_companies"] = sorted(set(rules.get("excluded_companies", [])) | set(extra))
    return rules


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
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

        CREATE TABLE IF NOT EXISTS seen_jobs (
            url        TEXT PRIMARY KEY,
            first_seen TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rejected_urls (
            url    TEXT PRIMARY KEY,
            reason TEXT DEFAULT '',
            at     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ats_boards (
            kind     TEXT NOT NULL,
            token    TEXT NOT NULL,
            source   TEXT DEFAULT '',
            failures INTEGER DEFAULT 0,
            added_at TEXT NOT NULL,
            PRIMARY KEY (kind, token)
        );

        CREATE TABLE IF NOT EXISTS discovered_companies (
            key        TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            website    TEXT DEFAULT '',
            source     TEXT DEFAULT '',
            info       TEXT DEFAULT '{}',
            first_seen TEXT NOT NULL,
            checked_at TEXT DEFAULT '',
            board      TEXT DEFAULT '',
            status     TEXT DEFAULT 'new'
        );

        CREATE TABLE IF NOT EXISTS startups (
            key          TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            funded_on    TEXT DEFAULT '',
            data         TEXT DEFAULT '{}',
            score        INTEGER DEFAULT 0,
            status       TEXT DEFAULT 'new',
            enriched_at  TEXT DEFAULT '',
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS li_companies (
            slug         TEXT PRIMARY KEY,
            name         TEXT DEFAULT '',
            last_checked TEXT DEFAULT '',
            posts_seen   INTEGER DEFAULT 0,
            hiring_found INTEGER DEFAULT 0,
            failures     INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS workday_boards (
            host     TEXT NOT NULL,
            tenant   TEXT NOT NULL,
            site     TEXT NOT NULL,
            source   TEXT DEFAULT '',
            failures INTEGER DEFAULT 0,
            PRIMARY KEY (host, tenant, site)
        );

        CREATE TABLE IF NOT EXISTS kv (
            k TEXT PRIMARY KEY,
            v TEXT
        );

        CREATE TABLE IF NOT EXISTS careers_pages (
            url          TEXT PRIMARY KEY,
            source       TEXT DEFAULT '',
            last_checked TEXT
        );

        CREATE TABLE IF NOT EXISTS company_contacts (
            company    TEXT PRIMARY KEY,
            data       TEXT NOT NULL,
            real       INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL
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

    # ── Schema migrations: add columns that older DB files may be missing ──
    # Safe to run on every startup — each ALTER is ignored if column exists.
    _migrations = [
        "ALTER TABLE scan_runs ADD COLUMN posts_added INTEGER DEFAULT 0",
        "ALTER TABLE scan_runs ADD COLUMN urls_scanned INTEGER DEFAULT 0",
        "ALTER TABLE jobs      ADD COLUMN notes TEXT DEFAULT ''",
        "ALTER TABLE posts     ADD COLUMN location TEXT DEFAULT ''",
        "ALTER TABLE jobs      ADD COLUMN contacts TEXT DEFAULT '[]'",
        "ALTER TABLE jobs      ADD COLUMN link_type TEXT DEFAULT 'direct'",
        "ALTER TABLE jobs      ADD COLUMN board_url TEXT DEFAULT ''",
        "ALTER TABLE jobs      ADD COLUMN link_status TEXT DEFAULT 'active'",
        "ALTER TABLE jobs      ADD COLUMN locked INTEGER DEFAULT 0",
        "ALTER TABLE jobs      ADD COLUMN apply_email TEXT DEFAULT ''",
        "ALTER TABLE jobs      ADD COLUMN checked_at TEXT DEFAULT ''",
        "ALTER TABLE posts     ADD COLUMN dedup_key TEXT DEFAULT ''",
        "ALTER TABLE ats_boards ADD COLUMN last_polled TEXT DEFAULT ''",
        "ALTER TABLE workday_boards ADD COLUMN last_polled TEXT DEFAULT ''",
        "ALTER TABLE posts     ADD COLUMN norm_text TEXT DEFAULT ''",
    ]
    for sql in _migrations:
        try:
            conn.execute(sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass   # column already exists — that's fine

    _purge_and_migrate(conn)
    conn.close()


def _purge_and_migrate(conn: sqlite3.Connection) -> None:
    """Re-key old rows by exact apply URL, drop rows that break the rules, backfill seen_jobs."""
    rows = conn.execute("SELECT id, company, title, direct_apply_url, job_hash FROM jobs").fetchall()
    for r in rows:
        url = r["direct_apply_url"]
        if looks_like_listing(url):
            conn.execute("DELETE FROM jobs WHERE id=?", (r["id"],))
            continue
        if is_aggregator(url):                       # board-only rows are re-vetted (direct link / e-mail) or removed
            conn.execute("UPDATE jobs SET link_type='board' WHERE id=? AND link_type!='email'", (r["id"],))
        nu = norm_url(url)
        h = make_hash(nu)
        if h != r["job_hash"]:
            try:
                conn.execute("UPDATE jobs SET job_hash=? WHERE id=?", (h, r["id"]))
            except sqlite3.IntegrityError:
                conn.execute("DELETE FROM jobs WHERE id=?", (r["id"],))
                continue
        conn.execute("INSERT OR IGNORE INTO seen_jobs (url, first_seen) VALUES (?,?)", (nu, now_utc()))
    seen_keys: set[str] = set()
    for r in conn.execute("SELECT id, post_text, status, dedup_key FROM posts ORDER BY id").fetchall():
        k = r["dedup_key"] or post_dedup_key(dict(r))
        if k and k in seen_keys and r["status"] == "new":
            conn.execute("DELETE FROM posts WHERE id=?", (r["id"],))
            continue
        seen_keys.add(k)
        conn.execute("UPDATE posts SET dedup_key=?, norm_text=? WHERE id=?", (k, " ".join(_post_words(r["post_text"])), r["id"]))
    for r in conn.execute("SELECT url FROM seen_jobs").fetchall():
        conn.execute("INSERT OR IGNORE INTO seen_jobs (url, first_seen) VALUES (?,?)", (norm_url(r["url"]), now_utc()))
    conn.commit()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_hash(*parts: str) -> str:
    key = "|".join(p.strip().lower() for p in parts)
    return hashlib.sha256(key.encode()).hexdigest()[:24]


# ─────────────────────────── URL resolution ─────────────────────────────────

def _netloc(url: str) -> str:
    h = urllib.parse.urlparse((url or "").lower()).netloc.split("@")[-1].split(":")[0]
    return h[4:] if h.startswith("www.") else h


_TRACKING_PARAMS = {"ref", "source", "src", "gh_src", "lever-source", "lever-origin", "lever-via", "utm_source", "utm_medium",
                    "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "trk", "trackingid", "referrer", "refid"}


def norm_url(url: str) -> str:
    """Canonical form of an apply URL, used for exact-URL dedup (rule 12)."""
    try:
        p = urllib.parse.urlparse((url or "").strip())
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in _TRACKING_PARAMS]
        host = _netloc(url)
        path = p.path.rstrip("/") or "/"
        if host in ("jobs.lever.co", "jobs.ashbyhq.com", "boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"):
            path = re.sub(r"/(apply|application)$", "", path).lower() if host != "boards.greenhouse.io" else re.sub(r"/(apply|application)$", "", path)
            host = host.replace("job-boards.eu.", "boards.").replace("job-boards.", "boards.")
        return urllib.parse.urlunparse(("https", host, path, "", urllib.parse.urlencode(sorted(q)), ""))
    except Exception:
        return (url or "").strip()


_LISTING_PATH = re.compile(
    r"^/((categories|category|tags?|search|companies|company|roles?|locations?)(/[^/]+)?"
    r"|remote-jobs|jobs|careers|open-positions|openings|positions|vacancies)/?$", re.I)


def looks_like_listing(url: str) -> bool:
    """True for category / search / company-index pages (not one specific opening)."""
    p = urllib.parse.urlparse(url or "")
    path = p.path or "/"
    if path in ("", "/"):
        return True
    if "gh_jid" in p.query:
        return False                                          # company-hosted Greenhouse posting
    host = _netloc(url)
    segs = [x for x in path.split("/") if x]
    ab = ats_board_from_url(url)
    if ab:
        if ab[0] == "zoho":
            return len(segs) < 3                              # /jobs/Careers/<id>/<title> is the posting
        if ab[0] == "keka":
            return "jobdetails" not in segs
        return len(segs) < 2                                  # board root, not a posting
    if host == "apply.workable.com":
        return len(segs) < 3                                  # /slug/j/<id> is the posting
    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com", "ats.rippling.com", "jobs.gem.com"):
        return len(segs) < 2 or (host == "ats.rippling.com" and len(segs) < 3)
    if host.endswith((".bamboohr.com", ".applytojob.com")) and len(segs) < 2:
        return True
    return bool(_LISTING_PATH.search(path)) or bool(re.search(r"/(?:jobs|careers|positions|openings|open-positions|join-us|join-the-team)/?$", path, re.I))


def looks_like_job_page(url: str) -> bool:
    if looks_like_listing(url) or is_aggregator(url):
        return False
    segs = [x for x in urllib.parse.urlparse(url).path.split("/") if x]
    return len(segs) >= 2 or "gh_jid" in url or "jobid" in url.lower()


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
    # 5 — look for ATS or career links in the page (scrape the aggregator page ourselves
    #     with Firecrawl if the caller didn't already; capped per scan)
    if not page_links and platform_url and _take_scrape_budget():
        sc = fc_scrape(platform_url)
        page_links = [l for l in (sc.get("links") or []) if isinstance(l, str)] if isinstance(sc, dict) else []
    cleaned = [l for l in page_links if isinstance(l, str) and l.startswith("http")
               and not is_aggregator(l) and _netloc(l) not in JUNK_LINK_HOSTS
               and not any(_netloc(l).endswith("." + j) for j in JUNK_LINK_HOSTS)]
    for link in cleaned:                       # known ATS hosts first
        if is_ats(link) and not looks_like_listing(link):
            return link
    for link in cleaned:                       # then links that say "apply"
        if re.search(r"/apply\b|apply[-_]?now|/applications?/new", link.lower()) and not looks_like_listing(link):
            return link
    # 6 — give up (aggregator-only / locked page → job is dropped, never shown as a third-party link)
    return None


_SCRAPE_BUDGET = {"left": 80}
_SCRAPE_LOCK = threading.Lock()


def _take_scrape_budget() -> bool:
    with _SCRAPE_LOCK:
        if _SCRAPE_BUDGET["left"] <= 0:
            return False
        _SCRAPE_BUDGET["left"] -= 1
        return True


# ─────────────────────────── HTTP helpers ───────────────────────────────────

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JobHunterBot/3.0; +personal-automation)"}

# ── Firecrawl availability cache ─────────────────────────────────────────────
_FC_OK: bool | None = None   # None = not yet tested

def _fc_available() -> bool:
    """Return True if Firecrawl server (local or cloud) is reachable."""
    global _FC_OK
    if _FC_OK is not None:
        return _FC_OK
    # Cloud Firecrawl: if API key is set, assume cloud URL is reachable
    if FIRECRAWL_API_KEY:
        _FC_OK = True
        print("[firecrawl] using cloud Firecrawl API")
        return True
    # Local Firecrawl: quick health check
    try:
        req = urllib.request.Request(
            f"{FIRECRAWL_URL}/health",
            headers={"User-Agent": "JobHunterBot/3.0"},
        )
        with urllib.request.urlopen(req, timeout=3):
            _FC_OK = True
            print("[firecrawl] local server is reachable")
            return True
    except Exception:
        _FC_OK = False
        print("[firecrawl] not reachable — Firecrawl-dependent sources will be skipped")
        return False


def _fc_headers() -> dict:
    """Return headers for Firecrawl requests (adds Bearer token for cloud)."""
    h = {"Content-Type": "application/json", "User-Agent": "JobHunterBot/3.0"}
    if FIRECRAWL_API_KEY:
        h["Authorization"] = f"Bearer {FIRECRAWL_API_KEY}"
    return h


def http_get(url: str, timeout: int = 20, quiet: bool = False) -> dict | list:
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        if not quiet:
            print(f"  [GET ✗] {url[:80]} → {e}")
        return {}


def http_get_text(url: str, timeout: int = 20, quiet: bool = False) -> str:
    hdr = {"User-Agent": BROWSER_UA, "Accept": "text/html,*/*", "Accept-Language": "en-US,en;q=0.9"} if quiet else _HEADERS
    req = urllib.request.Request(url, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(1_500_000).decode("utf-8", errors="replace")
    except Exception as e:
        if not quiet:
            print(f"  [GET-text ✗] {url[:80]} → {e}")
        return ""


def http_post(url: str, payload: dict, timeout: int = 45, extra_headers: dict | None = None) -> dict:
    data = json.dumps(payload).encode()
    headers = {**_HEADERS, "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  [POST ✗] {url[:80]} → {e}")
        return {}


# ─────────────────────────── Firecrawl helpers ──────────────────────────────

def fc_scrape(url: str) -> dict:
    if not _fc_available():
        return {}
    r = http_post(f"{FIRECRAWL_URL}/v2/scrape",
                  {"url": url, "formats": ["markdown", "links"]},
                  timeout=60, extra_headers=_fc_headers())
    return r.get("data", r)


_SEARCH_OK: bool | None = None


def _raw_post(url: str, payload: dict, timeout: int = 60) -> dict:
    """POST that also returns the JSON body of HTTP errors (Firecrawl sends useful ones)."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=_fc_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": str(e)}


def fc_search_available() -> bool:
    """Firecrawl /v2/search is cloud-only. Without FIRECRAWL_API_KEY the local server refuses it."""
    global _SEARCH_OK
    if _SEARCH_OK is not None:
        return _SEARCH_OK
    if not _fc_available():
        _SEARCH_OK = False
        return False
    r = _raw_post(f"{FIRECRAWL_SEARCH_URL}/v2/search", {"query": "software engineer jobs", "limit": 1}, timeout=30)
    _SEARCH_OK = bool(r.get("success", True)) and r.get("code") != "CLOUD_ONLY" and "data" in r
    if not _SEARCH_OK:
        print("[firecrawl] search is unavailable (local Firecrawl is cloud-only for /v2/search). "
              "Set FIRECRAWL_API_KEY to enable search-based sources + real contact lookup.")
    return _SEARCH_OK


def fc_search(query: str, limit: int = 10) -> list[dict]:
    if not fc_search_available():
        return []
    r = _raw_post(f"{FIRECRAWL_SEARCH_URL}/v2/search",
                  {"query": query, "limit": limit,
                   "scrapeOptions": {"formats": ["markdown", "links"]}})
    data = r.get("data", []) or []
    if isinstance(data, dict):                       # v2 groups results by type
        data = data.get("web", []) or []
    return data


def fc_map(url: str, limit: int = 50) -> list[str]:
    if not _fc_available():
        return []
    r = http_post(f"{FIRECRAWL_URL}/v2/map", {"url": url, "limit": limit},
                  extra_headers=_fc_headers())
    links = r.get("links") or r.get("data", {}).get("links") or []
    return [u for u in links if isinstance(u, str)]


# ─────────────────────────── filter engine ──────────────────────────────────
#
#  Two stages:
#   1. passes_hard_filters()  – cheap TITLE/COMPANY pre-filter used by collectors.
#                               It never looks for a stack (titles lie) – it only drops
#                               obvious non-matches so we don't fetch thousands of pages.
#   2. analyze_jd()           – runs on the FULL job description text fetched from the
#                               direct apply page (or ATS API). All real decisions
#                               (experience, stack, location, salary, staffing) live here.

def days_ago(dt_str: str) -> int | None:
    if not dt_str:
        return None
    clean = str(dt_str).strip()[:19].rstrip("Z").replace("T", " ")
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            d = datetime.strptime(clean[:n], fmt)
            return (datetime.now(timezone.utc).replace(tzinfo=None) - d).days
        except ValueError:
            continue
    return None


@lru_cache(maxsize=2048)
def _term_re(term: str) -> "re.Pattern[str]":
    """Word-boundary-ish matcher that also works for terms like 'c#', '.net', 'node.js'."""
    return re.compile(r"(?<![a-z0-9+#])" + re.escape(term.lower()) + r"(?![a-z0-9+#])")


def _has(term: str, text: str) -> bool:
    return bool(_term_re(term).search(text))


def _hits(terms: list[str], text: str) -> list[str]:
    return [t for t in terms if _has(t, text)]


def _occurrences(terms: list[str], text: str) -> int:
    return sum(len(_term_re(t).findall(text)) for t in terms)


def stack_hits(text: str, rules: dict) -> list[str]:
    """Core JS-family terms found in text (used for posts + scoring). ANY one is enough."""
    return _hits(rules.get("stack", {}).get("core", []), text.lower())


_ROLE_WORDS = re.compile(
    r"\b(software|developer|programmer|full[\s-]?stack|front[\s-]?end|back[\s-]?end|mern|sde|swe|"
    r"react|node(?:\.?js)?|javascript|typescript|next\.?js|web|product engineer|founding engineer|"
    r"application engineer|ui engineer|api engineer|platform engineer)\b", re.I)

# Titles that are clearly a different discipline. (Stack is NOT checked on titles.)
_NEGATIVE_TITLE = [
    "data scientist", "data analyst", "devops", "sre", "site reliability",
    "machine learning", "ml engineer", "quality assurance", "test engineer",
    "security engineer", "sales", "solutions engineer", "support engineer", "customer",
    "embedded", "firmware", "hardware", "mechanical", "civil", "electrical", "network engineer",
    "designer", "recruiter", "marketing",
    "product manager", "project manager", "account", "business analyst", "technical writer",
    "dba", "database administrator", "mainframe", "servicenow", "qa", "project engineer",
    "quality", "growth", "gtm", "interviewer", "ecosystem", "infrastructure", "technical services",
    "wordpress", "hubspot", "seo", "automotive", "vehicle", "solar", "creative", "annotation",
    "trainer", "tutor", "content", "writer", "reviewer", "contractor", "freelance",
    "systems engineer", "forward deployed", "developer relations", "devrel", "field engineer",
    "integration engineer", "deployment engineer", "solutions", "technical account", "new grad",
    "graduate engineer", "data security", "support", "implementation", "consultant", "architect",
    "in test", "sdet", "test automation", "ci engineer", "android", "ios", "flutter", "solidity", "developer advocate",
    "customer success", "support engineer", "sales engineer", "solutions engineer",
    "power bi", "bi developer", "tableau", "etl", "data warehouse", "datawarehouse", "dwh", "qlik", "business intelligence",
    "informatica", "snowflake developer", "mainframe", "cobol", "oracle apps", "peoplesoft",
    "liferay", "aem", "adobe experience manager", "snaplogic", "vermilion", "axiom", "kdb", "qlik", "qliksense",
    "sharepoint", "power apps", "powerapps", "openwrt", "data engineer", "developer success", "uat", "oracle programmer",
    "snowflake", "sitecore", "dynamics 365", "pega", "appian", "outsystems", "mulesoft", "servicenow developer",
]
_SOFT_NEG = {"customer", "marketing", "growth", "content", "creative", "solutions", "implementation", "consultant", "support",
             "account", "quality", "infrastructure", "ecosystem", "technical services", "contractor", "freelance", "reviewer",
             "writer", "trainer", "tutor", "architect", "integration engineer", "deployment engineer", "field engineer"}


def title_relevant(title: str, combined: str = "", rules: dict | None = None) -> bool:
    return bool(_ROLE_WORDS.search(title or ""))


def is_staffing_company(company: str, rules: dict) -> str | None:
    c = (company or "").lower()
    if any(a in c for a in rules.get("staffing_filter", {}).get("it_services_allowed", [])):
        return None                      # IT-services majors are allowed (they hire for themselves)
    for pat in rules.get("staffing_filter", {}).get("company_name_patterns", []):
        if pat.lower() in c:
            return pat
    return None


def is_staffing_text(text: str, rules: dict) -> str | None:
    t = (text or "").lower()
    for ph in rules.get("staffing_filter", {}).get("body_phrases", []):
        if _has(ph.lower(), t):
            return ph
    return None


_AMBIG_TOKENS = {"hybrid", "in", "office", "on", "site", "onsite", "n", "a", "na", "tbd", "multiple",
                 "locations", "location", "various", "remote", "or", "and", "work", "from", "home", "wfh"}


def location_ok(loc: str, rules: dict, jd: str | None = None) -> tuple[bool, str]:
    """Allow-list: India place / worldwide / APAC / plain 'remote' → ok.
    Hybrid / in-office / N/A with no city → ok only if the JD itself mentions India
    (jd=None means 'not read yet' and defers the decision).
    Anything else (Kuwait, Americas, Ontario, Remote-AZ ...) → rejected."""
    l = (loc or "").lower().strip()
    if not l:
        return True, "ok"
    L = rules.get("locations", {})
    has_india = "india" in l or bool(re.search(r"(?:^|[,\s(/-])(?:in|ind)\s*\)?$", l)) or any(_has(p, l) for p in L.get("india_places", []))
    if any(_has(p, l) for p in L.get("non_india_places", [])) and "india" not in l and not re.search(r"worldwide|anywhere in the world", l):
        return False, f"Location not India-eligible: {loc}"
    if has_india:
        return True, "ok"
    if any(p in l for p in L.get("global_ok_phrases", [])) and not re.search(r"anywhere in (?!the world)", l):
        return True, "ok"
    words = set(re.sub(r"[^a-z]+", " ", l).split())
    if words <= _AMBIG_TOKENS:
        if words == {"remote"} or words <= {"remote", "or", "and"}:
            return True, "ok"
        if jd is None:
            return True, "ok"
        head = jd.lower()[:4000]
        if any(_has(p, head) for p in L.get("india_places", [])):
            return True, "ok"
        return False, f"Location '{loc}' unspecified and JD never mentions India"
    return False, f"Location not India-eligible: {loc}"


_REGION = r"(?:us|usa|u\.s\.a?\.?|united states|canada|uk|united kingdom|europe|european union|eu|emea|americas|north america|latam|latin america)"
_GEO_RESTRICT = re.compile(
    r"(?:must|should|need to|required to)\s+(?:be\s+)?(?:located|based|reside|residing|live|living)\s+in\s+(?:the\s+)?" + _REGION + r"\b|"
    r"(?:based|located)\s+in\s+(?:the\s+)?" + _REGION + r"\b|"
    r"(?:remote|home[- ]?based)\s*(?:position\s*)?(?:available\s+)?(?:in|within|-|–)\s*(?:the\s+)?" + _REGION + r"\b|"
    r"\b(?:est|pst|cst|cet|edt|pdt)\s+(?:time\s*zone|hours)|" + r"\b(?:us|european|eu|uk|north american)\s+time\s*zones?\b|"
    r"(?:not\s+(?:open|available)\s+to\s+(?:candidates\s+)?(?:in|from)\s+india|excluding\s+india)", re.I)


def passes_hard_filters(title: str, company: str, url: str, body: str, rules: dict) -> tuple[bool, str]:
    t, c, u, b = (title or "").lower(), (company or "").lower(), (url or "").lower(), (body or "").lower()

    for excl in rules.get("excluded_companies", []):
        if _has(excl.lower(), c):
            return False, f"Excluded company: {excl}"
    for plat in rules.get("excluded_platforms", []):
        if plat.lower() in u:
            return False, f"Excluded platform: {plat}"
    if is_staffing_company(company, rules):
        return False, "Staffing/recruitment agency"
    for kw in rules.get("experience", {}).get("reject_title_keywords", []):
        k = kw.strip().lower().rstrip(".")
        if re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", t):
            return False, f"Reject title kw: {kw.strip()}"
    if re.search(r"\b(sde|swe|engineer|developer)\W*(?:3|4|5|iii|iv|v)\b(?!\s*(?:\+|-|–|to\b|years?|yrs?|openings?|positions?|days?|months?))", t):
        return False, "Senior level in title (3+/III+)"
    strong_dev = bool(re.search(r"software (?:engineer|developer)|full[\s-]?stack|front[\s-]?end|back[\s-]?end|\bsde\b|\bswe\b|"
                                r"react|node|javascript|typescript|next\.?js|web developer|mern", t))
    for bad in _NEGATIVE_TITLE:
        if bad.strip() in _SOFT_NEG and strong_dev:
            continue
        if _has(bad.strip(), t):
            return False, f"Wrong discipline in title: {bad.strip()}"
    _L = rules.get("locations", {})
    if any(_has(pl, t) for pl in _L.get("non_india_places", [])) and not re.search(r"india|worldwide|global|anywhere|apac", t):
        return False, "Non-India location in title"
    _S = rules.get("stack", {})
    _blk = _hits(_S.get("blocked", []), t)
    if _blk and not _hits(_S.get("core", []), t):
        return False, f"Framework outside your stack in title ('{_blk[0]}')"
    if "india" not in b:
        for phrase in rules.get("locations", {}).get("reject_region_phrases", []):
            if re.search(r"(?<![a-z0-9])" + re.escape(phrase.lower()) + r"(?![a-z0-9])", b):
                return False, f"Reject region: {phrase}"
    if not _ROLE_WORDS.search(t):
        return False, "Title not a software-dev role"
    return True, "ok"


# ── experience / salary parsing ─────────────────────────────────────────────
_YR = r"(?:years?|yrs?|yoe)['’]?"
_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_BETWEEN_RE = re.compile(r"between\s+(\d{1,2})\s+(?:and|to|&)\s+(\d{1,2})\s*" + _YR, re.I)
_RANGE_RE = re.compile(r"(\d{1,2})(?:\.\d+)?\s*\+?\s*(?:-|–|—|to)\s*(\d{1,2})(?:\.\d+)?\s*\+?\s*" + _YR, re.I)
_MIN_RE = re.compile(r"(?:at\s*least|atleast|minimum(?:\s+of)?|min\.?|over|more\s+than)\s*(\d{1,2})(?:\.\d+)?\s*\+?\s*" + _YR, re.I)
_PLUS_RE = re.compile(r"(\d{1,2})(?:\.\d+)?\s*\+\s*" + _YR, re.I)
_PLAIN_RE = re.compile(
    r"(\d{1,2})(?:\.\d+)?\s*" + _YR +
    r"\s*(?:of\s+)?(?:(?:relevant|professional|hands-on|hands\s+on|industry|software|work|total|overall|prior|proven|solid|strong|real-world|commercial|development)\s+)*(?:experience|exp\b)", re.I)
_PLAIN2_RE = re.compile(r"experience\s*(?:of|:|-|–)?\s*(\d{1,2})(?:\.\d+)?\s*" + _YR, re.I)
_EXP_CTX = re.compile(r"experience|exp\b|developer|engineer|professional|industry|work(?:ing)?|background|track record|expertise", re.I)


def parse_experience(text: str) -> list[tuple[int, int | None]]:
    """Return experience statements as (min_years, max_years_or_None)."""
    text = re.sub(r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\b(?=[\s-]*(?:\+|plus)?\s*(?:years?|yrs?))",
                  lambda m: str(_WORDNUM[m.group(1).lower()]), text, flags=re.I)
    spans: list[tuple[int, int]] = []
    out: list[tuple[int, int | None]] = []
    starts: list[int] = []

    def free(m: "re.Match[str]") -> bool:
        return all(m.end() <= a or m.start() >= b for a, b in spans)

    def ctx(m: "re.Match[str]") -> bool:
        s = max(0, m.start() - 70)
        return bool(_EXP_CTX.search(text[s:m.end() + 70]))

    for rx, kind in ((_BETWEEN_RE, "range"), (_RANGE_RE, "range"), (_MIN_RE, "min"), (_PLUS_RE, "plus"),
                     (_PLAIN_RE, "plain"), (_PLAIN2_RE, "plain")):
        for m in rx.finditer(text):
            if not free(m):
                continue
            if kind in ("range", "min", "plus") and not ctx(m):
                continue
            spans.append((m.start(), m.end()))
            if kind == "range":
                lo, hi = int(m.group(1)), int(m.group(2))
                if hi < lo or hi > 20:
                    continue
                out.append((lo, hi)); starts.append(m.start())
            else:
                if int(m.group(1)) > 15:          # "30+ years" is about the company, not the role
                    continue
                out.append((int(m.group(1)), None)); starts.append(m.start())
    return [o for _, o in sorted(zip(starts, out), key=lambda x: x[0])]


def _fmt_exp(stmts: list[tuple[int, int | None]]) -> str:
    if not stmts:
        return "not stated"
    lo, hi = min(stmts, key=lambda s: s[0])
    return f"{lo}–{hi} yrs" if hi else f"{lo}+ yrs"


_CUR = r"(?:₹|rs\.?|inr)"
_NUM = r"(\d{1,3}(?:\.\d+)?)"
_LPA_PATTERNS = [
    (re.compile(_NUM + r"\s*(?:-|–|to)\s*" + _NUM + r"\s*(?:lpa|l\.p\.a)\b", re.I), 1),
    (re.compile(_NUM + r"\s*(?:lpa|l\.p\.a)\b", re.I), 1),
    (re.compile(_NUM + r"\s*(?:-|–|to)\s*" + _NUM + r"\s*(?:lakhs?|lacs?)\s*(?:per\s*annum|p\.?a\.?|annually|ctc|/\s*year)", re.I), 1),
    (re.compile(_NUM + r"\s*(?:lakhs?|lacs?)\s*(?:per\s*annum|p\.?a\.?|annually|ctc|/\s*year)", re.I), 1),
    (re.compile(r"ctc\s*(?:of|:|-)?\s*" + _CUR + r"?\s*" + _NUM + r"\s*(?:-|–|to)?\s*" + _CUR + r"?\s*(?:" + _NUM + r")?\s*(?:lakhs?|lacs?|l\b)", re.I), 1),
    (re.compile(_CUR + r"\s*" + _NUM + r"\s*(?:l|lakhs?|lacs?)?\s*(?:-|–|to)\s*" + _CUR + r"?\s*" + _NUM + r"\s*(?:l\b|lakhs?|lacs?)", re.I), 1),
    (re.compile(_CUR + r"\s*" + _NUM + r"\s*(?:l\b|lakhs?|lacs?)", re.I), 1),
    (re.compile(_NUM + r"\s*(?:-|–|to)\s*" + _NUM + r"\s*l\b(?=\s*(?:inr|per annum|pa\b|/))", re.I), 1),
    (re.compile(_NUM + r"\s*(?:lakhs?|lacs?)\s*(?:per|/|a)\s*month", re.I), 12),
]
_INR_RE = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]{6,10})(?!\s*(?:/|per)\s*(?:month|mo|hour|hr))", re.I)


def parse_salary_lpa(text: str) -> tuple[float | None, str]:
    """Highest INR annual figure disclosed, in LPA (None if nothing disclosed). Needs INR/LPA/per-annum context —
    '10 lakh users' is not a salary."""
    best: float | None = None
    snippet = ""
    for rx, mult in _LPA_PATTERNS:
        for m in rx.finditer(text or ""):
            nums = [float(g) for g in m.groups() if g]
            if not nums:
                continue
            v = max(nums) * mult
            if best is None or v > best:
                best, snippet = v, m.group(0).strip()
    for m in _INR_RE.finditer(text or ""):
        try:
            v = int(m.group(1).replace(",", "")) / 100000
        except ValueError:
            continue
        if 1 <= v <= 500 and (best is None or v > best):
            best, snippet = v, m.group(0).strip()
    return best, snippet


_DEAD_PHRASES = [
    "no longer accepting applications", "position has been filled", "job is no longer available",
    "this job has expired", "posting has expired", "page not found", "job board you were viewing is no longer active",
    "this position is closed", "no longer open", "job not found", "we couldn't find that job",
    "this job has been closed", "applications are closed", "applications closed", "no longer available",
    "not currently hiring", "has been removed", "has been closed", "position is no longer", "role is no longer",
    "job posting is no longer", "this posting is closed", "this role has been filled", "job has been filled",
    "no longer accepting", "opening is closed", "vacancy is closed",
]
_SHORTENERS = {"lnkd.in", "bit.ly", "t.co", "tinyurl.com", "goo.gl", "ow.ly", "buff.ly", "rb.gy", "is.gd", "cutt.ly", "shorturl.at"}


def bad_apply_target(u: str, own: str = "") -> bool:
    """A link that is NOT a real company/ATS posting: shortener, job board, social/junk host, bare root."""
    if not u:
        return True
    h = _netloc(u)
    if h in _SHORTENERS or is_aggregator(u) or h in JUNK_LINK_HOSTS or any(h.endswith("." + j) for j in JUNK_LINK_HOSTS):
        return True
    if h.endswith("ycombinator.com") and not urllib.parse.urlparse(u).path.startswith("/companies/"):
        return True
    path = urllib.parse.urlparse(u).path.strip("/")
    return path == "" or looks_like_listing(u)



def analyze_jd(title: str, company: str, jd: str, rules: dict,
               location: str = "", salary: str = "", url: str = "") -> tuple[bool, str, dict]:
    """Decide on the FULL job description. Returns (ok, reason, info)."""
    t, b = (title or "").lower(), (jd or "").lower()
    full = t + "\n" + b
    S = rules.get("stack", {})
    E = rules.get("experience", {})

    ok, reason = passes_hard_filters(title, company, url, b, rules)
    if not ok:
        return False, reason, {}

    p = is_staffing_text(b, rules)
    if p:
        return False, f"Staffing/agency post ('{p}')", {}

    # seniority hidden in the body ("We are seeking a highly skilled Senior Software Engineer…")
    m = re.search(r"(?:seeking|looking for|hiring|need)\s+(?:an?\s+)?(?:[\w,\- ]{0,40}?)\b(senior|sr\.?|staff|principal|lead)\s+"
                  r"(?:software|full|back|front|web|engineer|developer)", b[:1500])
    if m:
        return False, f"Senior role in JD body ('{m.group(1)}')", {}

    ok, reason = location_ok(location, rules, jd or "")
    if not ok:
        return False, reason, {}
    if _GEO_RESTRICT.search(jd or "") and not re.search(r"india|worldwide|anywhere in the world|apac|asia", b[:6000]):
        return False, "JD restricts hiring to a non-India region", {}

    # ── stack: ANY core term is enough; wrong-stack dominated JDs are rejected ──
    # JavaScript ecosystem is the PRIMARY stack, but other stacks are NOT rejected — they are
    # included and simply scored lower (rejecting them left too few roles to apply to).
    core = _hits(S.get("core", []), full)
    core_title = _hits(S.get("core", []), t)
    core_occ = _occurrences(S.get("core", []), b)
    wrong = _hits(S.get("wrong_stack", []), full)
    wrong_occ = _occurrences(S.get("wrong_stack", []), b)
    if core_title or len(core) >= 2 or core_occ >= 3:
        tier = "js"
    elif core:
        tier = "mixed"
    else:
        tier = "other"
    if tier != "other" and not core_title and wrong_occ >= 3 and wrong_occ > 2 * core_occ:
        tier = "other"
    # Java rule (user, 2026-11-26): plain Java is fine, but NOT Java full-stack roles and NOT anything Spring-based.
    if re.search(r"java\s*(?:/|\+|&|and)?\s*(?:react\s*)?full[\s-]?stack|java\s*fullstack|full[\s-]?stack[\s\-–(:/,]*java\b|java\s*(?:/|\+|&|and)\s*react|react\s*(?:/|\+|&|and)\s*java", t):
        return False, "Java full-stack role (Spring-based — not your stack)", {}
    if re.search(r"\bjava\b", full) and re.search(r"\bspring\b|springboot|hibernate|\bj2ee\b|\bjsp\b|\bstruts\b|\bjpa\b", full):
        return False, "Java + Spring / Hibernate in the JD", {}
    if re.search(r"\bjava\b", t) and re.search(r"micro-?services?", full):
        return False, "Java microservices role (Spring-based)", {}
    # Roles built on frameworks outside the candidate's reach (Spring Boot, .NET, Django, PHP …) are rejected;
    # plain Java / Python roles are fine.
    blocked = list(S.get("blocked", []))
    if re.search(r"\bjava\b", full) and re.search(r"\bspring\b", full):
        blocked.append("spring")
    b_title = _hits(blocked, t)
    b_occ = _occurrences(blocked, b)
    _js_core = [c for c in S.get("core", []) if c not in ("python", "java", "fastapi")]
    core_occ = _occurrences(_js_core, b) + (core_occ - _occurrences(S.get("core", []), b) if False else 0)
    if b_title and b_occ >= core_occ and not (core_title and b_occ < 2):
        return False, f"Framework outside your stack in title ('{b_title[0]}')", {}
    if not core_title and ((b_occ >= 2 and b_occ >= core_occ) or (b_occ >= 1 and core_occ == 0)):
        return False, f"JD built on a stack outside yours ({', '.join(_hits(blocked, b)[:2])})", {}

    # ── experience: the JD's MINIMUM must be <= 3 ──
    stmts = parse_experience(jd or "")
    max_min = E.get("max_min_years", 3)
    real_min = [s for s in stmts if s[0] >= 1]
    if stmts:
        lowest = min(s[0] for s in stmts)
        if lowest > max_min:
            return False, f"Min experience {lowest}+ yrs (> {max_min})", {}
        if stmts[0][0] > max_min:
            return False, f"Experience stated first is {stmts[0][0]}+ yrs (> {max_min})", {}
        if all((s[1] is not None and s[1] <= 1) or (s[0] == 0 and s[1] is None) for s in stmts):
            return False, "Entry-level (0–1 yrs)", {}

    head = t + "\n" + b[:3000]
    fresher = [ph for ph in E.get("fresher_phrases", []) if ph.lower() in head]
    if fresher and not real_min:
        return False, f"Fresher/entry-level post ('{fresher[0]}')", {}

    # ── salary: reject only when an INR figure is disclosed and below the floor ──
    lpa, snip = parse_salary_lpa((salary or "") + " \n" + (jd or ""))
    floor = rules.get("compensation", {}).get("india_min_lpa", 15)
    if lpa is not None and lpa < floor:
        return False, f"Salary {lpa:g} LPA < {floor} LPA", {}

    return True, "ok", {
        "exp": _fmt_exp(stmts), "salary": snip, "core": core, "tier": tier, "wrong": wrong,
        "stmts": stmts, "lpa": lpa,
    }


def _score(title: str, body: str, rules: dict, info: dict | None = None) -> int:
    s = 5
    t, b = (title or "").lower(), (body or "").lower()
    if re.search(r"full[\s-]?stack|mern", t):
        s += 2
    elif re.search(r"backend|back-end|frontend|front-end|react|node|next", t):
        s += 1
    core = (info or {}).get("core") or stack_hits(t + " " + b, rules)
    s += min(len(core) // 2, 2)
    if info and info.get("stmts") and any(lo <= 3 <= (hi or lo + 2) for lo, hi in info["stmts"]):
        s += 1
    tier = (info or {}).get("tier", "js")
    s += {"js": 1, "mixed": -1, "other": -3}.get(tier, 0)
    return max(3, min(s, 10))


def _guess_cat(title: str) -> str:
    t = title.lower()
    if re.search(r"full[\s-]?stack|mern", t):
        return "Full Stack"
    if any(k in t for k in ["frontend", "front-end", "front end", "react", "vue", "ui "]):
        return "Frontend"
    if any(k in t for k in ["backend", "back-end", "back end", "node", "api ", "server"]):
        return "Backend"
    return "Full Stack"


def _extract_skills(text: str, rules: dict) -> str:
    S = rules.get("stack", {})
    found = _hits(S.get("core", []) + S.get("secondary", []), text.lower())
    return ", ".join(found[:8])


_POSTER_KW_RE = re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(k) for k in POSTER_ROLE_KEYWORDS) + r")(?![a-z0-9])", re.I)


def _is_valid_poster(text: str) -> bool:
    return bool(_POSTER_KW_RE.search(text or ""))


# ─────────────────────────── DB insert ──────────────────────────────────────

def insert_job(conn: sqlite3.Connection, job: dict) -> bool:
    """A job is 'already seen' only if its EXACT apply URL was shown before (persisted forever)."""
    nu = norm_url(job["direct_apply_url"])
    h = make_hash(nu)
    if conn.execute("SELECT 1 FROM seen_jobs WHERE url=?", (nu,)).fetchone():
        return False
    if conn.execute("SELECT id FROM jobs WHERE job_hash=?", (h,)).fetchone():
        return False
    conn.execute("""
        INSERT INTO jobs (
            job_hash,company,title,direct_apply_url,discovery_source,
            role_category,location,remote_eligible_india,experience_req,
            salary,posted_date,match_score,match_reason,
            founder_name,founder_linkedin,founder_email,hiring_post_url,
            status,notes,contacts,link_type,board_url,link_status,locked,apply_email,checked_at,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (h, job["company"], job["title"], job["direct_apply_url"],
         job.get("discovery_source", ""),
         job.get("role_category", "Full Stack"),
         job.get("location", "") or "Remote / not stated",
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
         "new", job.get("notes", ""), job.get("contacts", "[]"),
         job.get("link_type", "direct"), job.get("board_url", ""), job.get("link_status", "active"),
         int(job.get("locked", 0)), job.get("apply_email", ""), now_utc(), now_utc()))
    conn.execute("INSERT OR IGNORE INTO seen_jobs (url, first_seen) VALUES (?,?)", (nu, now_utc()))
    conn.commit()
    return True


_ROLE_PHRASE = re.compile(r"\b((?:senior |junior |sr\.? |jr\.? )?(?:react(?:\.?js)?|node(?:\.?js)?|next\.?js|mern|mean|full[\s-]?stack|front[\s-]?end|"
                          r"back[\s-]?end|javascript|typescript|software|web|sde|founding|product|ui)\s*(?:-|/|\s)?\s*"
                          r"(?:developer|engineer|dev|sde|intern)s?)\b", re.I)
_MAIL_ANY = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.[a-z]{2,}", re.I)


def _post_words(text: str) -> list[str]:
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = re.sub(r"https?://\S+|#\w+|@\w+|[^a-z0-9 ]+", " ", t)
    return t.split()[:160]


def post_dedup_key(post: dict) -> str:
    """Same opening shared by many people → one key: the apply e-mail + the role, else the post's core wording."""
    text = unicodedata.normalize("NFKC", post.get("post_text") or "").lower()
    role = _ROLE_PHRASE.search(text)
    role_k = re.sub(r"[^a-z]+", "", role.group(1)) if role else ""
    mails = [m.group(0) for m in _MAIL_ANY.finditer(text) if not any(b in m.group(0) for b in _BAD_MAIL)]
    if mails:
        return f"mail:{mails[0]}|{role_k}"
    words = _post_words(text)
    return "txt:" + hashlib.md5(" ".join(words[:60]).encode()).hexdigest() if words else ""


def _shingles(words: list[str], n: int = 5) -> set:
    return {" ".join(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def insert_post(conn: sqlite3.Connection, post: dict) -> bool:
    """Skip exact repeats (same URL), the same opening reposted by others (same apply e-mail + role),
    and near-copies (≥ 60% identical wording to a post from the last 45 days)."""
    h = make_hash(post.get("post_url", ""), post.get("post_text", "")[:80])
    if conn.execute("SELECT id FROM posts WHERE post_hash=?", (h,)).fetchone():
        return False
    key = post_dedup_key(post)
    if key and conn.execute("SELECT id FROM posts WHERE dedup_key=?", (key,)).fetchone():
        return False
    words = _post_words(post.get("post_text", ""))
    sh = _shingles(words)
    if len(sh) >= 8:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        for r in conn.execute("SELECT norm_text FROM posts WHERE created_at >= ? AND norm_text != ''", (cutoff,)):
            other = _shingles(r["norm_text"].split())
            if other and len(sh & other) / max(1, min(len(sh), len(other))) >= 0.6:
                return False
    conn.execute("""
        INSERT INTO posts (
            post_hash,source,poster_name,poster_role,poster_url,
            post_url,post_text,skills_found,location,posted_at,
            status,created_at,dedup_key,norm_text
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (h, post.get("source",""), post.get("poster_name",""),
         post.get("poster_role",""), post.get("poster_url",""),
         post.get("post_url",""), post.get("post_text","")[:2000],
         post.get("skills_found",""), post.get("location",""),
         post.get("posted_at",""), "new", now_utc(), key, " ".join(words)))
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
        "location": extra.get("location", ""),
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
        "jd_api": extra.get("jd_api"),
        "jd_text": extra.get("jd_text", ""),
        "need_fetch": extra.get("need_fetch", False),
        "title_from_page": extra.get("title_from_page", False),
        "apply_hint": extra.get("apply_hint"),
        "board": extra.get("board", ""),
        "jd_wd": extra.get("jd_wd"),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  REMOTE-JOB-BOARD CRAWLERS
#  Flow for every board:  listing/feed → job page → still active? → real apply link
#   • real company/ATS link found   → saved as a DIRECT link
#   • only the board page possible  → saved as a BOARD link (flagged, may need login)
#   • page is 404/closed            → dropped
# ═══════════════════════════════════════════════════════════════════════════

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_MD_LINK = re.compile(r"\[([^\]\n]{1,300})\]\((https?://[^)\s]+)\)")
_A_RE = re.compile(r"<a\b[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_LOCK_PHRASES = ["unlock all jobs", "unlock this job", "upgrade to apply", "sign in to apply",
                 "log in to apply", "login to apply", "create an account to apply", "members only",
                 "premium members", "join to apply", "subscribe to apply", "register to apply",
                 "sign up to apply", "join remotive", "to view the apply"]
_CHALLENGE = ["just a moment", "attention required", "verify you are human",
              "enable javascript and cookies", "checking your browser"]


def md_links(md: str) -> list[tuple[str, str]]:
    return [(t.strip(), u) for t, u in _MD_LINK.findall(md or "")]


def _is_junk(u: str) -> bool:
    h = _netloc(u)
    return h in JUNK_LINK_HOSTS or any(h.endswith("." + j) for j in JUNK_LINK_HOSTS)


def md_apply_link(md: str, own_url: str) -> str | None:
    own = _netloc(own_url)
    for text, u in md_links(md):
        if re.search(r"\bapply\b", text, re.I) and _netloc(u) != own and not _is_junk(u) and not is_aggregator(u):
            return u
    return None


def html_apply_link(raw: str, base_url: str) -> str | None:
    own = _netloc(base_url)
    for href, inner in _A_RE.findall(raw or ""):
        if not re.search(r"\bapply\b", re.sub(r"<[^>]+>", " ", inner), re.I):
            continue
        u = urllib.parse.urljoin(base_url, htmllib.unescape(href))
        if u.startswith("http") and _netloc(u) != own and not _is_junk(u) and not is_aggregator(u):
            return u
    return None


def pick_apply_from_links(links: list[str], own_url: str = "") -> str | None:
    own = _netloc(own_url)
    clean = [l for l in links if isinstance(l, str) and l.startswith("http") and _netloc(l) != own
             and not is_aggregator(l) and not _is_junk(l)]
    for l in clean:
        if is_ats(l) and not looks_like_listing(l):
            return l
    for l in clean:
        if re.search(r"/apply\b|apply[-_]?now|/applications?/new", l.lower()) and not looks_like_listing(l):
            return l
    return None


def check_page(url: str, timeout: int = 20) -> tuple[str, bool, str, str, str | None]:
    """Open a job page ourselves → (status, locked, text, final_url, apply_link).
    status: 'active' | 'dead' (404/410/closed text) | 'unverified' (blocked / challenge / error)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "text/html,*/*",
                                                   "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            final = r.geturl()
            raw = r.read(1_800_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return ("dead" if e.code in (404, 410) else "unverified"), False, "", url, None
    except Exception:
        return "unverified", False, "", url, None
    text = _html_to_text(raw)
    low = text.lower()
    if len(text) < 1500 and any(c in low[:2500] for c in _CHALLENGE):
        return "unverified", False, text, final, None
    req_path, fin_path = urllib.parse.urlparse(url).path.strip("/"), urllib.parse.urlparse(final).path.strip("/")
    if req_path and (fin_path == "" or (looks_like_listing(final) and not looks_like_listing(url))):
        return "dead", False, text, final, None            # redirected to the careers root / listing → posting is gone
    if any(p in low[:4000] for p in _DEAD_PHRASES):
        return "dead", False, text, final, None
    locked = any(p in low for p in _LOCK_PHRASES)
    return "active", locked, text, final, html_apply_link(raw, final)


_LOC_RE = re.compile(r"(?:^|\n|\s)Location[\s:\-]*([A-Za-z][^\n]{1,140}?)"
                     r"(?=\s{2,}|Work type|Work Format|Job Type|Type\b|Posted|Salary|Level|Experience|\n|$)")


def extract_location(text: str) -> str:
    m = _LOC_RE.search((text or "")[:6000])
    loc = m.group(1).strip(" |-:") if m else ""
    return loc if 2 <= len(loc) <= 70 else ""


_CHROME = next((c for c in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
                            "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser") if os.path.exists(c)), "")
_CHROME_SEM = threading.Semaphore(int(os.environ.get("CHROME_CONCURRENCY", "3")))


def chrome_render(url: str, budget_ms: int = 9000) -> str:
    """Render a JavaScript page in the machine's own headless Chrome (Firecrawl-local only does plain HTTP fetches)."""
    if not _CHROME:
        return ""
    import subprocess
    with _CHROME_SEM:
        try:
            r = subprocess.run([_CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars",
                                f"--virtual-time-budget={budget_ms}", "--dump-dom", url],
                               capture_output=True, timeout=45)
            return r.stdout.decode("utf-8", "replace")
        except Exception:
            return ""


try:                                   # optional: pip install playwright && playwright install chromium
    from playwright.sync_api import sync_playwright as _sync_playwright
except Exception:                      # not installed → the plain headless-Chrome subprocess is used instead
    _sync_playwright = None
_PW: dict = {"pw": None, "browser": None}
_PW_EXEC = ThreadPoolExecutor(1)      # Playwright's sync API is bound to the thread that started it


def _pw_task(url: str, wait_ms: int) -> str:
    try:
        if _PW["browser"] is None:
            _PW["pw"] = _sync_playwright().start()
            _PW["browser"] = _PW["pw"].chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = _PW["browser"].new_context(user_agent=BROWSER_UA, locale="en-IN", viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        page.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media", "font") else r.continue_())
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=wait_ms)
        except Exception:
            pass
        html = page.content()
        ctx.close()
        return html
    except Exception:
        return ""


def render_page(url: str, budget_ms: int = 9000) -> str:
    """Rendered HTML of a JavaScript page: Playwright when installed (waits for the network to settle, blocks images),
    otherwise the machine's headless Chrome."""
    if _sync_playwright is not None:
        try:
            html = _PW_EXEC.submit(_pw_task, url, budget_ms).result(timeout=70)
            if html:
                return html
        except Exception:
            pass
    return chrome_render(url, budget_ms)


def _fetch_page(url: str) -> tuple[str, list[str]]:
    """Firecrawl-local render (JS pages) with plain-HTTP fallback → (markdown/text, links)."""
    sc = fc_scrape(url)
    md = (sc.get("markdown") or "") if isinstance(sc, dict) else ""
    links = [l for l in (sc.get("links") or []) if isinstance(l, str)] if isinstance(sc, dict) else []
    if len(md) < 400:
        raw = http_get_text(url, timeout=25)
        txt = _html_to_text(raw) if raw else ""
        if len(txt) > len(md):
            md = txt
    if len(md) < 400:                                   # JS-rendered page (Ashby, Workday, SPA…) → real browser
        dom = render_page(url)
        txt = _html_to_text(dom) if dom else ""
        if len(txt) > len(md) and not any(c in txt.lower()[:300] for c in _CHALLENGE):
            md = txt
            links = links or re.findall(r'href="(https?://[^"]+)"', dom)
    return md[:30000], links


def _board_ok(title: str, company: str, loc: str, rules: dict) -> bool:
    if not title_relevant(title):
        return False
    ok, _ = passes_hard_filters(title, company, "", "", rules)
    return ok and location_ok(loc, rules)[0]


def _fresh_enough(date_s: str, rules: dict) -> bool:
    age = days_ago(date_s) if date_s else None
    return age is None or age <= rules.get("freshness", {}).get("max_age_days", 40)


def _multi_get(urls: list[str], workers: int = 8) -> list[str]:
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(lambda u: http_get_text(u, timeout=30), urls))


# ── 1. RemoteOK (API has the full JD) ────────────────────────────────────────
def collect_remoteok(rules: dict) -> list[dict]:
    print("[remoteok] fetching API (all + per-tag feeds)...")
    tags = ["", "react", "javascript", "typescript", "node", "full-stack", "frontend", "backend", "nextjs", "web",
            "engineer", "developer", "software", "api", "startup", "front-end", "back-end", "fullstack", "js", "mern"]
    feeds = _multi_get([f"https://remoteok.com/api{'?tag=' + g if g else ''}" for g in tags], 4)
    out, seen = [], set()
    for text in feeds:
        try:
            raw = json.loads(text) if text else []
        except ValueError:
            continue
        for it in raw if isinstance(raw, list) else []:
            if not isinstance(it, dict) or not it.get("position") or it.get("id") in seen:
                continue
            seen.add(it.get("id"))
            title, company, loc = it["position"], it.get("company", ""), it.get("location", "") or "Worldwide"
            date_s = (it.get("date") or "")[:10]
            if not _fresh_enough(date_s, rules) or not _board_ok(title, company, loc, rules):
                continue
            sal = ""
            if it.get("salary_min"):
                sal = f"${int(it['salary_min']):,}–${int(it.get('salary_max') or it['salary_min']):,}"
            out.append(_job(company, title, it.get("url", ""), "remoteok", rules, location=loc, date=date_s,
                            salary=sal, jd_text=_html_to_text(it.get("description", "")),
                            reason="RemoteOK", board="RemoteOK", apply_hint=None))
    print(f"  → {len(out)} candidates from {len(seen)} listings")
    return out


# ── 2. Remotive ──────────────────────────────────────────────────────────────
def collect_remotive(rules: dict) -> list[dict]:
    print("[remotive] fetching API...")
    out, seen = [], set()
    for q in ("category=software-dev&limit=300", "search=react&limit=100", "search=node&limit=100",
              "search=typescript&limit=100", "search=full%20stack&limit=100", "search=javascript&limit=100",
              "search=frontend&limit=100", "search=backend&limit=100", "search=software%20engineer&limit=100",
              "search=developer&limit=100", "search=next.js&limit=100", "search=engineer&limit=100", "search=web&limit=100",
              "category=all-others&limit=100"):
        raw = http_get(f"https://remotive.com/api/remote-jobs?{q}", timeout=30)
        for it in raw.get("jobs", []) if isinstance(raw, dict) else []:
            url = it.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            title, company = it.get("title", ""), it.get("company_name", "")
            loc, date_s = it.get("candidate_required_location", "") or "Worldwide", (it.get("publication_date") or "")[:10]
            if not _fresh_enough(date_s, rules) or not _board_ok(title, company, loc, rules):
                continue
            out.append(_job(company, title, url, "remotive", rules, location=loc, date=date_s,
                            salary=it.get("salary", ""), jd_text=_html_to_text(it.get("description", "")),
                            reason="Remotive", board="Remotive"))
    print(f"  → {len(out)} candidates")
    return out


# ── 3. Working Nomads (/job/go/<id> redirects to the real apply page) ────────
def collect_working_nomads(rules: dict) -> list[dict]:
    print("[workingnomads] fetching API...")
    raw = http_get("https://www.workingnomads.com/api/exposed_jobs/?category=development", timeout=30)
    out = []
    for it in raw if isinstance(raw, list) else []:
        title, company = it.get("title", ""), it.get("company_name", "")
        loc, date_s = it.get("location", "") or "Worldwide", (it.get("pub_date") or "")[:10]
        if not it.get("url") or not _fresh_enough(date_s, rules) or not _board_ok(title, company, loc, rules):
            continue
        out.append(_job(company, title, it["url"], "workingnomads", rules, location=loc, date=date_s,
                        jd_text=_html_to_text(it.get("description", "")), reason="Working Nomads",
                        board="Working Nomads"))
    print(f"  → {len(out)} candidates")
    return out


# ── 4. Himalayas ─────────────────────────────────────────────────────────────
def collect_himalayas(rules: dict) -> list[dict]:
    print("[himalayas] searching (react / node / typescript / ...) ...")
    qs = ["react", "node.js", "typescript", "javascript", "full stack", "next.js", "frontend", "backend", "software engineer",
          "web developer", "mern", "nestjs"]
    urls = [f"https://himalayas.app/jobs/api/search?q={urllib.parse.quote(q)}&page={pg}" for q in qs for pg in (1, 2, 3, 4)]
    out, seen = [], set()
    for text in _multi_get(urls, 4):
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            continue
        for it in data.get("jobs", []):
            url = it.get("guid") or it.get("applicationLink", "")
            if not url or url in seen:
                continue
            seen.add(url)
            title, company = it.get("title", ""), it.get("companyName", "")
            locs = it.get("locationRestrictions") or []
            loc = ", ".join(locs) if locs else "Worldwide"
            try:
                date_s = datetime.fromtimestamp(int(it.get("pubDate")), timezone.utc).strftime("%Y-%m-%d")
            except Exception:
                date_s = ""
            if not _fresh_enough(date_s, rules) or not _board_ok(title, company, loc, rules):
                continue
            sal = ""
            if it.get("minSalary"):
                sal = f"{it.get('currency') or ''} {it['minSalary']}–{it.get('maxSalary')}".strip()
            out.append(_job(company, title, url.split("?")[0], "himalayas", rules, location=loc, date=date_s, salary=sal,
                            jd_text=_html_to_text(it.get("description", "")), reason="Himalayas",
                            board="Himalayas"))
    print(f"  → {len(out)} candidates from {len(seen)} listings")
    return out


# ── 5. Jobicy ────────────────────────────────────────────────────────────────
def collect_jobicy(rules: dict) -> list[dict]:
    print("[jobicy] fetching API (rate-limited → sequential)...")
    urls = [f"https://jobicy.com/api/v2/remote-jobs?count=50&tag={t}{g}"
            for t in ("javascript", "typescript", "react", "node", "full-stack")
            for g in ("", "&geo=apac")]
    out, seen = [], set()
    for u in urls:
        text = http_get_text(u, timeout=25)
        time.sleep(1.5)
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            continue
        for it in data.get("jobs", []):
            url = it.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            title, company = it.get("jobTitle", ""), it.get("companyName", "")
            loc, date_s = it.get("jobGeo", "") or "Worldwide", (it.get("pubDate") or "")[:10]
            if not _fresh_enough(date_s, rules) or not _board_ok(title, company, loc, rules):
                continue
            out.append(_job(company, title, url, "jobicy", rules, location=loc, date=date_s,
                            jd_text=_html_to_text(it.get("jobDescription", "")), reason="Jobicy",
                            board="Jobicy"))
    print(f"  → {len(out)} candidates")
    return out


# ── 6. Arbeitnow ─────────────────────────────────────────────────────────────
def collect_arbeitnow(rules: dict) -> list[dict]:
    print("[arbeitnow] fetching API pages...")
    out = []
    for text in _multi_get([f"https://www.arbeitnow.com/api/job-board-api?page={p}" for p in (1, 2, 3)], 3):
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            continue
        for it in data.get("data", []):
            title, company, url = it.get("title", ""), it.get("company_name", ""), it.get("url", "")
            loc = it.get("location", "") or ""
            if it.get("remote") and "remote" not in loc.lower():
                loc = (loc + " (remote)").strip()
            epoch = it.get("created_at", 0)
            date_s = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d") if epoch else ""
            if not url or not _fresh_enough(date_s, rules) or not _board_ok(title, company, loc, rules):
                continue
            out.append(_job(company, title, url, "arbeitnow", rules, location=loc, date=date_s,
                            jd_text=_html_to_text(it.get("description", "")), reason="Arbeitnow",
                            board="Arbeitnow"))
    print(f"  → {len(out)} candidates")
    return out


# ── 7. We Work Remotely (RSS carries the full JD; the page itself is login/CF-locked) ──
def collect_weworkremotely(rules: dict) -> list[dict]:
    print("[weworkremotely] fetching RSS feeds...")
    feeds = ["remote-full-stack-programming-jobs", "remote-front-end-programming-jobs",
             "remote-back-end-programming-jobs", "remote-programming-jobs", "all-other-remote-jobs"]
    from email.utils import parsedate_to_datetime
    out, seen = [], set()
    for text in _multi_get([f"https://weworkremotely.com/categories/{f}.rss" for f in feeds]
                           + ["https://weworkremotely.com/remote-jobs.rss"], 6):
        if not text:
            continue
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            continue
        for item in root.findall(".//item"):
            raw_title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not link or link in seen:
                continue
            seen.add(link)
            company, _, title = raw_title.partition(": ")
            if not title:
                company, title = "", raw_title
            loc = (item.findtext("region") or "").strip() or "Worldwide"
            try:
                date_s = parsedate_to_datetime(item.findtext("pubDate") or "").strftime("%Y-%m-%d")
            except Exception:
                date_s = ""
            if not _fresh_enough(date_s, rules) or not _board_ok(title, company, loc, rules):
                continue
            out.append(_job(company or "Unknown", title.strip(), link, "weworkremotely", rules, location=loc,
                            date=date_s, jd_text=_html_to_text(item.findtext("description") or ""),
                            reason="We Work Remotely", board="We Work Remotely"))
    print(f"  → {len(out)} candidates")
    return out


# ── 8. HuntYourTribe — India startup/SMB roles straight from career pages ────
_HYT_ITEM = re.compile(
    r'"@type":"ListItem","name":"([^"]+)","url":"(https://huntyourtribe\.com/jobs/[^"]+)",'
    r'"item":\{"@type":"WebPage","name":"([^"]*)","url":"[^"]*","about":"([^"]*)"')


def collect_huntyourtribe(rules: dict) -> list[dict]:
    pages = int(os.environ.get("HYT_PAGES", "40"))
    print(f"[huntyourtribe] crawling {pages} listing pages...")
    out, seen = [], set()
    for html in _multi_get([f"https://huntyourtribe.com/jobs?page={p}" for p in range(1, pages + 1)], 8):
        for title, url, full, loc in _HYT_ITEM.findall(html or ""):
            if url in seen:
                continue
            seen.add(url)
            title, loc = htmllib.unescape(title), htmllib.unescape(loc)
            company = htmllib.unescape(full.rsplit(" at ", 1)[-1]) if " at " in full else ""
            if not _board_ok(title, company, loc or "India", rules):
                continue
            out.append(_job(company or "Unknown", title, url, "huntyourtribe", rules, location=loc or "India",
                            need_fetch=True, reason="HuntYourTribe", board="HuntYourTribe"))
    print(f"  → {len(seen)} listed, {len(out)} candidates")
    return out


# ── 9. UrbanoDX / RoleFinder — role pages with "direct from company" links ───
def collect_urbanodx(rules: dict) -> list[dict]:
    roles = ["full-stack-developer", "frontend-developer", "backend-developer", "software-engineer",
             "react-developer", "node-js-developer", "javascript-developer"]
    pages = int(os.environ.get("URBANO_PAGES", "3"))
    print(f"[urbanodx] crawling {len(roles)} role pages × {pages}...")
    targets = [f"https://jobs.urbanodx.com/jobs/{r}" + (f"?page={p}" if p > 1 else "")
               for r in roles for p in range(1, pages + 1)] + ["https://jobs.urbanodx.com/jobs/remote"]
    out, seen = [], set()

    def crawl(u: str) -> list[tuple[str, str]]:
        sc = fc_scrape(u)
        return md_links((sc.get("markdown") or "") if isinstance(sc, dict) else "")

    with ThreadPoolExecutor(4) as ex:
        results = list(ex.map(crawl, targets))
    for links in results:
        for text, url in links:
            if "/job/" not in url or "urbanodx.com" not in url or url in seen:
                continue
            seen.add(url)
            if not _board_ok(text, "", "", rules):
                continue
            out.append(_job("", text[:140], url, "urbanodx", rules, need_fetch=True, title_from_page=True,
                            reason="RoleFinder (UrbanoDX)", board="RoleFinder"))
    print(f"  → {len(seen)} listed, {len(out)} candidates")
    return out


# ── 10. JSGuruJobs — JavaScript-only jobs ────────────────────────────────────
def collect_jsguru(rules: dict) -> list[dict]:
    pages = int(os.environ.get("JSGURU_PAGES", "8"))
    print(f"[jsgurujobs] crawling {pages} pages...")
    out, seen = [], set()

    def crawl(p: int) -> list[tuple[str, str]]:
        sc = fc_scrape(f"https://jsgurujobs.com/jobs/?page={p}")
        return md_links((sc.get("markdown") or "") if isinstance(sc, dict) else "")

    with ThreadPoolExecutor(4) as ex:
        results = list(ex.map(crawl, range(1, pages + 1)))
    for links in results:
        for text, url in links:
            if not re.fullmatch(r"https://jsgurujobs\.com/jobs/\d+", url) or url in seen:
                continue
            seen.add(url)
            if not _board_ok(text, "", "", rules):
                continue
            out.append(_job("", text[:140], url, "jsgurujobs", rules, need_fetch=True, title_from_page=True,
                            reason="JSGuruJobs", board="JSGuruJobs"))
    print(f"  → {len(seen)} listed, {len(out)} candidates")
    return out


# ── 11. Matcha — under-the-radar remote startup roles ────────────────────────
def collect_matcha(rules: dict) -> list[dict]:
    """Matcha only exposes a few job pages publicly (the rest are email-gated), so read each public page."""
    print("[matcha] crawling public job pages...")
    out, seen = [], set()
    for pg in ("jobs/ww", "jobs/software-engineer/ww", "jobs/in", "jobs/software-engineer/in"):
        sc = fc_scrape(f"https://matcha.fm/{pg}")
        if not isinstance(sc, dict):
            continue
        for u in sc.get("links") or []:
            if isinstance(u, str) and re.fullmatch(r"https://matcha\.fm/job/[0-9a-f-]{36}", u) and u not in seen:
                seen.add(u)
                out.append(_job("", "Software Engineer", u, "matcha", rules, need_fetch=True,
                                title_from_page=True, reason="Matcha", board="Matcha"))
    print(f"  → {len(out)} public job pages")
    return out


# ── 12. Company career pages (established-remote list + JobLeLo companies) ───
_ER_README = "https://raw.githubusercontent.com/yanirs/established-remote/master/README.md"
CAREERS_PER_SCAN = int(os.environ.get("CAREERS_PER_SCAN", "40"))


def _seed_careers_pages(conn: sqlite3.Connection) -> None:
    readme = http_get_text(_ER_README, timeout=30)
    urls = re.findall(r"\[Jobs\]\((https?://[^)\s]+)\)", readme)
    sc = fc_scrape("https://www.joblelo.online/companies")
    md = (sc.get("markdown") or "") if isinstance(sc, dict) else ""
    jl = [u for _, u in md_links(md) if re.fullmatch(r"https://www\.joblelo\.online/companies/[\w-]+", u)]
    for u in urls:
        conn.execute("INSERT OR IGNORE INTO careers_pages (url, source) VALUES (?,?)", (u, "established-remote"))
    for u in jl:
        conn.execute("INSERT OR IGNORE INTO careers_pages (url, source) VALUES (?,?)", (u, "joblelo"))
    conn.commit()


def collect_company_careers(rules: dict) -> list[dict]:
    """Rotate through company career pages: register their ATS boards (so ATS polling grows) and
    pick up dev-role links straight from the page."""
    conn = get_db()
    if not conn.execute("SELECT 1 FROM careers_pages LIMIT 1").fetchone():
        _seed_careers_pages(conn)
    rows = conn.execute("SELECT url, source FROM careers_pages ORDER BY COALESCE(last_checked,'') ASC LIMIT ?",
                        (CAREERS_PER_SCAN,)).fetchall()
    print(f"[careers] checking {len(rows)} company career pages...")
    out, registered = [], 0

    def work(row):
        url = row["url"]
        sc = fc_scrape(url)
        if not isinstance(sc, dict):
            return row, "", []
        md = sc.get("markdown") or ""
        links = [l for l in (sc.get("links") or []) if isinstance(l, str)]
        if len(md) < 300 and len(links) < 5:                 # JS-rendered careers page → real headless browser
            dom = render_page(url, 10000)
            if dom and not any(c in dom.lower()[:1500] for c in _CHALLENGE):
                anchors = [(re.sub(r"<[^>]+>|\s+", " ", a).strip(), urllib.parse.urljoin(url, h))
                           for h, a in re.findall(r'<a\b[^>]*?href="([^"#][^"]*)"[^>]*>(.*?)</a>', dom, re.S | re.I)]
                md = "\n".join(f"[{a[:80]}]({h})" for a, h in anchors if a and h.startswith("http"))
                links = [h for _, h in anchors if h.startswith("http")]
        if row["source"] == "joblelo":                  # company page → its official careers page
            careers = next((l for l in links if "joblelo" not in l and not _is_junk(l)
                            and "linkedin.com" not in l), "")
            return row, md, [careers] if careers else []
        return row, md, links

    with ThreadPoolExecutor(4) as ex:
        results = list(ex.map(work, rows))
    for row, md, links in results:
        conn.execute("UPDATE careers_pages SET last_checked=? WHERE url=?", (now_utc(), row["url"]))
        for l in links:
            if ats_board_from_url(l):
                register_board(conn, l, f"careers:{row['source']}")
                registered += 1
        for text, u in md_links(md):
            if is_aggregator(u) or _is_junk(u) or not looks_like_job_page(u):
                continue
            if re.search(r"\.(jpe?g|png|gif|svg|webp|pdf|css|js)(\?|$)", u.lower()) or len(text) > 90:
                continue
            if not (ats_board_from_url(u) or re.search(r"job|career|position|opening|vacanc|apply|gh_jid|/roles?/", u.lower())):
                continue
            if ats_board_from_url(u) is None and _netloc(u) != _netloc(row["url"]):
                continue
            if not _board_ok(text, "", "", rules):
                continue
            out.append(_job(company_from_url(u), text[:140], u, f"careers:{row['source']}", rules,
                            need_fetch=True, reason="Company careers page"))
    conn.commit()
    conn.close()
    print(f"  → {registered} ATS boards registered, {len(out)} candidate roles")
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  HIRING-POST SOURCES  (founders / recruiters talking directly)
# ═══════════════════════════════════════════════════════════════════════════

_SHEET_ID = "1nou4l-qvShy0FZAaEgvc5-RQdHdDVIBGQPYBlIxDwfI"
_SHEET_GID = "47994240"


def collect_hiring_sheet(rules: dict) -> list[dict]:
    """TextBackdoor sheet: public HN hiring posts with the hiring email."""
    import csv
    import io
    print("[sheet] TextBackdoor remote hiring contacts...")
    text = http_get_text(f"https://docs.google.com/spreadsheets/d/{_SHEET_ID}/export?format=csv&gid={_SHEET_GID}", 40)
    out = []
    if not text:
        return out
    rows = list(csv.reader(io.StringIO(text)))
    hdr_i = next((i for i, r in enumerate(rows) if r and r[0].strip().lower() == "company"), None)
    if hdr_i is None:
        return out
    hdr = [h.strip().lower() for h in rows[hdr_i]]
    col = {n: next((i for i, h in enumerate(hdr) if n in h), None)
           for n in ("company", "email", "headline", "location", "keywords", "profile", "post date", "original", "pay")}
    max_days = rules.get("freshness", {}).get("max_age_days", 40) * 3
    for r in rows[hdr_i + 1:]:
        def g(k):
            i = col.get(k)
            return r[i].strip() if i is not None and i < len(r) else ""
        company, email, headline, loc = g("company"), g("email"), g("headline"), g("location")
        blob = f"{headline} {g('keywords')}"
        if not company or not re.search(r"engineer|developer|full[\s-]?stack|front[\s-]?end|back[\s-]?end|software|react|node|typescript|javascript", blob, re.I):
            continue
        if not location_ok(loc, rules)[0] or any(p in blob.lower() for p in rules.get("locations", {}).get("reject_region_phrases", [])):
            continue
        if re.search(r"\b(senior|sr\.?|staff|principal|lead|head of|director|manager|vp)\b", headline.lower().split("|")[1] if "|" in headline else headline.lower()):
            continue
        if is_staffing_text(blob, rules) or is_staffing_company(company, rules):
            continue
        age = days_ago(g("post date"))
        if age is not None and age > max_days:
            continue
        out.append({
            "source": "Hiring sheet (HN posts)", "poster_name": company,
            "poster_role": f"Hiring contact · {email}" if email else "Hiring post",
            "poster_url": g("profile") or g("original"), "post_url": g("original") or g("profile"),
            "post_text": f"{headline}\n\nEmail: {email}\nLocation: {loc}\nPay: {g('pay')}",
            "skills_found": _extract_skills(blob, rules), "location": loc, "posted_at": g("post date"),
        })
    print(f"  → {len(out)} relevant hiring contacts")
    return out


def collect_tal_club(rules: dict) -> list[dict]:
    """jobsinthewild.tal.club — LinkedIn/X hiring posts straight from founders & in-house teams.
    Page layout per post:  [author image/name/headline/time] [↗](/j/ID)  ### title + text + tags  [message boss ↗](post)"""
    print("[tal.club] jobs-in-the-wild posts...")
    out, seen = [], set()
    arrow = re.compile(r"\[↗\]\(https://jobsinthewild\.tal\.club/j/\d+\)")

    def clean_lines(t: str) -> list[str]:
        t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
        return [l.strip() for l in t.splitlines() if l.strip()]

    for qs in ("?sort=new&role_family=engineering", "?sort=new&role_family=ai-ml",
               "?sort=new&remote_mode=remote", "?sort=new&seniority=junior", "?sort=new&seniority=mid"):
        sc = fc_scrape("https://jobsinthewild.tal.club/" + qs)
        md = (sc.get("markdown") or "") if isinstance(sc, dict) else ""
        parts = arrow.split(md)
        header = clean_lines(parts[0])[-3:] if parts else []
        for k in range(1, len(parts)):
            seg = parts[k]
            body, sep, rest = seg.partition("[message boss ↗](")
            if not sep:
                header = []
                continue
            post_url, _, after = rest.partition(")")
            next_header = clean_lines(after)[:3]
            cur_header, header = header, next_header
            if post_url in seen or not post_url.startswith("http"):
                continue
            seen.add(post_url)
            lines = clean_lines(body)
            tags = ""
            for i in range(len(lines) - 1, -1, -1):
                if re.fullmatch(r"(?:[A-Za-z\-]+ ?){2,6}", lines[i]) and any(c.isdigit() for c in "".join(lines[i + 1:i + 3])):
                    tags = lines[i]
                    lines = lines[:i]
                    break
            text = re.sub(r"https?://\S+", "", " ".join(l.lstrip("# ") for l in lines))
            text = re.sub(r"\s+", " ", text).strip()
            name = cur_header[0][:80] if cur_header else ""
            role = cur_header[1][:120] if len(cur_header) > 1 else ""
            blob = (text + " " + tags).lower()
            fam = tags.lower().split()[0] if tags else ""
            if fam and fam not in ("engineering", "ai-ml"):
                continue                                   # sales / marketing / ops posts etc.
            if re.search(r"\b(senior|staff|lead|head|principal|intern)\b", tags.lower()) and not re.search(r"\b(junior|mid)\b", tags.lower()):
                continue
            if not re.search(r"software|developer|full[\s-]?stack|front[\s-]?end|back[\s-]?end|react|node|typescript|javascript|\bsde\b|founding engineer|\bswe\b|ai engineer|web", blob):
                continue
            if is_staffing_text(blob, rules) or any(p in blob for p in rules.get("locations", {}).get("reject_region_phrases", [])):
                continue
            out.append({
                "source": "LinkedIn" if "linkedin.com" in post_url else "X/Twitter" if ("x.com" in post_url or "twitter.com" in post_url) else "tal.club",
                "poster_name": name, "poster_role": role or "Founder / hiring team (tal.club)",
                "poster_url": post_url, "post_url": post_url, "post_text": f"[{tags}] {text}"[:1800],
                "skills_found": _extract_skills(blob, rules), "location": "India (Bangalore-focused)",
                "posted_at": "",
            })
    print(f"  → {len(out)} hiring posts")
    return out


# ── 9. Firecrawl search — global + India queries ─────────────────────────────
# These use Firecrawl /v2/search which acts as a Bing/Google wrapper.
# If FIRECRAWL_URL is pointing at firecrawl-local (port 3002), cloud search
# may not be available — the calls fail silently and are skipped.

GLOBAL_JOB_QUERIES = [
    # India-specific
    '"Full Stack" "TypeScript" "Node.js" remote India hiring',
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
            company = company_from_url(url)
            combined= (title + " " + markdown[:800]).lower()
            if not title_relevant(title, combined, rules):
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
            company  = company_from_url(link)
            combined = (title + " " + markdown[:1500]).lower()
            if not title_relevant(title, combined, rules):
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

# ── HN Who's Hiring (every top-level comment of the current monthly thread) ─────
def _hn_text(raw: str) -> str:
    t = re.sub(r"(?i)<a [^>]*href=\"([^\"]+)\"[^>]*>.*?</a>", lambda m: " " + htmllib.unescape(m.group(1)) + " ", raw or "")
    t = re.sub(r"(?i)<p>|<br\s*/?>", "\n", t)
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", t)).strip()


def collect_hn_hiring(rules: dict) -> list[dict]:
    print("[hn-hiring] reading the monthly 'Who is hiring?' thread ...")
    max_days = rules.get("freshness", {}).get("max_age_days", 40)
    top = http_get("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=6", timeout=20)
    threads = [h for h in (top.get("hits", []) if isinstance(top, dict) else [])
               if "who is hiring" in (h.get("title") or "").lower()][:1]
    out: list[dict] = []
    for th in threads:
        tid = th.get("objectID", "")
        for page in range(0, 6):
            data = http_get(f"https://hn.algolia.com/api/v1/search_by_date?tags=comment,story_{tid}"
                            f"&hitsPerPage=500&page={page}", timeout=30)
            hits = data.get("hits", []) if isinstance(data, dict) else []
            for c in hits:
                if str(c.get("parent_id")) != str(tid):          # top-level = one company's job post
                    continue
                text = _hn_text(c.get("comment_text", ""))
                created = c.get("created_at", "")
                age = days_ago(created)
                if not text or (age is not None and age > max_days):
                    continue
                first = text.split("\n")[0][:200]
                email = find_apply_email(text)
                body = (f"✉ Apply: {email}\n" if email else "") + text
                out.append({"source": "HN Who's Hiring", "poster_name": first.split("|")[0].strip()[:60] or c.get("author", ""),
                            "poster_role": "Company post (HN Who is hiring)",
                            "poster_url": f"https://news.ycombinator.com/user?id={c.get('author', '')}",
                            "post_url": f"https://news.ycombinator.com/item?id={c.get('objectID', '')}",
                            "post_text": body[:1800], "skills_found": _extract_skills(text, rules),
                            "location": "", "posted_at": created[:10] if created else ""})
            if len(hits) < 500:
                break
    print(f"  → {len(out)} HN company posts (before vetting)")
    return out


def collect_hn_jobs(rules: dict) -> list[dict]:
    """news.ycombinator.com/jobs — YC-backed startups, each pointing at its OWN careers page."""
    out: list[dict] = []
    max_days = rules.get("freshness", {}).get("max_age_days", 40)
    for q in ("engineer", "full stack", "react", "node", "typescript", "software"):
        d = http_get(f"https://hn.algolia.com/api/v1/search_by_date?tags=job&query={urllib.parse.quote(q)}&hitsPerPage=60", timeout=25)
        for h in (d.get("hits", []) if isinstance(d, dict) else []):
            url, title = h.get("url") or "", h.get("title") or ""
            if not url or is_aggregator(url) or not _fresh_enough((h.get("created_at") or "")[:10], rules):
                continue
            pth = urllib.parse.urlparse(url).path
            if not (is_ats(url) or re.search(r"/jobs?/[^/]+", pth) or re.search(r"\d{4,}|[0-9a-f]{8}-|/forms/", url) or pth.count("/") >= 3) \
                    or looks_like_listing(url):
                continue                                   # a careers ROOT page is not a specific posting
            m = re.match(r"(.+?)\s*(?:\(YC [^)]*\))?\s+(?:is hiring|are hiring|hiring)\s*(.*)", title, re.I)
            company = (m.group(1) if m else title.split(" is ")[0]).strip()
            role = (m.group(2) if m and m.group(2) else title)
            if not title_relevant(role) or not _board_ok(role, company, "", rules):
                continue
            out.append(_job(company, role, url, "hn_jobs", rules, need_fetch=True, title_from_page=False,
                            date=(h.get("created_at") or "")[:10], reason="HN Jobs (YC startup)"))
    seen: set[str] = set()
    out = [j for j in out if not (j["direct_apply_url"] in seen or seen.add(j["direct_apply_url"]))]
    print(f"[hn-jobs] {len(out)} candidates")
    return out


# ── Reddit via the free Arctic Shift archive (reddit.com itself blocks scripts) ─────
def collect_reddit_posts(rules: dict) -> list[dict]:
    subs = ["forhire", "remotejs", "developersIndia", "IndiaJobSearch", "hiring", "ProgrammingJobs",
            "startups", "reactjs", "node", "DeveloperJobs", "indianstartups", "StartUpIndia", "jobbit"]
    out: list[dict] = []
    max_days = rules.get("freshness", {}).get("max_age_days", 40)
    for sub in subs:
        d = http_get(f"https://arctic-shift.photon-reddit.com/api/posts/search?subreddit={sub}&limit=100&sort=desc", timeout=40)
        items = d.get("data", []) if isinstance(d, dict) else []
        n0 = len(out)
        for p in items:
            title, body = p.get("title", ""), p.get("selftext", "") or ""
            low = (title + " " + body).lower()
            if sub == "forhire" and "[hiring]" not in title.lower():
                continue
            if "[for hire]" in title.lower() or "looking for work" in low or "[task]" in title.lower():
                continue
            created = p.get("created_utc") or 0
            if created and (time.time() - created) / 86400 > max_days:
                continue
            if not _HIRING_RE.search(low) or not (stack_hits(low, rules) or re.search(r"developer|engineer", low)):
                continue
            author = p.get("author", "")
            out.append({"source": f"Reddit r/{sub}", "poster_name": author, "poster_role": "Poster (Reddit)",
                        "poster_url": f"https://reddit.com/user/{author}",
                        "post_url": "https://reddit.com" + p.get("permalink", ""),
                        "post_text": (title + "\n\n" + body)[:1800], "skills_found": _extract_skills(low, rules),
                        "location": "", "posted_at": datetime.fromtimestamp(created, timezone.utc).strftime("%Y-%m-%d") if created else ""})
        print(f"[reddit] r/{sub}: {len(out) - n0} candidate posts")
        time.sleep(0.4)
    return out


# ── LinkedIn Jobs (public guest endpoint, no login) ─────────────────────────────
_LI_KEYWORDS = ["react developer", "node.js developer", "full stack developer", "typescript developer", "next.js developer",
                "javascript developer", "mern stack developer", "frontend developer react", "backend developer node",
                "software engineer react", "software development engineer", "founding engineer", "react js", "nodejs",
                "frontend engineer", "backend engineer javascript", "full stack engineer startup", "web developer react",
                "software engineer typescript", "sde 1", "sde 2", "software engineer 2", "product engineer", "react native web"]
_LI_CARD = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)".*?base-search-card__title[^>]*>\s*(.*?)\s*</h3>.*?'
                      r'base-search-card__subtitle[^>]*>(.*?)</h4>.*?job-search-card__location[^>]*>\s*(.*?)\s*</span>'
                      r'(?:.*?datetime="([\d-]+)")?', re.S)


def collect_linkedin_jobs(rules: dict) -> list[dict]:
    """LinkedIn public job cards → company name + role; the board page is never shown — verify_candidate only
    keeps the role if the company's own ATS posting (or an apply e-mail in the JD) is found."""
    out: list[dict] = []
    seen_ids: set[str] = set()
    light = SCAN_MODE == "light"
    if light:                                             # every 30 min: only roles posted in the last 24 h, all of India
        combos = [(k, "India", "r86400", (0, 25)) for k in _LI_KEYWORDS[:10]]
    else:                                                 # full scan: all of India (every city) + India-remote + worldwide remote
        combos = [(k, "India", "r1209600", (0, 25, 50, 75, 100, 125, 150, 175)) for k in _LI_KEYWORDS] + \
                 [(k, "India", "r1209600&f_WT=2", (0, 25, 50)) for k in _LI_KEYWORDS[:12]] + \
                 [(k, "Worldwide", "r1209600&f_WT=2", (0, 25)) for k in _LI_KEYWORDS[:8]]
    deadline = time.time() + int(os.environ.get("LINKEDIN_BUDGET_SECS", "110" if light else "480"))

    def fetch(kv):
        k, loc, tpr, starts = kv
        res = []
        if time.time() > deadline:
            return res
        for start in starts:
            h = ""
            for attempt in range(3):                              # LinkedIn answers 429 when hit too fast → back off
                h = http_get_text("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
                                  f"keywords={urllib.parse.quote(k)}&location={urllib.parse.quote(loc)}&f_TPR={tpr}&f_E=2%2C3&start={start}",
                                  timeout=25, quiet=True)
                if h:
                    break
                time.sleep(2 + attempt * 3)
            if not h or "base-search-card" not in h:
                break
            res.append(h)
            time.sleep(0.8)
        return res
    with ThreadPoolExecutor(2) as ex:
        pages = [p for r in ex.map(fetch, combos) for p in r]
    cards = []
    for html_page in pages:
        for blk in html_page.split("<li>")[1:]:
            m = _LI_CARD.search(blk)
            if not m:
                continue
            jid, title, comp, loc, date = m.groups()
            if jid in seen_ids:
                continue
            seen_ids.add(jid)
            slug_m = re.search(r'linkedin\.com/company/([^"?/]+)', comp)
            comp = re.sub(r"<[^>]+>", "", comp).strip()
            if slug_m:
                _li_register_company(slug_m.group(1), comp)
            title = htmllib.unescape(re.sub(r"<[^>]+>", "", title)).strip()
            loc = htmllib.unescape(loc).strip()
            if not _board_ok(title, comp, loc, rules) or not _fresh_enough(date or "", rules):
                continue
            cards.append((jid, title, comp, loc, date or ""))
    def detail(c):
        jid, title, comp, loc, date = c
        h = ""
        for attempt in range(3):
            h = http_get_text(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}", timeout=25, quiet=True)
            if h:
                break
            time.sleep(2 + attempt * 3)
        time.sleep(0.5)
        m = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', h or "", re.S)
        jd = _html_to_text(m.group(1)) if m else ""
        if not date:                                        # "2 days ago" on the posting itself
            ago = re.search(r'posted-time-ago__text[^>]*>\s*(\d+)\s+(minute|hour|day|week|month)s?\s+ago', h or "")
            if ago:
                n, unit = int(ago.group(1)), ago.group(2)
                days = {"minute": 0, "hour": 0, "day": n, "week": 7 * n, "month": 30 * n}[unit]
                date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
                c = (jid, title, comp, loc, date)
        poster = None                                       # "Direct message the job poster" = the real recruiter / hiring manager
        pm = re.search(r'message-the-recruiter.*?href="(https://[a-z.]*linkedin\.com/in/[^"?]+)[^"]*".*?<span class="sr-only">\s*([^<]+?)\s*(?:<|$)',
                       h or "", re.S)
        if pm:
            poster = {"url": pm.group(1).replace("in.linkedin.com", "www.linkedin.com"), "name": htmllib.unescape(pm.group(2)).strip()}
        return c, jd, poster
    with ThreadPoolExecutor(3) as ex:
        for (jid, title, comp, loc, date), jd, poster in ex.map(detail, cards):
            if len(jd) < 200:
                continue
            j = _job(comp, title, f"https://www.linkedin.com/jobs/view/{jid}", "linkedin_jobs", rules,
                     location=loc, date=date, jd_text=jd, board="LinkedIn", reason="LinkedIn Jobs")
            j["li_poster"] = poster
            out.append(j)
    print(f"[linkedin-jobs] {len(cards)} cards → {len(out)} with full JD")
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  POST VETTING — only genuine hiring posts by the people doing the hiring
# ═══════════════════════════════════════════════════════════════════════════
_HIRING_RE = re.compile(r"\b(we(?:'re| are)\s+(?:hiring|looking|expanding)|is hiring|are hiring|(?<!\w)hiring\b(?!\s+(?:round|process|manager|season))|"
                        r"vacanc|join (?:us|our team|the team)|send (?:your )?(?:cv|resume|profile)|dm me|\bopenings?\b|\bopen roles?\b|\bopen positions?\b|"
                        r"recruiting|looking for (?:a|an|talented|experienced|passionate|skilled|strong|motivated)\b)", re.I)
_FEED_HEADLINE = re.compile(r"hiring solutions|client coordinator|consulting|helping people find|dream jobs?|job (?:alerts?|updates?|portal|board|feed|vacanc)|jobs? (?:and|&) (?:career|internship)|daily jobs?|"
                            r"placement|staffing|consultanc|manpower|recruitment (?:agency|services|firm)|opportunit(?:y|ies) (?:hub|for)|"
                            r"career guidance|freelance(?:r| platform)|hiring updates|off[- ]campus|referral (?:group|community)", re.I)
_SEEKER_TEXT = re.compile(r"refer me|can anyone refer|my resume|available for hire|for hire\b|open to work|#opentowork|available immediately|looking for (?:my next|a new|new) (?:role|opportunit|position|job)|"
                          r"(?:actively )?(?:seeking|looking for) (?:a )?(?:new )?(?:role|opportunit|position|job)s? (?:as|in|for|where)|"
                          r"i(?:'m| am) (?:currently )?(?:looking|available|seeking)", re.I)
_FEED_TEXT = re.compile(r"\bclient\s*:|\bw-?2\b|\b1099\b|#?job ?alert|like,? (?:share|comment)|tag (?:someone|a friend)|"
                        r"follow (?:me|us|the page) for|join (?:my|our) (?:whatsapp|telegram|community|group)|walk[- ]?in|"
                        r"our client|client requirement|\bbench\b|\bc2c\b|corp[- ]to[- ]corp", re.I)
_POSTER_TITLE = re.compile(r"founder|co-?founder|\bceo\b|\bcto\b|\bcpo\b|\bcoo\b|chief|owner|president|"
                           r"head of|\bvp\b|director|recruit|talent|\bhr\b|human resources|people (?:ops|operations|partner|team|and culture)|"
                           r"hiring manager|engineering manager|tech lead|\bengineer\b|\bdeveloper\b|\bsde\b|\bswe\b", re.I)
_POST_NEG = ["designer", "ui/ux", "ux", "sales", "marketing", "devops", "data scientist", "ml engineer", "qa", "tester",
             "intern", "interns", "internship", "internships", "freelance", "freelancer", "contract", "contractor", "robotics",
             "embedded", "firmware", "ai engineer", "machine learning", "data engineer", "cloud engineer", "forward deployed",
             "security engineer", "project manager", "product manager", "customer success", "solutions engineer", "support engineer",
             "video editor", "artist", "blender", "seo", "bde", "telecaller", "graphic", "content writer", "react native"]
_NOT_HIRING = re.compile(r"open[- ]sourced|applications in just|would you ignore|interviews i asked|i didn'?t start|winning a new website|"
                         r"thrilled to announce|excited to announce|congratulations|proud to share|we(?:'ve| have) launched|"
                         r"for client\b|on behalf of|help(?:ing)? (?:you|companies|startups) (?:hire|find)|turing\b", re.I)
_SENIOR_HEAD = re.compile(r"\b(senior|sr\.?|staff|principal|distinguished|architect|director|head of|vp|lead)\b", re.I)


_DEV_ROLE = re.compile(r"\b(?:react(?:\.?js)?|node(?:\.?js)?|next\.?js|mern|mean|javascript|typescript|full[\s-]?stack|front[\s-]?end|back[\s-]?end|"
                       r"software (?:engineer|developer)|web developer|sde[\s-]?\d?|founding engineer|ui developer|developers?|engineers?)\b", re.I)
_BAD_ROLE = re.compile(r"\b(?:manager|head of|team lead(?:er)?|tech lead|\blead\b|cto|vp|director|graduates?|b\.?tech|freshers?|trainee|apprentice|"
                       r"angular|laravel|php|wordpress|shopify|flutter|android|ios|\.net|golang|ruby|app developer|"
                       r"qa\b|tester|analyst|executive|specialist)\b", re.I)
_CONTACT_ROUTE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\bdm\b|direct message|message me|reach out|get in touch|contact (?:me|us)|"
                            r"send (?:your |an? )?(?:cv|resume|profile|portfolio|application)|share (?:your )?(?:cv|resume|profile)|"
                            r"drop (?:your )?(?:cv|resume)|whatsapp|\bcomment\b|ping me|(?:call|text)\s*[:\-]?\s*\+?\d[\d ]{8,}|\+?\d[\d -]{9,}\d|"
                            r"apply (?:here|now|at|via|through|on)\b.{0,60}https?://|https?://\S*(?:apply|careers?|jobs?|forms?)\S*", re.I)
POST_MAX_DAYS = int(os.environ.get("POST_MAX_DAYS", "30"))
_ENTRY_TEXT = re.compile(r"\bfreshers?\b|\bfresh graduates?\b|\b0\s*(?:-|–|to)\s*(?:6\s*months?|1\s*(?:years?|yrs?)|2\s*(?:years?|yrs?))\b|"
                         r"\b0\s*(?:-|–|to)\s*[12]\b|6\s*months?\s*(?:-|–|to)\s*1\s*(?:years?|yrs?)|"
                         r"\b(?:0|zero)\s*(?:\+)?\s*(?:years?|yrs?)\b|\b(?:[1-9]|1[0-1])\s*months?\s*(?:of\s+)?(?:experience|exp)\b|"
                         r"entry[- ]level|batch of 20\d\d|20\d\d (?:batch|graduates?|pass[- ]?outs?)|\bcampus (?:hire|drive)\b", re.I)


def _exp_ranges(text: str):
    try:
        return [(a, b) for a, b in parse_experience(text)]
    except Exception:
        return []


_MONTHLY = re.compile(r"(?:per\s*month|/\s*month|\bpm\b|monthly|a month|p\.m\.|per\s*mo\b)", re.I)
_AMT = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)\s*(k|lpa|lakhs?|l\b)?|\b([\d.]+)\s*(k|lpa|lakhs?)\b", re.I)


def post_salary_lpa(text: str):
    """Highest INR salary found in the post, in LPA. Monthly figures (₹15,000–20,000 per month, 50K, up to ₹35,000)
    are annualised; figures with no period and under ₹10 lakh are treated as monthly."""
    best = None
    for line in re.split(r"\n", text):
        if not re.search(r"salary|ctc|stipend|pay|package|compensation|₹|\brs\b|inr|\d\s*k\b|lpa|lakh", line, re.I):
            continue
        vals = []
        for m in _AMT.finditer(line):
            num = m.group(1) or m.group(3)
            unit = (m.group(2) or m.group(4) or "").lower()
            try:
                v = float((num or "").replace(",", ""))
            except ValueError:
                continue
            if unit in ("lpa", "l") or unit.startswith("lakh"):
                vals.append(v)
            elif unit == "k":
                vals.append(v * 1000 * 12 / 1e5 if (_MONTHLY.search(line) or v < 200) else v * 1000 / 1e5)
            elif v >= 1e6:
                vals.append(v / 1e5)
            elif v >= 1000:
                vals.append(v * 12 / 1e5)            # ₹35,000 → monthly
        # "15-20K" style range where the second number carries the K
        for m in re.finditer(r"(\d{1,3})\s*(?:-|–|to)\s*(\d{1,3})\s*k\b", line, re.I):
            vals.append(float(m.group(2)) * 1000 * 12 / 1e5)
        if vals:
            top = max(vals)
            best = top if best is None else max(best, top)
    return best


_BLOCK_WORDS = re.compile(r"\b(?:rust|golang|go lang|c\+\+|c#|\.net|asp\.net|laravel|codeigniter|angular(?:js)?|django|flask|flutter|kotlin|swift|ruby|rails|"
                          r"php|spring|hibernate|wordpress|shopify|salesforce|sap|scala|elixir|erlang|haskell|unity)\b", re.I)
_SOFT = re.compile(r"good to have|nice to have|plus\b|familiar|advantage|bonus|preferred|optional|or\b.*\b(?:node|react|next)|\b(?:node|react|next)", re.I)
_ONSITE = re.compile(r"face[- ]to[- ]face|walk[- ]in|local (?:candidates|applicants)|\bwfo\b|work from office|on-?site|in[- ]office|office[- ]based|"
                     r"(?:must|should) (?:be )?(?:based|located|reside)|only .{0,25}\bbased\b|local (?:candidates|applicants)", re.I)
_REMOTE_OK = re.compile(r"\bremote\b|work from home|\bwfh\b|hybrid|anywhere", re.I)
_NOT_INDIA = re.compile(r"\b(?:egypt|saudi|riyadh|jeddah|uae|dubai|abu dhabi|qatar|kuwait|bahrain|oman|nigeria|philippines|pakistan|bangladesh|"
                        r"sri lanka|poland|brazil|mexico|turkey|israel|germany|france|netherlands|singapore|malaysia|australia)\b|#egypt", re.I)
_GIG = re.compile(r"trial|contract basis|stipend|part[- ]time|unpaid|per hour|\bfreelance|internship|\bintern\b|project[- ]based|commission", re.I)
_FIRST_PERSON_SEEKER = re.compile(r"\babout me\b|\bi had been\b|\bi was laid off\b|laid off|\bmy (?:experience|skills|resume|cv|portfolio|projects)\b|"
                                  r"\bi(?:'m| am) (?:really |highly |currently |also )*(?:looking|urgently|proficient|seeking|available|a )|"
                                  r"urgently looking for a job|\bany suggestions\b|exhausted|\bi have (?:built|worked|been)\b|\bhire me\b", re.I)
_STRONG_HIRING = re.compile(r"\bwe(?:'re| are)\s+(?:hiring|looking|expanding|seeking|building)|\b(?:is|are)\s+hiring|#hiring|\bhiring\s*[:|!\-–]|now hiring|hiring alert|"
                            r"\bopenings?\b|\bvacanc|join (?:us|our)|to join our|\bwe need\b|\bi(?:'m| am) hiring|\bhiring (?:a|an|for)\b|looking to hire|apply (?:now|here)|send (?:your |an? )?(?:cv|resume)", re.I)


def _post_full_text_checks(text: str, low: str, p: dict, rules: dict):
    """Reads the WHOLE post, not just the title. Returns a rejection reason or ''."""
    first = low[:200]
    if re.match(r"\s*(?:looking for|seeking|need|want)\b[^\n]{0,60}(?:role|job|position|opportunit)", first) or _FIRST_PERSON_SEEKER.search(low):
        return "job-seeker post (the author is looking for a job)"
    if not _STRONG_HIRING.search(low):
        return "no clear 'we are hiring' statement"
    lpa = post_salary_lpa(text)
    if lpa is not None and lpa < rules.get("compensation", {}).get("india_min_lpa", 15):
        return f"salary stated in the post is ~{lpa:.1f} LPA — below your floor"
    if _GIG.search(low):
        return "trial / contract / part-time / intern wording"
    if _NOT_INDIA.search(low) and not re.search(r"india|bangalore|bengaluru|hyderabad|pune|mumbai|delhi|noida|gurugram|chennai", low):
        return "role is for another country"
    core_w = re.compile(r"node|react|next|typescript|javascript|mern", re.I)
    for line in re.split(r"\n|(?<=[.!?])\s+", text):
        if _BLOCK_WORDS.search(line) and not _SOFT.search(line) and not core_w.search(line):
            return f"requires another stack ({_BLOCK_WORDS.search(line).group(0).lower()})"
    if re.search(r"\b(?:us|canada|europe|uk|latam|emea)\b[^\n]{0,30}\b(?:us|canada|europe|uk|latam|emea)\b", low[:260]) and not re.search(r"india|asia|apac", low[:400]):
        return "regions listed do not include India"
    return ""


def _post_age_days(p: dict):
    d = (p.get("posted_at") or "")[:10]
    if d:
        a = days_ago(d)
        if a is not None:
            return a
    m = re.search(r"activity-(\d{16,19})|urn:li:activity:(\d{16,19})", str(p.get("post_url", "")))
    if m:
        ms = int(m.group(1) or m.group(2)) >> 22
        return max(0, int((time.time() * 1000 - ms) / 86_400_000))
    return None


_TEMPLATE_POST = re.compile(r"apply today or share this post|share this post with your network|click (?:on )?the link below to apply$", re.I)


def vet_post(p: dict, rules: dict) -> tuple[bool, str]:
    ok, why = _vet_post_base(p, rules)
    if not ok:
        return ok, why
    return _vet_post_strict(p, rules)


def _vet_post_strict(p: dict, rules: dict) -> tuple[bool, str]:
    """Final gate (user rule: ONLY real hiring posts by founders / recruiters / hiring managers — a named dev role
    at 1-3 yrs, a way to contact them, and nobody reposting other people's jobs)."""
    text = unicodedata.normalize("NFKC", p.get("post_text") or "").replace("’", "'")
    low = text.lower()
    head = low[:450]
    src = str(p.get("source", ""))
    role = p.get("poster_role") or ""
    age = _post_age_days(p)
    if age is not None and age > POST_MAX_DAYS:
        return False, f"older than {POST_MAX_DAYS} days ({age}d) — the role is usually filled"
    if _ENTRY_TEXT.search(low):
        return False, "entry-level wording (fresher / 0-1 yr / months of experience)"
    mx = _exp_ranges(text)
    if mx and all(hi is not None and hi <= 1 for lo, hi in mx):
        return False, "experience range tops out at 1 yr or less"
    why = _post_full_text_checks(text, low, p, rules)
    if why:
        return False, why
    if re.search(r"teamware|\bstaffing\b|\bconsultancy\b|manpower|talent solutions|hr solutions|recruitment", (p.get("_tr_org") or "") + " " + (p.get("poster_role") or ""), re.I):
        return False, "staffing / recruitment agency"
    if _TEMPLATE_POST.search(low) or re.search(r"^\s*we're #hiring a new [^\n]{3,120}\.\s*apply", low):
        return False, "auto-generated company-page job card"
    if re.search(r"welcom(?:e|ing)\b.{0,40}\b(?:to the team|to \w+ as)|joined (?:us|our team) as|\bnew (?:joiner|hire)s?\b|work anniversary|birthday", head):
        return False, "welcome / celebration post"
    if not _DEV_ROLE.search(head):
        return False, "no developer role named"
    heading = re.split(r"\n\s*\n", text.strip(), 1)[0][:300].lower()
    if re.search(r"\b(?:sde|swe|software (?:development )?engineer|engineer|developer)\s*[-–]?\s*(?:3|iii|4|iv|5)\b|\bl[4-6]\b|\bsde[\s-]?(?:3|iii)\b", heading):
        return False, "senior-level (level III+)"
    if _BAD_ROLE.search(heading):
        return False, "role not a match (manager / lead / other stack)"
    if re.search(r"part[- ]time|unpaid|volunteer|reverse engineering|deobfuscation|\bintern", head):
        return False, "part-time / intern / not a dev job"
    m = re.search(r"\b(manager|head of|team lead(?:er)?|angular|laravel|php|golang|graduates?|b\.?tech|freshers?)\b", heading)
    if m:
        return False, f"role not a match ({m.group(1)})"
    core = _hits(rules.get("stack", {}).get("core", []), low)
    structured = src.startswith(("Hiring sheet", "HN", "Reddit"))
    generic = re.search(r"full[\s-]?stack|front[\s-]?end|software (?:engineer|developer)|web developer|founding engineer", head)
    if not core and not (structured and generic):
        return False, "no JS/TS/React/Node/Next stack in the post"
    if re.search(r"angular|laravel|php|\.net|flutter|wordpress", head) and len(core) < 3:
        return False, "other stack in the post"
    if not structured and not _CONTACT_ROUTE.search(low):
        return False, "no way to contact / apply (no email, DM or link)"
    if role == "Company page" and not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+|\bdm\b|send (?:your )?(?:cv|resume)|reach out", low):
        return False, "company page post with no contact email / DM"
    if src in ("LinkedIn", "X/Twitter") and role and role != "Company page":
        dev_only = not re.search(r"founder|\bceo\b|\bcto\b|owner|director|\bhr\b|recruit|talent|people|hiring|head of|\bvp\b|manager|human resources", role.lower())
        if dev_only and not re.search(r"\b(?:we|our|my team|i am hiring|i'm hiring|referral|refer)\b", low[:500]):
            return False, "engineer reposting someone else's job"
        if re.search(r"helping people find|dream job|sharing tech tips|resources|job (?:seekers?|search)|career (?:coach|mentor)|linkedin (?:growth|creator)", role.lower()):
            return False, "job-feed / career-content author"
    if src.startswith("HN") and (re.match(r"\s*i'?m [^,\n]{2,40},", low) or "seeking work" in low or "wants to be hired" in low):
        return False, "job-seeker post"
    if src.startswith(("HN", "Hiring sheet")):
        if re.search(r"\b(?:us|usa|canada|europe|eu|uk|latam|emea)\b", low[:220]) and not re.search(r"india|asia|apac|anywhere|global", low[:400]):
            return False, "remote limited to US / Europe"
        if not re.search(r"india|worldwide|anywhere|global|apac|asia|remote\s*\(?(?:global|any)", low[:500]) and \
                (re.search(r",\s*[A-Z]{2}\b", text[:300]) or re.search(r"hybrid|on-?site|in[- ]office", head)):
            return False, "on-site / hybrid outside India"
    return True, "ok"


def _vet_post_base(p: dict, rules: dict) -> tuple[bool, str]:
    """Same rules as jobs, applied to a hiring post: stack, seniority, experience, location, genuine hirer."""
    text = unicodedata.normalize("NFKC", p.get("post_text") or "").replace("’", "'")
    text = re.sub(r"^✉ Apply:[^\n]*\n", "", text)
    low = text.lower()
    head = low[:320]
    S = rules.get("stack", {})
    structured = str(p.get("source", "")).startswith(("Hiring sheet", "HN"))     # "Company | Role | Remote | Email: …"
    if not structured and not _HIRING_RE.search(low):
        return False, "not a hiring post"
    if is_staffing_text(low, rules) or _FEED_TEXT.search(low):
        return False, "staffing / reshare / job-feed style post"
    if _SEEKER_TEXT.search(low) or re.match(r"\s*(?:looking for|seeking)\b[^\n]{0,70}(?:opportunit|roles?\b|positions?\b)", low) or "open to relocate" in low:
        return False, "job-seeker post, not a hiring post"
    if re.search(r"\bi(?:'m| am) a (?:\w+ ){0,4}(?:developer|engineer|designer)\b", low[:300]):
        return False, "job-seeker post, not a hiring post"
    hdr = low[:300].split("\n")[0] if "\n" in low[:300] else low[:300]
    if re.search(r"remote\s*\(?\s*(?:us|usa|u\.s\.|canada|eu|europe|uk|emea|north america)\b|\(us\b|us citizens?|\bus[- ]?only\b", hdr) \
            and not re.search(r"india|worldwide|anywhere|global|apac", hdr):
        return False, "US/EU-only remote"
    if _NOT_HIRING.search(low[:1200]):
        return False, "announcement / marketing / agency post, not a hiring post"
    if "[removed]" in low or "[deleted]" in low:
        return False, "post body removed"
    if re.search(r"\bu\.?s\.?[- ]based\b|\bus[- ]only\b|\bper hour\b|/\s*(?:hour|hr)\b|\bai training\b|\bproject-based\b", low):
        return False, "US-only / hourly gig post"
    if not (_HIRING_RE.search(low[:400]) or structured):
        return False, "not clearly a hiring announcement"
    role = (p.get("poster_role") or "")
    if _FEED_HEADLINE.search(role):
        return False, "poster looks like a job-feed / agency account"
    for excl in rules.get("excluded_companies", []):
        if _has(excl.lower(), low[:400]):
            return False, f"excluded company {excl}"
    core = _hits(S.get("core", []), low)
    blocked = list(S.get("blocked", []))
    if re.search(r"\bjava\b", low) and re.search(r"\bspring\b", low):
        blocked.append("spring")
    b_occ, c_occ = _occurrences(blocked, low), _occurrences([c for c in S.get("core", []) if c not in ("python", "java", "fastapi")], low)
    if b_occ and b_occ >= max(c_occ, 1):
        return False, "stack outside yours (" + ", ".join(_hits(blocked, low)[:2]) + ")"
    generic = re.search(r"\b(software|full[\s-]?stack|front[\s-]?end|back[\s-]?end|web|application|product) (?:engineer|developer)|"
                        r"\bsde\b|\bsoftware development\b|\bfounding engineer|\bfull[\s-]?stack\b", head)
    if not core and not generic:
        return False, "not a matching software-dev role"
    for bad in _POST_NEG:
        if bad != "recruiter" and _has(bad, head):
            return False, f"different discipline ({bad})"
    for ph in rules.get("experience", {}).get("fresher_phrases", []):
        if ph.lower() in low[:1500] and not re.search(r"\b[1-3]\s*(?:-|to|\+)\s*[3-6]?\s*(?:years|yrs)", low):
            return False, f"fresher / intern post ('{ph}')"
    lpa, _snip = parse_salary_lpa(text)
    if lpa is not None and lpa < rules.get("compensation", {}).get("india_min_lpa", 15):
        return False, f"salary {lpa:g} LPA below floor"
    stmts = parse_experience(text)
    if stmts and min(s[0] for s in stmts) > rules.get("experience", {}).get("max_min_years", 3):
        return False, f"min experience {min(s[0] for s in stmts)}+ yrs"
    if _SENIOR_HEAD.search(head) and not re.search(r"junior|\bsde[\s-]?(1|2|i|ii)\b|[123]\s*(?:-|to)\s*[34]\s*(?:years|yrs)|\b[123]\+?\s*(?:years|yrs)", low):
        return False, "senior-level post"
    if stmts and all((x[1] is not None and x[1] <= 1) for x in stmts):
        return False, "entry-level post"
    for phrase in rules.get("locations", {}).get("reject_region_phrases", []):
        if phrase.lower() in low:
            return False, f"region: {phrase}"
    L = rules.get("locations", {})
    india = "india" in low or any(_has(x, low) for x in L.get("india_places", []))
    worldwide = bool(re.search(r"worldwide|anywhere in the world|work from anywhere|global(?:ly)? remote|remote[- ]first|apac|asia", low))
    if not india and not worldwide and any(_has(x, low[:700]) for x in L.get("non_india_places", [])):
        return False, "non-India location"
    if not (india or worldwide or "remote" in low) and not p.get("location"):
        p["location"] = "not stated — ask the poster"          # unknown ≠ wrong: kept, flagged for the candidate
    return True, "ok"


def _kv_get(k: str) -> str:
    conn = get_db()
    try:
        r = conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else ""
    finally:
        conn.close()


def _kv_set(k: str, v: str) -> None:
    conn = get_db()
    try:
        conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?,?)", (k, v))
        conn.commit()
    finally:
        conn.close()


def _due(key: str, hours: float) -> bool:
    last = _kv_get(key)
    if not last:
        return True
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() > hours * 3600
    except Exception:
        return True


# ── Apify (optional, pay-per-result: needs APIFY_TOKEN) ─────────────────────
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
APIFY_POST_HOURS = float(os.environ.get("APIFY_POST_HOURS", "168"))      # weekly keeps the free $5/month enough      # min gap between paid post pulls
LI_QUERY_SET_A = [
    "we are hiring react developer", "we are hiring full stack developer node.js", "hiring node.js developer 2 years",
    "hiring typescript engineer", "hiring next.js developer", "hiring MERN stack developer", "hiring frontend developer react 1-3 years",
    "hiring backend developer node.js", "hiring software engineer india startup", "founding engineer hiring react node",
    "send your resume react developer", "DM me hiring full stack developer", "hiring SDE 1 SDE 2 javascript",
    "looking for full stack engineer react node startup", "referral open positions react node",
]
LI_QUERY_SET_B = [
    "hiring react developer bangalore", "hiring full stack developer hyderabad", "hiring react js developer pune",
    "hiring node.js developer chennai", "hiring frontend developer remote india", "hiring software engineer mumbai react",
    "hiring full stack developer gurgaon noida delhi", "hiring javascript developer immediate joiner", "hiring typescript react remote worldwide",
    "hiring next.js react developer work from home", "we're hiring MERN developer", "hiring web developer react node startup",
    "open position software engineer react node 2 years", "hiring sde react node india", "join our team full stack developer javascript",
]
LI_QUERY_SET_C = [
    "I'm hiring react developer", "my startup is hiring full stack developer", "we're looking for a React developer", "DM me node.js developer opening",
    "referral react typescript opening", "open roles react typescript", "hiring SDE-1 react node", "hiring software developer 1-3 years javascript",
    "hiring web developer next.js", "immediate joiners react node developer", "hiring full stack developer mern 1 year experience",
    "hiring react developer 2+ years remote", "we are growing our engineering team react node", "looking for frontend developer react typescript startup",
    "hiring backend engineer node typescript",
]
LI_POST_QUERIES = [LI_QUERY_SET_A, LI_QUERY_SET_B, LI_QUERY_SET_C][datetime.now().toordinal() % 3]
# the actor returns nothing when this filter is long, so it runs once per short group
LI_AUTHOR_GROUPS = ["founder OR CEO OR CTO", "recruiter OR talent OR HR"]
LI_MAX_POSTS = int(os.environ.get("LI_MAX_POSTS", "20"))                # per query → ~$0.002 each


def apify_run(actor: str, payload: dict, timeout: int = 420, max_usd: float = 1.5) -> list[dict]:
    if not APIFY_TOKEN:
        return []
    url = (f"https://api.apify.com/v2/acts/{actor.replace('/', '~')}/run-sync-get-dataset-items"
           f"?token={APIFY_TOKEN}&format=json&clean=1&maxTotalChargeUsd={max_usd}")
    d = http_post(url, payload, timeout=timeout)
    return d if isinstance(d, list) else []


def _li_item_to_post(it: dict, rules: dict) -> dict | None:
    a = it.get("author") or {}
    if str(a.get("type", "")).lower() == "company":          # company pages include the job-feed accounts; people only
        return None
    nf = lambda s: unicodedata.normalize("NFKC", s or "")          # LinkedIn fancy bold (𝗛𝗶𝗿𝗶𝗻𝗴) → plain letters
    name = nf(a.get("name")).strip()
    headline = nf(a.get("info") or a.get("headline") or a.get("position")).strip()
    text = nf(it.get("content") or it.get("text")).strip()
    url = it.get("linkedinUrl") or it.get("url") or ""
    if not text or not url:
        return None
    date = ((it.get("postedAt") or {}).get("date") or "")[:10] if isinstance(it.get("postedAt"), dict) else str(it.get("postedAt") or "")[:10]
    return {"source": "LinkedIn", "poster_name": name or "LinkedIn member",
            "poster_role": headline or "", "poster_url": (a.get("linkedinUrl") or url).split("?")[0], "post_url": url,
            "post_text": text[:1800], "skills_found": _extract_skills(text, rules),
            "location": "", "posted_at": date, "_headline": headline}


# ── LinkedIn posts via FREE search APIs (no Apify): find post URLs → read the PUBLIC post page (no login) ──
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")     # serper.dev — 2,500 free searches, no card
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")     # tavily.com — 1,000 free searches / month, no card
LI_SEARCH_PHRASES = [
    '"we are hiring" "react developer"', '"hiring" "full stack developer" "node.js"', '"hiring" "typescript" "react" engineer',
    '"hiring" "next.js developer"', '"hiring" "MERN stack"', '"hiring" "software engineer" "2-4 years" india', '"hiring" "frontend developer" "react" india',
    '"founding engineer" hiring react node', '"send your resume" "react" developer', '"DM me" hiring "full stack"', '"open position" "node.js developer" india',
    '"hiring" "javascript developer" bangalore OR hyderabad OR pune OR remote',
]


def _search_post_urls(query: str) -> list[str]:
    out: list[str] = []
    try:
        if SERPER_API_KEY:
            req = urllib.request.Request("https://google.serper.dev/search", method="POST",
                                         data=json.dumps({"q": f"site:linkedin.com/posts {query}", "tbs": "qdr:w"}).encode(),
                                         headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                out = [x.get("link", "") for x in json.loads(r.read().decode()).get("organic", [])]
        elif TAVILY_API_KEY:
            req = urllib.request.Request("https://api.tavily.com/search", method="POST",
                                         data=json.dumps({"query": query.replace('"', ""), "include_domains": ["linkedin.com"], "max_results": 20,
                                                          "time_range": "month", "search_depth": "basic"}).encode(),
                                         headers={"Authorization": f"Bearer {TAVILY_API_KEY}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as r:
                out = [x.get("url", "") for x in json.loads(r.read().decode()).get("results", [])]
        else:                                           # keyless: Brave (past-month filter), then Bing, then DuckDuckGo
            q = urllib.parse.quote(f"site:linkedin.com/posts {query}")
            for url in (f"https://search.brave.com/search?q={q}&tf=pm&source=web",
                        f"https://www.bing.com/search?q={q}&count=50&filters=ex1%3a%22ez5%22",
                        f"https://html.duckduckgo.com/html/?q={q}&df=m"):
                h = http_get_text(url, 25, quiet=True) or ""
                found = [urllib.parse.unquote(m) for m in re.findall(r"uddg=([^&\"]+)", h)] + \
                        re.findall(r"https://(?:[a-z]{2,3}\.)?linkedin\.com/(?:posts|feed/update)/[^\"&<>\s\\]+", h)
                found = [u for u in found if "linkedin.com/posts/" in u or "linkedin.com/feed/update/" in u]
                if found:
                    out = found
                    break
                time.sleep(1)
    except Exception as exc:
        print(f"  [search ✗] {str(exc)[:80]}")
    return [u.split("?")[0] for u in out if "linkedin.com/posts/" in u or "linkedin.com/feed/update/" in u]


_PROFILE_HEAD: dict[str, str] = {}


def _li_headline(profile_url: str) -> str | None:
    """'Name - Founder @ X - Y | LinkedIn' from the public profile page → 'Founder @ X - Y' (None when unreadable)."""
    if not profile_url:
        return None
    if profile_url in _PROFILE_HEAD:
        return _PROFILE_HEAD[profile_url]
    time.sleep(1.0)
    h = http_get_text(profile_url, 20, quiet=True)
    m = re.search(r'<meta property="og:title" content="([^"]+)"', h or "")
    head = None
    if m:
        t = htmllib.unescape(m.group(1)).replace(" | LinkedIn", "")
        head = t.split(" - ", 1)[1] if " - " in t else ""
    _PROFILE_HEAD[profile_url] = head
    return head


def _li_public_post(url: str, rules: dict) -> dict | None:
    time.sleep(1.2)
    h = http_get_text(url, 25, quiet=True)
    if not h:
        return None
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', h, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") in ("SocialMediaPosting", "DiscussionForumPosting", "Article"):
            body = unicodedata.normalize("NFKC", d.get("articleBody") or d.get("text") or "").strip()
            a = d.get("author") or {}
            if not body or not isinstance(a, dict):
                return None
            date = (d.get("datePublished") or "")[:10]
            if date and (days_ago(date) or 0) > rules.get("freshness", {}).get("max_age_days", 40):
                return None
            purl = (a.get("url") or "").split("?")[0]
            head = _li_headline(purl)
            return {"source": "LinkedIn", "poster_name": unicodedata.normalize("NFKC", a.get("name", "")), "poster_role": head or "",
                    "poster_url": purl or url, "post_url": url, "post_text": body[:1800], "skills_found": _extract_skills(body, rules),
                    "location": "", "posted_at": date, "_headline": head}
    return None


def collect_linkedin_search(rules: dict) -> list[dict]:
    """Free route to LinkedIn hiring posts: Serper / Tavily (free tiers) find URLs, the public page gives the full text."""
    if not (SERPER_API_KEY or TAVILY_API_KEY) and os.environ.get("DDG_FALLBACK", "1") != "1":
        return []
    day = datetime.now().toordinal()
    n = int(os.environ.get("LI_SEARCH_QUERIES", "6"))
    phrases = [LI_SEARCH_PHRASES[(day * n + i) % len(LI_SEARCH_PHRASES)] for i in range(n)]      # rotate through the list daily
    conn = get_db()
    known = {r[0] for r in conn.execute("SELECT post_url FROM posts")}
    conn.close()
    urls: list[str] = []
    for q in phrases:
        for u in _search_post_urls(q):
            if u not in known and u not in urls:
                urls.append(u)
        time.sleep(0.5)
    print(f"[linkedin-search] {len(phrases)} searches → {len(urls)} new post URLs "
          f"({'Serper' if SERPER_API_KEY else 'Tavily' if TAVILY_API_KEY else 'keyless'})")
    out = []
    urls = [u for u in urls if (_post_age_days({"post_url": u}) or 0) <= POST_MAX_DAYS]
    for u in urls[: int(os.environ.get("LI_SEARCH_FETCH", "40"))]:
        p = _li_public_post(u, rules)
        if p:
            out.append(p)
    print(f"  → {len(out)} readable posts")
    return out


# ── LinkedIn company pages (public, no login): ~10 recent posts per page in one request ─────────────
def _li_register_company(slug: str, name: str = "") -> None:
    if not slug or len(slug) > 80:
        return
    conn = get_db()
    try:
        conn.execute("INSERT OR IGNORE INTO li_companies (slug, name) VALUES (?,?)", (slug.lower(), name))
        conn.commit()
    finally:
        conn.close()


_LI_SIZE: dict = {}


def _li_company_page_posts(slug: str) -> tuple[list[dict] | None, int]:
    u = f"https://www.linkedin.com/company/{slug}/posts/"
    try:
        req = urllib.request.Request(u, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read(900_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception:
        return None, 0
    out = []
    sm = re.search(r'numberOfEmployees"\s*:\s*\{[^}]*?"value"\s*:\s*(\d+)', body)
    rng = re.search(r'([\d,]+(?:-[\d,]+)?\+?) employees', body)
    if sm or rng:
        _LI_SIZE[slug] = {"count": int(sm.group(1)) if sm else 0, "range": ([x for x in re.findall(r'([\d,]+-[\d,]+\+?|[\d,]+\+) employees', body)] or [""])[0]}
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', body, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        for node in (d.get("@graph") if isinstance(d, dict) else None) or []:
            if isinstance(node, dict) and node.get("@type") == "SocialMediaPosting":
                a = node.get("author") or {}
                out.append({"name": a.get("name", ""), "url": a.get("url", ""),
                            "post_url": (node.get("url") or node.get("mainEntityOfPage") or "").split("?")[0],
                            "text": unicodedata.normalize("NFKC", node.get("text", "") or ""),
                            "date": (node.get("datePublished") or "")[:10]})
    return out, 200


def collect_linkedin_companies(rules: dict) -> list[dict]:
    """Hiring posts that companies publish on their own LinkedIn page. The company list = every company seen hiring
    JavaScript people on LinkedIn Jobs; each full scan checks the least-recently-checked ones."""
    per_run = int(os.environ.get("LI_COMPANIES_PER_RUN", "300"))
    conn = get_db()
    rows = conn.execute("SELECT slug, name FROM li_companies WHERE failures < 4 ORDER BY last_checked ASC LIMIT ?", (per_run,)).fetchall()
    known = {r[0] for r in conn.execute("SELECT post_url FROM posts")}
    conn.close()
    out: list[dict] = []
    blocked = 0
    seen_posts = 0
    max_days = rules.get("freshness", {}).get("max_age_days", 40)
    for r in rows:
        posts, code = _li_company_page_posts(r["slug"])
        conn = get_db()
        if posts is None:
            blocked = blocked + 1 if code in (429, 999) else 0
            conn.execute("UPDATE li_companies SET failures=failures+?, last_checked=? WHERE slug=?",
                         (0 if code in (429, 999) else 1, now_utc(), r["slug"]))
            conn.commit(); conn.close()
            if blocked >= 3:
                print("[linkedin-companies] LinkedIn is rate-limiting this IP — stopping early")
                break
            time.sleep(2)
            continue
        blocked = 0
        found = 0
        for p in posts:
            seen_posts += 1
            if not p["text"] or p["post_url"] in known or (p["date"] and (days_ago(p["date"]) or 0) > max_days):
                continue
            if is_staffing_company(p["name"], rules):
                continue
            out.append({"source": "LinkedIn", "poster_name": p["name"] or r["name"], "poster_role": "Company page",
                        "poster_url": p["url"] or f"https://www.linkedin.com/company/{r['slug']}", "post_url": p["post_url"],
                        "post_text": p["text"][:1800], "skills_found": _extract_skills(p["text"], rules), "location": "",
                        "posted_at": p["date"], "_headline": None})
            found += 1
        conn.execute("UPDATE li_companies SET last_checked=?, posts_seen=posts_seen+?, hiring_found=hiring_found+? WHERE slug=?",
                     (now_utc(), len(posts), found, r["slug"]))
        conn.commit(); conn.close()
        time.sleep(1.0)
    print(f"[linkedin-companies] {len(rows)} company pages → {seen_posts} posts read, {len(out)} new candidates")
    return out


def _apify_budget_ok(min_usd: float = 0.8) -> bool:
    """Never burn the free monthly credit: skip the paid pull when less than `min_usd` is left."""
    try:
        d = http_get(f"https://api.apify.com/v2/users/me/limits?token={APIFY_TOKEN}", timeout=20, quiet=True)
        data = d.get("data", {}) if isinstance(d, dict) else {}
        left = float(data["limits"]["maxMonthlyUsageUsd"]) - float(data["current"]["monthlyUsageUsd"])
        print(f"[apify] ${left:.2f} of the monthly credit left")
        return left >= min_usd
    except Exception:
        return False


def collect_linkedin_apify(rules: dict) -> list[dict]:
    if not APIFY_TOKEN or not _due("apify_li_posts", APIFY_POST_HOURS) or not _apify_budget_ok():
        return []
    # each query is run per author group; spend is capped per run (see apify_run)
    per_q = max(10, int(LI_MAX_POSTS * 0.6))
    print(f"[linkedin] Apify post search: {len(LI_POST_QUERIES)} queries × {len(LI_AUTHOR_GROUPS)} author groups …")
    items: list[dict] = []
    seen: set[str] = set()
    for grp in LI_AUTHOR_GROUPS:
        got = apify_run("harvestapi/linkedin-post-search", {
            "searchQueries": LI_POST_QUERIES, "maxPosts": per_q, "postedLimit": "month", "sortBy": "date",
            "authorKeywords": grp, "profileScraperMode": "short"}, max_usd=0.6)
        for it in got:
            k = it.get("linkedinUrl") or it.get("id") or ""
            if k and k not in seen:
                seen.add(k)
                items.append(it)
        print(f"    group '{grp}': {len(got)} posts")
    if items:
        _kv_set("apify_li_posts", now_utc())
        if os.environ.get("APIFY_DUMP"):
            Path(os.environ["APIFY_DUMP"]).write_text(json.dumps(items), encoding="utf-8")
    out = [p for p in (_li_item_to_post(i, rules) for i in items) if p]
    print(f"  → {len(items)} unique raw posts, {len(out)} parsed (people only)")
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
    '"founder" OR "cto" "hiring" "full stack engineer" "typescript" OR "javascript" "react" "node" india',
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
            if not stack_hits(combined, rules):
                continue
            if not _is_valid_poster(combined):
                continue
            source = ("LinkedIn" if "linkedin.com" in url
                      else "X/Twitter" if "x.com" in url or "twitter.com" in url
                      else "Web")
            out.append({
                "source":      source,
                "poster_name": title[:80],
                "poster_role": "",
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
#  ATS BOARD SOURCES  (Greenhouse · Lever · Ashby public job-board APIs)
#  Links from these are DIRECT apply links by construction, and the APIs
#  return the full JD. Boards are auto-discovered from every other source.
# ═══════════════════════════════════════════════════════════════════════════

_ATS_PATTERNS = {
    "greenhouse": re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/([a-z0-9_-]+)", re.I),
    "lever":      re.compile(r"jobs\.lever\.co/([a-z0-9_.-]+)", re.I),
    "ashby":      re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_.%-]+)", re.I),
    "keka":       re.compile(r"//([a-z0-9-]+)\.keka\.com/careers", re.I),
    "zoho":       re.compile(r"//([a-z0-9-]+\.zohorecruit\.(?:in|com|eu))", re.I),
}
_BAD_TOKENS = {"embed", "jobs", "api", "v1", "boards"}


def ats_board_from_url(url: str) -> tuple[str, str] | None:
    for kind, rx in _ATS_PATTERNS.items():
        m = rx.search(url or "")
        if m and m.group(1).lower() not in _BAD_TOKENS:
            tok = m.group(1).lower()
            if kind == "zoho":
                tok = tok.replace(".zohorecruit.", ".")            # "kissflow.zohorecruit.in" → "kissflow.in"
            return kind, tok
    return None


_GENERIC_SUB = {"careers", "jobs", "apply", "www", "job", "career", "hire", "hiring", "join", "work", "talent", "boards",
                "ats", "app", "recruit", "people", "team", "en", "in"}
_PATH_SLUG_HOSTS = ("apply.workable.com", "jobs.smartrecruiters.com", "careers.smartrecruiters.com", "ats.rippling.com",
                    "jobs.gem.com", "jobs.jobvite.com", "app.trinethire.com", "jobs.polymer.co")
_SUB_SLUG_DOMAINS = ("bamboohr.com", "applytojob.com", "breezy.hr", "recruitee.com", "teamtailor.com", "workable.com",
                     "myworkdayjobs.com", "keka.com", "freshteam.com", "zohorecruit.com", "pinpointhq.com")


def company_from_url(url: str) -> str:
    b = ats_board_from_url(url)
    if b:
        return b[1].split(".")[0].replace("-", " ").replace("_", " ").title()
    host = _netloc(url)
    if host in _PATH_SLUG_HOSTS:
        seg = next((x for x in urllib.parse.urlparse(url).path.split("/") if x), "")
        if seg:
            return seg.replace("-", " ").replace("_", " ").title()
    parts = host.split(".")
    if any(host.endswith("." + d) for d in _SUB_SLUG_DOMAINS) and len(parts) > 2:
        return parts[0].replace("-", " ").title()
    labels = [x for x in parts[:-1] if x not in _GENERIC_SUB]
    # registrable label (handles co.in / com.au style suffixes)
    if len(labels) >= 2 and labels[-1] in ("co", "com", "org", "net", "gov"):
        labels = labels[:-1]
    return (labels[-1] if labels else parts[0]).replace("-", " ").title()


def register_board(conn: sqlite3.Connection, url: str, source: str = "") -> None:
    b = ats_board_from_url(url)
    if not b:
        return
    try:
        conn.execute("INSERT OR IGNORE INTO ats_boards (kind, token, source, added_at) VALUES (?,?,?,?)",
                     (b[0], b[1], source, now_utc()))
        conn.commit()
    except sqlite3.Error:
        pass


def _board_failed(kind: str, token: str, failed: bool) -> None:
    conn = get_db()
    try:
        if failed:
            conn.execute("UPDATE ats_boards SET failures=failures+1 WHERE kind=? AND token=?", (kind, token))
        else:
            conn.execute("UPDATE ats_boards SET failures=0 WHERE kind=? AND token=?", (kind, token))
        conn.commit()
    finally:
        conn.close()


def _prefilter(title: str, company: str, url: str, loc: str, rules: dict) -> bool:
    if not title_relevant(title):
        return False
    ok, _ = passes_hard_filters(title, company, url, "", rules)
    if not ok:
        return False
    return location_ok(loc, rules)[0]


def _fetch_board(board: tuple[str, str], rules: dict) -> list[dict]:
    kind, token = board
    out: list[dict] = []
    max_days = rules.get("freshness", {}).get("max_age_days", 40)
    company = token.split(".")[0].replace("-", " ").replace("_", " ").title()

    if kind == "greenhouse":
        raw = http_get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", timeout=25)
        jobs = raw.get("jobs") if isinstance(raw, dict) else None
        _board_failed(kind, token, jobs is None)
        for j in jobs or []:
            title, url = j.get("title", ""), j.get("absolute_url", "")
            loc = (j.get("location") or {}).get("name", "")
            if not url or not _prefilter(title, company, url, loc, rules):
                continue
            out.append(_job(company, title, url, "ats:greenhouse", rules, location=loc,
                            date=(j.get("updated_at") or "")[:10],
                            jd_api=f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{j.get('id')}",
                            reason=f"Greenhouse · {token}"))
    elif kind == "lever":
        raw = http_get(f"https://api.lever.co/v0/postings/{token}?mode=json", timeout=25)
        _board_failed(kind, token, not isinstance(raw, list))
        for j in raw if isinstance(raw, list) else []:
            title, url = j.get("text", ""), j.get("hostedUrl", "")
            cats = j.get("categories") or {}
            loc = cats.get("location", "") or " / ".join(cats.get("allLocations", []) or [])
            if not url or not _prefilter(title, company, url, loc, rules):
                continue
            lists = " ".join(f"{x.get('text','')} {_html_to_text(x.get('content',''))}" for x in j.get("lists", []))
            jd = "\n".join([j.get("descriptionPlain", ""), lists, j.get("additionalPlain", "")])
            ms = j.get("createdAt") or 0
            date_s = datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d") if ms else ""
            out.append(_job(company, title, url, "ats:lever", rules, location=loc, date=date_s,
                            jd_text=jd, reason=f"Lever · {token}"))
    elif kind in ("keka", "zoho"):
        jobs = keka_jobs(token) if kind == "keka" else zoho_jobs(token)
        _board_failed(kind, token, jobs is None)
        for j in jobs or []:
            if not j["url"] or not _prefilter(j["title"], company, j["url"], j["location"], rules) or not _fresh_enough(j["date"], rules):
                continue
            out.append(_job(company.split(".")[0], j["title"], j["url"], f"ats:{kind}", rules, location=j["location"], date=j["date"],
                            jd_text=(j["jd"] + (f"\nExperience: {j['exp']}" if j["exp"] else "")), reason=f"{kind.title()} · {token}"))
    elif kind == "ashby":
        raw = http_get(f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true", timeout=25)
        jobs = raw.get("jobs") if isinstance(raw, dict) else None
        _board_failed(kind, token, jobs is None)
        for j in jobs or []:
            if j.get("isListed") is False:
                continue
            title, url = j.get("title", ""), j.get("jobUrl") or j.get("applyUrl", "")
            loc = j.get("location", "") or ""
            if j.get("isRemote") and "remote" not in loc.lower():
                loc = (loc + " / Remote").strip(" /")
            if not url or not _prefilter(title, company, url, loc, rules):
                continue
            comp = (j.get("compensation") or {}).get("compensationTierSummary", "") or ""
            out.append(_job(company, title, url, "ats:ashby", rules, location=loc,
                            date=(j.get("publishedAt") or "")[:10], salary=comp,
                            jd_text=j.get("descriptionPlain") or _html_to_text(j.get("descriptionHtml", "")),
                            reason=f"Ashby · {token}"))
    return out


_DATASET_PAGES = ("https://datahub.io/lastroundai-hiring-data/lastroundai-hiring-data/ats-directory",)
_DATASET_CSV = "https://datahub.io/lastroundai-hiring-data/lastroundai-hiring-data/_r/-/ats-directory/lastroundai-ats-company-directory-2026-08.csv"
_OPEN_JOBS_JSON = "https://raw.githubusercontent.com/ConorsCode/open-jobs-data/main/companies.json"


def import_board_datasets(conn: sqlite3.Connection) -> int:
    """Public directories of companies' Greenhouse / Lever / Ashby / Workday boards (LastRound AI ATS directory ~10k,
    open-jobs-data). Dead boards drop out by themselves after 3 failed polls."""
    import csv, io
    rows: list[tuple[str, str]] = []
    wd: list[tuple[str, str, str]] = []
    csv_url = _DATASET_CSV
    for pg in _DATASET_PAGES:                                   # newest monthly file if the page lists one
        h = http_get_text(pg, 30, quiet=True) or ""
        links = sorted(set(re.findall(r'https://datahub\.io/[^"\s]+ats-directory/[^"\s]+\.csv', h)))
        if links:
            csv_url = links[-1]
    try:
        req = urllib.request.Request(csv_url, headers={"User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=90) as r:
            for row in csv.DictReader(io.StringIO(r.read().decode("utf-8", "ignore"))):
                k, t = (row.get("ats_vendor") or "").strip().lower(), (row.get("board_slug") or "").strip().lower()
                if k in ("greenhouse", "lever", "ashby") and t and t not in _BAD_TOKENS:
                    rows.append((k, t))
    except Exception as exc:
        print(f"  [datasets ✗] lastround: {exc}")
    d = http_get(_OPEN_JOBS_JSON, timeout=40, quiet=True)
    for o in d if isinstance(d, list) else []:
        k = (o.get("platform") or "").lower()
        if k in ("greenhouse", "lever", "ashby") and o.get("slug"):
            rows.append((k, o["slug"].lower()))
        elif k == "workday" and isinstance(o.get("workday"), dict):
            w = o["workday"]
            if w.get("tenant") and w.get("site") and w.get("shard"):
                wd.append((f"{w['tenant']}.{w['shard']}.myworkdayjobs.com".lower(), w["tenant"].lower(), w["site"]))
    n = 0
    stamp = now_utc()
    for k, t in rows:
        n += conn.execute("INSERT OR IGNORE INTO ats_boards (kind, token, source, added_at) VALUES (?,?,?,?)", (k, t, "dataset", stamp)).rowcount
    for b in wd:
        n += conn.execute("INSERT OR IGNORE INTO workday_boards (host, tenant, site, source) VALUES (?,?,?,?)", (*b, "dataset")).rowcount
    conn.commit()
    print(f"[datasets] {len(rows)} ATS boards + {len(wd)} Workday sites listed → {n} new")
    return n if rows else -1                                   # -1 = download failed → retry next scan


def collect_ats_boards(rules: dict) -> list[dict]:
    """Polls company boards in rotation (least recently polled first) so the list can grow to thousands."""
    conn = get_db()
    if _due("board_datasets_at", 24 * 7):
        try:
            if import_board_datasets(conn) >= 0:
                _kv_set("board_datasets_at", now_utc())
        except Exception as exc:
            print(f"  [datasets ✗] {exc}")
    for kind, toks in rules.get("ats_boards", {}).items():
        if isinstance(toks, list):
            for t in toks:
                conn.execute("INSERT OR IGNORE INTO ats_boards (kind, token, source, added_at) VALUES (?,?,?,?)", (kind, t.lower(), "seed", now_utc()))
    conn.commit()
    limit = int(os.environ.get("BOARDS_PER_RUN", "2500" if SCAN_MODE != "light" else "800"))
    total = conn.execute("SELECT COUNT(*) FROM ats_boards WHERE failures < 3").fetchone()[0]
    boards = [(r["kind"], r["token"]) for r in conn.execute(
        "SELECT kind, token FROM ats_boards WHERE failures < 3 ORDER BY IFNULL(last_polled,'') ASC LIMIT ?", (limit,))]
    stamp = now_utc()
    conn.executemany("UPDATE ats_boards SET last_polled=? WHERE kind=? AND token=?", [(stamp, k, t) for k, t in boards])
    conn.commit()
    conn.close()
    print(f"[ats-boards] polling {len(boards)} of {total} company boards (rotation)...")
    with ThreadPoolExecutor(20) as ex:
        results = list(ex.map(lambda b: _fetch_board(b, rules), boards))
    out = [j for r in results for j in r]
    print(f"  → {len(out)} candidate roles (India/remote-eligible, title-filtered)")
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  WORKDAY  (huge companies; the public CXS JSON API returns the full JD without a browser)
# ═══════════════════════════════════════════════════════════════════════════
_WD_URL = re.compile(r"https?://([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)", re.I)
_WD_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}
_WD_QUERIES = ["software engineer", "developer", "react", "node", "javascript", "full stack"]
_WD_NAMES = {"jiostar": "JioStar", "capitalone": "Capital One", "americanexpress": "American Express", "thomsonreuters": "Thomson Reuters",
             "redhat": "Red Hat", "paypal": "PayPal", "hp": "HP", "ge": "GE", "netapp": "NetApp", "blackrock": "BlackRock"}


_HOST_LAST: dict[str, float] = {}
_HOST_LOCK = threading.Lock()


def _host_gap(host: str, gap: float) -> None:
    """Block until at least `gap` seconds passed since the last request to this host (shared by all threads)."""
    while True:
        with _HOST_LOCK:
            now = time.time()
            wait = _HOST_LAST.get(host, 0) + gap - now
            if wait <= 0:
                _HOST_LAST[host] = now
                return
        time.sleep(min(wait, gap))


def workday_board_from_url(url: str) -> tuple[str, str, str] | None:
    m = _WD_URL.match(url or "")
    if not m or m.group(3).lower() in ("wday", "en-us"):
        return None
    return f"{m.group(1)}.{m.group(2)}.myworkdayjobs.com".lower(), m.group(1).lower(), m.group(3)


def register_workday(conn: sqlite3.Connection, url: str, source: str = "") -> None:
    b = workday_board_from_url(url)
    if b:
        conn.execute("INSERT OR IGNORE INTO workday_boards (host, tenant, site, source) VALUES (?,?,?,?)", (*b, source))
        conn.commit()


def _fetch_workday(board: tuple[str, str, str], rules: dict) -> list[dict]:
    host, tenant, site = board
    company = _WD_NAMES.get(tenant, tenant.replace("-", " ").title())
    out: dict[str, dict] = {}
    deadline = time.time() + 75                                   # never let one slow / rate-limited tenant stall the scan
    throttled = 0
    for q in _WD_QUERIES:
        for offset in (0, 20):
            if time.time() > deadline or throttled >= 2:
                break
            d = http_post(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                          {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": q},
                          timeout=20, extra_headers=_WD_HEADERS)
            if not isinstance(d, dict) or d.get("jobPostings") is None:
                throttled += 1                                    # 429 / blocked: back off, try the next query once
                time.sleep(3)
                break
            posts = d["jobPostings"]
            for j in posts:
                path, title, loc = j.get("externalPath", ""), j.get("title", ""), j.get("locationsText", "")
                if not path or path in out:
                    continue
                multi = bool(re.match(r"\d+\s+locations", loc or "", re.I))
                if not _prefilter(title, company, f"https://{host}/{site}{path}", "" if multi else loc, rules):
                    continue
                out[path] = _job(company, title, f"https://{host}/{site}{path}", "ats:workday", rules, location="" if multi else loc,
                                 jd_wd=f"https://{host}/wday/cxs/{tenant}/{site}{path}", reason=f"Workday · {tenant}")
            if len(posts) < 20:
                break
            time.sleep(0.3)
    _wd_failed(board, throttled >= 2 and not out)
    return list(out.values())


def _wd_failed(board: tuple[str, str, str], failed: bool) -> None:
    conn = get_db()
    try:
        conn.execute("UPDATE workday_boards SET failures=? WHERE host=? AND tenant=? AND site=?",
                     ((conn.execute("SELECT failures FROM workday_boards WHERE host=? AND tenant=? AND site=?", board).fetchone() or [0])[0] + 1 if failed else 0, *board))
        conn.commit()
    finally:
        conn.close()


def collect_workday(rules: dict) -> list[dict]:
    conn = get_db()
    for b in rules.get("workday_boards", []):
        conn.execute("INSERT OR IGNORE INTO workday_boards (host, tenant, site, source) VALUES (?,?,?,?)",
                     (b["host"], b["tenant"], b["site"], "seed"))
    conn.commit()
    lim = int(os.environ.get("WORKDAY_PER_RUN", "120" if SCAN_MODE == "light" else "400"))
    boards = [(r["host"], r["tenant"], r["site"]) for r in conn.execute(
        "SELECT host, tenant, site FROM workday_boards WHERE failures < 4 ORDER BY IFNULL(last_polled,'') ASC LIMIT ?", (lim,))]
    stamp = now_utc()
    conn.executemany("UPDATE workday_boards SET last_polled=? WHERE host=? AND tenant=? AND site=?", [(stamp, *b) for b in boards])
    conn.commit()
    conn.close()
    print(f"[workday] polling {len(boards)} Workday career sites ...")
    with ThreadPoolExecutor(5) as ex:
        results = list(ex.map(lambda b: _fetch_workday(b, rules), boards))
    out = [j for r in results for j in r]
    print(f"  → {len(out)} candidate roles")
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  DIRECT-LINK RESOLUTION  (company/ATS page for a role found on a job board)
# ═══════════════════════════════════════════════════════════════════════════

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_BAD_MAIL = ("noreply", "no-reply", "donotreply", "do-not-reply", "privacy@", "legal@", "abuse@", "press@",
             "support@", "billing@", "sales@", "example.", "sentry", "unsubscribe", "webmaster@", "notifications@",
             "accessibility@", "dpo@", "security@", "compliance@")
_MAIL_CTX = re.compile(r"apply|send|e-?mail|mail|resume|r[eé]sum[eé]|\bcv\b|reach|contact|submit|application|"
                       r"interested|share|drop|write to|forward|hiring|careers?|jobs?", re.I)


_STRONG_MAIL_CTX = re.compile(r"send|apply|resume|r[eé]sum[eé]|\bcv\b|submit|application|e-?mail (?:your|us|me|at|to)|"
                              r"drop (?:us|your)|forward|write to|interested candidates", re.I)
_BAD_DOMAINS = ("email.com", "domain.com", "yourcompany", "company.com", "mail.com", "example.")


def find_apply_email(text: str, own_host: str = "") -> str | None:
    """An address the JD tells candidates to send their application to (never the job board's own address)."""
    t = text or ""
    for m in _EMAIL_RE.finditer(t):
        e = m.group(0).lower().rstrip(".")
        dom = e.split("@", 1)[1]
        if e.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".woff", ".css", ".js")) or any(b in e for b in _BAD_MAIL):
            continue
        if any(b in dom for b in _BAD_DOMAINS) or e.split("@")[0] in ("you", "name", "yourname", "your.name", "user", "accessibility", "dpo", "privacy"):
            continue
        if any(dom == a or dom.endswith("." + a) for a in AGGREGATOR_HOSTS) or (own_host and (dom == own_host or dom.endswith("." + own_host))):
            continue
        if _STRONG_MAIL_CTX.search(t[max(0, m.start() - 140): m.end() + 40]):
            return e
    return None


def _posting_text(url: str) -> str:
    st, _lk, text, _f, _a = check_page(url)
    if len(text or "") >= 300:
        return text
    return _fetch_page(url)[0]


_SLUG_STOP = {"inc", "llc", "ltd", "limited", "pvt", "private", "corp", "corporation", "co", "company", "technologies",
              "technology", "labs", "lab", "software", "solutions", "systems", "the", "group", "global", "india",
              "hq", "ai", "io", "app", "apps"}


def _slug_candidates(company: str) -> list[str]:
    words = [w for w in re.sub(r"[^a-z0-9 ]+", " ", (company or "").lower().replace("&", " and ")).split() if w]
    core = [w for w in words if w not in _SLUG_STOP] or words
    out: list[str] = []
    for c in ("".join(words), "".join(core), "-".join(core), core[0] if core else "", "".join(core[:2])):
        if c and len(c) > 2 and c not in out:
            out.append(c)
    return out[:4]


# ── Keka & Zoho Recruit (very common with Indian startups) — public job lists with the full JD ─────────
_KEKA_GUID: dict[str, str | None] = {}


def keka_jobs(sub: str) -> list[dict] | None:
    """[{title, url, location, jd, exp, date}] from <sub>.keka.com, or None when it isn't a Keka customer."""
    if sub not in _KEKA_GUID:
        h = http_get_text(f"https://{sub}.keka.com/careers/", 15, quiet=True)
        m = re.search(r"/ats/documents/([0-9a-f-]{36})/careerportal", h or "")
        _KEKA_GUID[sub] = m.group(1) if m else None
    guid = _KEKA_GUID[sub]
    if not guid:
        return None
    d = http_get(f"https://{sub}.keka.com/careers/api/embedjobs/default/active/{guid}", timeout=20, quiet=True)
    if not isinstance(d, list):
        return None
    out = []
    for j in d:
        locs = j.get("jobLocations") or []
        loc = " / ".join(", ".join(x for x in (l.get("city"), l.get("state"), l.get("countryCode")) if x) for l in locs if isinstance(l, dict))
        out.append({"title": j.get("title", ""), "url": f"https://{sub}.keka.com/careers/jobdetails/{j.get('id')}",
                    "location": loc, "jd": _html_to_text(j.get("description") or ""), "exp": j.get("experience") or "",
                    "date": (j.get("publishedOn") or "")[:10]})
    return out


def zoho_jobs(token: str) -> list[dict] | None:
    """token = "<sub>.in" / "<sub>.com" (or a bare name: both domains are tried)."""
    hosts = [f"{token.rsplit('.', 1)[0]}.zohorecruit.{token.rsplit('.', 1)[1]}"] if "." in token else \
            [f"{token}.zohorecruit.in", f"{token}.zohorecruit.com"]
    for host in hosts:
        d = http_get(f"https://{host}/recruit/v2/public/Job_Openings?pagename=Careers&source=CareerSite", timeout=20, quiet=True)
        if isinstance(d, dict) and d.get("code") == "success":
            out = []
            for j in d.get("data") or []:
                if str(j.get("Publish", "True")) == "False":
                    continue
                loc = ", ".join(x for x in (j.get("City"), j.get("State"), j.get("Country")) if x)
                if str(j.get("Remote_Job", "")).lower() == "true":
                    loc = (loc + " / Remote").strip(" /")
                try:
                    date = datetime.strptime(j.get("Date_Opened", ""), "%m/%d/%Y").strftime("%Y-%m-%d")
                except ValueError:
                    date = ""
                out.append({"title": j.get("Posting_Title") or j.get("Job_Opening_Name", ""), "url": j.get("$url", ""),
                            "location": loc, "jd": _html_to_text(j.get("Job_Description") or ""),
                            "exp": str(j.get("Work_Experience") or "").replace("None", ""), "date": date})
            return out
    return None


_ATS_LIST_CACHE: dict[tuple[str, str], tuple[float, list | None]] = {}


_ATS_DATES: dict = {}


def _remember_dates(kind: str, items) -> None:
    for j in items or []:
        try:
            if kind == "greenhouse":
                u, d = j.get("absolute_url", ""), (j.get("first_published") or j.get("updated_at") or "")[:10]
            elif kind == "lever":
                u, ms = j.get("hostedUrl", ""), j.get("createdAt")
                d = datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d") if ms else ""
            elif kind == "ashby":
                u, d = j.get("jobUrl") or j.get("applyUrl", ""), (j.get("publishedAt") or "")[:10]
            elif kind == "recruitee":
                u, d = j.get("careers_url", ""), (j.get("published_at") or j.get("created_at") or "")[:10]
            elif kind == "workable":
                u, d = j.get("url") or j.get("shortlink", ""), (j.get("published_on") or j.get("created_at") or "")[:10]
            else:
                continue
            if u and d:
                _ATS_DATES[u] = d
        except Exception:
            continue


def _ats_postings(kind: str, slug: str) -> list[tuple[str, str, str]] | None:
    """[(title, url, location)] for a public ATS board, or None when the board doesn't exist (cached 1h / 10min)."""
    key = (kind, slug)
    hit = _ATS_LIST_CACHE.get(key)
    if hit and time.time() - hit[0] < (3600 if hit[1] else 600):
        return hit[1]
    res: list[tuple[str, str, str]] | None = None
    try:
        if kind == "greenhouse":
            d = http_get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=15, quiet=True)
            if isinstance(d, dict) and "jobs" in d:
                _remember_dates("greenhouse", d["jobs"])
                res = [(j.get("title", ""), j.get("absolute_url", ""), (j.get("location") or {}).get("name", "")) for j in d["jobs"]]
        elif kind == "lever":
            d = http_get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=15, quiet=True)
            if isinstance(d, list):
                _remember_dates("lever", d)
                res = [(j.get("text", ""), j.get("hostedUrl", ""), (j.get("categories") or {}).get("location", "") or "") for j in d]
        elif kind == "ashby":
            d = http_get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=15, quiet=True)
            if isinstance(d, dict) and "jobs" in d:
                _remember_dates("ashby", d["jobs"])
                res = [(j.get("title", ""), j.get("jobUrl") or j.get("applyUrl", ""),
                        (j.get("location", "") or "") + (" / Remote" if j.get("isRemote") else "")) for j in d["jobs"]]
        elif kind == "recruitee":
            d = http_get(f"https://{slug}.recruitee.com/api/offers/", timeout=15, quiet=True)
            if isinstance(d, dict) and "offers" in d:
                _remember_dates("recruitee", d["offers"])
                res = [(j.get("title", ""), j.get("careers_url", ""), j.get("location", "") or "") for j in d["offers"]]
        elif kind == "smartrecruiters":
            d = http_get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100", timeout=15, quiet=True)
            if isinstance(d, dict) and d.get("totalFound", 0) > 0:
                res = [(j.get("name", ""), f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
                        (j.get("location") or {}).get("city", "") + " " + (j.get("location") or {}).get("country", "")) for j in d.get("content", [])]
        elif kind == "bamboohr":
            d = http_get(f"https://{slug}.bamboohr.com/careers/list", timeout=15, quiet=True)
            if isinstance(d, dict) and isinstance(d.get("result"), list):
                res = [(j.get("jobOpeningName", ""), f"https://{slug}.bamboohr.com/careers/{j.get('id')}",
                        ((j.get("location") or {}).get("city") or "") + " " + ((j.get("location") or {}).get("state") or "")) for j in d["result"]]
        elif kind == "breezy":
            d = http_get(f"https://{slug}.breezy.hr/json", timeout=15, quiet=True)
            if isinstance(d, list) and d:
                res = [(j.get("name", ""), j.get("url", ""), ((j.get("location") or {}).get("name") or "")) for j in d]
        elif kind == "keka":
            kj = keka_jobs(slug)
            res = [(j["title"], j["url"], j["location"]) for j in kj] if kj is not None else None
        elif kind == "zoho":
            zj = zoho_jobs(slug)
            res = [(j["title"], j["url"], j["location"]) for j in zj] if zj is not None else None
        elif kind == "workable":
            d = http_get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", timeout=15, quiet=True)
            if isinstance(d, dict) and "jobs" in d:
                _remember_dates("workable", d["jobs"])
                res = [(j.get("title", ""), j.get("url") or j.get("shortlink", ""), j.get("city", "") + " " + j.get("country", "")) for j in d["jobs"]]
    except Exception:
        res = None
    _ATS_LIST_CACHE[key] = (time.time(), res)
    return res


_LEVEL_TOKENS = re.compile(r"\b(senior|sr|staff|principal|lead|junior|jr|intern|manager|head|architect|associate|ii|iii|iv|2|3|4|5)\b")


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def resolve_direct(company: str, title: str, rules: dict | None = None) -> tuple[str, str] | None:
    """Company's own posting on a public ATS board → (url, ATS location). The full title must match, seniority tokens must
    be identical, and an India-eligible posting is preferred when the company lists the same role in several places."""
    import difflib
    want = _norm_title(title)
    if not want:
        return None
    want_lv = set(_LEVEL_TOKENS.findall(want))
    for slug in _slug_candidates(company):
        for kind in ("greenhouse", "lever", "ashby", "workable", "recruitee", "smartrecruiters", "bamboohr", "breezy", "keka", "zoho"):
            posts = _ats_postings(kind, slug)
            if not posts:
                continue
            cands = []
            for t, u, loc in posts:
                if not u:
                    continue
                nt = _norm_title(t)
                if set(_LEVEL_TOKENS.findall(nt)) != want_lv:
                    continue
                r = difflib.SequenceMatcher(None, want, nt).ratio()
                if r >= 0.95:
                    cands.append((r, u, loc))
            if cands:
                ok_loc = [c for c in cands if not rules or location_ok(c[2], rules)[0]]
                pick = max(ok_loc or cands, key=lambda c: c[0])
                return pick[1], pick[2]
    return None


_SITE_RES_CACHE: dict[tuple[str, str], tuple[str, str] | None] = {}


def resolve_via_site(company: str, title: str, rules: dict | None = None) -> tuple[str, str] | None:
    """Fallback for companies on no supported ATS: open their own website's careers page, find the exact job link
    (or an ATS board linked from it) → (url, location)."""
    import difflib
    key = (_company_key(company), _norm_title(title))
    if key in _SITE_RES_CACHE:
        return _SITE_RES_CACHE[key]
    result = None
    try:
        found = _find_company_site(company)
        if found:
            base, home = found
            want = _norm_title(title)
            want_lv = set(_LEVEL_TOKENS.findall(want))
            pages = [(base, home)]
            for pth in ("/careers", "/jobs", "/join-us", "/company/careers", "/about/careers", "/careers/open-positions"):
                h = http_get_text(base.rstrip("/") + pth, timeout=6, quiet=True)
                if h and len(h) > 600:
                    pages.append((base.rstrip("/") + pth, h))
            if _CHROME or _sync_playwright:               # careers pages are often JS-rendered → also try the real browser
                for pth in ("/careers", "/jobs"):
                    dom = render_page(base.rstrip("/") + pth, 8000)
                    if dom and len(dom) > 1500 and not any(ch in dom.lower()[:1500] for ch in _CHALLENGE):
                        pages.append((base.rstrip("/") + pth, dom))
            for page_url, h in pages:
                for href, anchor in re.findall(r'<a\b[^>]*?href="([^"#][^"]*)"[^>]*>(.*?)</a>', h, re.S | re.I):
                    u = urllib.parse.urljoin(page_url, htmllib.unescape(href))
                    txt = _norm_title(re.sub(r"<[^>]+>", " ", htmllib.unescape(anchor)))
                    head = txt[:len(want) + 3].strip()              # card text = title + teaser; compare the title part only
                    if head and set(_LEVEL_TOKENS.findall(head)) == want_lv and (
                            head.startswith(want) or difflib.SequenceMatcher(None, want, head[:len(want)]).ratio() >= 0.95) \
                            and u.startswith("http") and not bad_apply_target(u):
                        result = (u, "")
                        break
                    ab = ats_board_from_url(u)
                    if ab and not result and ab[0] in ("greenhouse", "lever", "ashby"):
                        posts = _ats_postings(ab[0], ab[1]) or []
                        for tt, uu, loc in posts:
                            nt = _norm_title(tt)
                            if uu and set(_LEVEL_TOKENS.findall(nt)) == want_lv and difflib.SequenceMatcher(None, want, nt).ratio() >= 0.95:
                                result = (uu, loc)
                                break
                    if result:
                        break
                if result:
                    break
    except Exception:
        result = None
    _SITE_RES_CACHE[key] = result
    return result


_URL_LEVELS = {"senior", "sr", "staff", "principal", "lead", "architect", "manager", "director", "head", "intern", "internship",
               "trainee", "vp", "advisor"}


def _url_level_conflict(title: str, url: str) -> bool:
    """The company link names a seniority the role's title doesn't ('Tech-Lead-…' for 'Software Development Engineer')."""
    path = set(re.split(r"[^a-z]+", urllib.parse.urlparse(url or "").path.lower()))
    have = set(re.split(r"[^a-z]+", (title or "").lower()))
    return bool((path & _URL_LEVELS) - have)


def _same_title(want: str, got: str, ratio: float = 0.9) -> bool:
    """Same role: identical seniority tokens and ≥90% similar normalised title."""
    import difflib
    a, b = _norm_title(want), _norm_title(got)
    if not a or not b or set(_LEVEL_TOKENS.findall(a)) != set(_LEVEL_TOKENS.findall(b)):
        return False
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= ratio


def resolve_workday(company: str, title: str, rules: dict | None = None) -> tuple[str, str] | None:
    """Big companies (Accenture, Citi, PayPal …) post LinkedIn roles from Workday: search their Workday site for the title."""
    cands = set(_slug_candidates(company))
    if not cands:
        return None
    conn = get_db()
    try:
        boards = [(r["host"], r["tenant"], r["site"]) for r in conn.execute("SELECT host, tenant, site FROM workday_boards WHERE failures < 4")
                  if r["tenant"].lower() in cands or any(len(c) >= 5 and r["tenant"].lower().startswith(c) for c in cands)]
    finally:
        conn.close()
    for host, tenant, site in boards[:3]:
        d = http_post(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                      {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": title[:80]}, timeout=20, extra_headers=_WD_HEADERS)
        hits = [(j.get("title", ""), j.get("externalPath", ""), j.get("locationsText", "")) for j in (d or {}).get("jobPostings") or []] \
            if isinstance(d, dict) else []
        ok = [(t, pth, loc) for t, pth, loc in hits if pth and _same_title(title, t)]
        if ok:
            good = [o for o in ok if not rules or location_ok(o[2], rules)[0]]
            t, pth, loc = (good or ok)[0]
            return f"https://{host}/{site}{pth}", ("" if re.match(r"\d+\s+locations", loc or "", re.I) else loc)
    return None


def resolve_bigco(company: str, title: str, rules: dict | None = None) -> tuple[str, str] | None:
    """Companies with their own public job API (no ATS board): Amazon / AWS."""
    key = _company_key(company)
    if not (key.startswith("amazon") or key in ("aws", "amazonwebservices", "amazondevelopmentcentre")):
        return None
    d = http_get("https://www.amazon.jobs/en/search.json?" + urllib.parse.urlencode(
        {"base_query": title[:100], "loc_query": "India", "result_limit": 20}), timeout=20, quiet=True)
    for j in (d or {}).get("jobs", []) if isinstance(d, dict) else []:
        if j.get("job_path") and _same_title(title, j.get("title", "")):
            url = "https://www.amazon.jobs" + j["job_path"]
            try:
                _POSTED_CACHE[url] = datetime.strptime(re.sub(r"\s+", " ", j.get("posted_date", "")).strip(), "%B %d, %Y").strftime("%Y-%m-%d")
            except ValueError:
                pass
            return url, j.get("location", "")
    return None


SERPER_RESOLVE_PER_DAY = int(os.environ.get("SERPER_RESOLVE_PER_DAY", "40"))
_JOB_SITE_NOISE = ("linkedin.", "indeed.", "glassdoor.", "naukri.", "foundit.", "monster", "shine.com", "timesjobs.", "iimjobs.",
                   "hirist.", "instahyre.", "cutshort.", "wellfound.", "angel.co", "ziprecruiter.", "simplyhired.", "jooble.",
                   "talent.com", "startup.jobs", "sorce.jobs", "jobspri.", "huntyourtribe.", "builtin.", "ambitionbox.", "apna.co",
                   "workindia.", "freshersworld.", "jobsora.", "careerjet.", "adzuna.", "jobrapido.", "whatjobs.", "jobleads.",
                   "trabajo.", "learn4good.", "teal", "remoterocketship.", "himalayas.", "remoteok.", "weworkremotely.",
                   "dice.com", "levels.fyi", "6figr.", "facebook.com", "x.com", "twitter.com", "youtube.", "medium.com",
                   "saarthilms.", "jobaaj.", "jobsearch", "jobkart", "jobstreet", "jobs.ac", "jobicy.", "wayup.", "otta.", "welcometothejungle.", "jobgether.", "remotive.")
_CAREER_HOST = re.compile(r"myworkdayjobs\.com|myworkdaysite\.com|greenhouse\.io|lever\.co|ashbyhq\.com|smartrecruiters\.com|workable\.com|"
                          r"icims\.com|successfactors\.|jobs\.sap\.com|oraclecloud\.com|taleo\.net|jobvite\.com|darwinbox\.|keka\.com|"
                          r"zohorecruit\.|freshteam\.com|recruitee\.com|bamboohr\.com|teamtailor\.com|breezy\.hr|rippling\.com|"
                          r"eightfold\.ai|phenompeople\.com|avature\.net|amazon\.jobs", re.I)


def resolve_by_search(company: str, title: str, rules: dict | None = None) -> tuple[str, str] | None:
    """Last resort for a LinkedIn role: one Google search (Serper) for the exact title + company, keep only the
    company's own careers / ATS page whose text really shows this role. Capped per day; cached for 30 days."""
    if not SERPER_API_KEY:
        return None
    ck = f"res:{_company_key(company)}:{_norm_title(title)}"[:200]
    try:
        cached = _kv_get(ck)
    except Exception:
        cached = ""
    if cached:
        try:
            c = json.loads(cached)
            if (days_ago(c.get("at", "")) or 0) < 30:
                return (c["url"], c.get("loc", "")) if c.get("url") else None
        except ValueError:
            pass
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _RESOLVE_LOCK:
        used = int(_kv_get(f"serper_resolve:{day}") or 0)
        if used >= SERPER_RESOLVE_PER_DAY:
            return None
        _kv_set(f"serper_resolve:{day}", str(used + 1))
    links: list[str] = []
    try:
        req = urllib.request.Request("https://google.serper.dev/search", method="POST",
                                     data=json.dumps({"q": f'"{title}" "{company}" careers apply'}).encode(),
                                     headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            links = [x.get("link", "") for x in json.loads(r.read().decode()).get("organic", [])]
    except Exception as exc:
        print(f"  [resolve-search ✗] {str(exc)[:80]}")
    keys = [k for k in _slug_candidates(company) if len(k) >= 3]
    found = None
    for u in links[:10]:
        h = _netloc(u)
        if not u.startswith("http") or is_aggregator(u) or any(n in h for n in _JOB_SITE_NOISE) or bad_apply_target(u):
            continue
        flat = re.sub(r"[^a-z0-9]+", "", u.lower())
        own = any(k.replace("-", "") in re.sub(r"[^a-z0-9]+", "", h) for k in keys)
        if not _CAREER_HOST.search(u) and not (own and re.search(r"career|jobs?\b|join|hiring|opening|position|vacanc", u, re.I)):
            continue                                              # a known ATS, or the company's OWN site's careers page
        txt = _posting_text(u)
        if not txt or len(txt) < 300:
            continue
        low = re.sub(r"[^a-z0-9]+", "", txt.lower())
        if not any(k.replace("-", "") in flat or k.replace("-", "") in low for k in keys):
            continue                                              # page must belong to this company
        if _norm_title(title).replace(" ", "") not in re.sub(r"[^a-z0-9]+", "", _norm_title(txt[:4000])) and \
                not _same_title(title, txt.split("\n", 1)[0][:120]):
            continue                                              # and show this exact role
        found = (u, "")
        break
    try:
        _kv_set(ck, json.dumps({"url": found[0] if found else "", "at": now_utc()}))
    except Exception:
        pass
    return found


_RESOLVE_LOCK = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════
#  FULL-JD VERIFICATION
# ═══════════════════════════════════════════════════════════════════════════

def _html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", h or "")
    h = re.sub(r"(?i)</(p|li|div|h[1-6]|tr|ul|ol)>|<br\s*/?>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = htmllib.unescape(h)
    h = re.sub(r"[ \t\r\f\v]+", " ", h)
    return re.sub(r"\n\s*\n+", "\n", h).strip()


_POSTED_CACHE: dict = {}


def posting_date(url: str, hint: str = "", source: str = "") -> str:
    """When the job was FIRST published (YYYY-MM-DD), from the job system itself — never 'last updated' (Greenhouse /
    Lever refresh that and make old roles look new). '' when it can't be verified."""
    if url in _POSTED_CACHE:
        return _POSTED_CACHE[url]
    d = ""
    try:
        m = re.search(r"https?://(?:job-boards|boards)(\.eu)?\.greenhouse\.io/([^/]+)/jobs/(\d+)", url)
        gj = re.search(r"[?&]gh_jid=(\d+)", url)
        if m or (gj and source.startswith("ats:greenhouse:")):
            eu, tok, jid = (m.group(1) or "", m.group(2), m.group(3)) if m else ("", source.split(":")[2], gj.group(1))
            j = http_get(f"https://boards-api{eu}.greenhouse.io/v1/boards/{tok}/jobs/{jid}", timeout=15, quiet=True)
            if isinstance(j, dict):
                d = (j.get("first_published") or "")[:10]
        elif (m := re.search(r"https?://jobs(\.eu)?\.lever\.co/([^/]+)/([0-9a-f-]{36})", url)):
            j = http_get(f"https://api{m.group(1) or ''}.lever.co/v0/postings/{m.group(2)}/{m.group(3)}", timeout=15, quiet=True)
            if isinstance(j, dict) and j.get("createdAt"):
                d = datetime.fromtimestamp(j["createdAt"] / 1000, timezone.utc).strftime("%Y-%m-%d")
        elif (m := re.search(r"https?://jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", url)):
            _ats_postings("ashby", m.group(1))
            d = next((v for k, v in _ATS_DATES.items() if m.group(2) in k), "")
        elif (m := re.search(r"https?://jobs\.smartrecruiters\.com/([^/]+)/(\d+)", url)):
            j = http_get(f"https://api.smartrecruiters.com/v1/companies/{m.group(1)}/postings/{m.group(2)}", timeout=15, quiet=True)
            if isinstance(j, dict):
                d = (j.get("releasedDate") or "")[:10]
        elif (m := re.search(r"https?://apply\.workable\.com/([^/]+)/j/([A-Z0-9]+)", url)):
            req = urllib.request.Request(f"https://apply.workable.com/api/v2/accounts/{m.group(1)}/jobs/{m.group(2)}",
                                         headers={"User-Agent": BROWSER_UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = (json.loads(r.read()).get("published") or "")[:10]
        elif (m := re.search(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)(/job/.+?)(?:[?#]|$)", url, re.I)) or \
                (m2 := re.search(r"https?://(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([^/]+)/([^/]+)(/job/.+?)(?:[?#]|$)", url, re.I)):
            if m:
                api = f"https://{m.group(1)}.{m.group(2)}.myworkdayjobs.com/wday/cxs/{m.group(1)}/{m.group(3)}{m.group(4)}"
            else:
                api = f"https://{m2.group(1)}.myworkdaysite.com/wday/cxs/{m2.group(2)}/{m2.group(3)}{m2.group(4)}"
            j = None
            for attempt in range(3):
                j = http_get(api, timeout=20, quiet=True)
                if isinstance(j, dict):
                    break
                time.sleep(3 + attempt * 4)
            if isinstance(j, dict):
                d = ((j.get("jobPostingInfo") or {}).get("startDate") or "")[:10]
        elif "linkedin.com/jobs/view" in url:
            d = (hint or "")[:10]
        else:                                                    # company careers page / other ATS: JSON-LD datePosted
            h = http_get_text(url, 20, quiet=True) or ""
            mm = re.search(r'"datePosted"\s*:\s*"(\d{4}-\d{2}-\d{2})', h)
            if mm:
                d = mm.group(1)
            else:
                ago = re.search(r"posted\s+(\d+)\s+days?\s+ago", h, re.I)
                if ago:
                    d = (datetime.now(timezone.utc) - timedelta(days=int(ago.group(1)))).strftime("%Y-%m-%d")
                elif re.search(r"posted\s+(?:today|yesterday|just now|\d+\s+hours?\s+ago)", h, re.I):
                    d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        d = ""
    _POSTED_CACHE[url] = d
    return d


def verify_candidate(job: dict, rules: dict) -> tuple[dict | None, str]:
    """Open the job, make sure it is still active, read the FULL JD, find the best apply link,
    then apply every rule. Returns (job, 'ok') or (None, reason)."""
    url = job["direct_apply_url"]
    if not url or looks_like_listing(url):
        return None, "listing page, not a single job"
    src = job.get("discovery_source", "")
    slug = urllib.parse.urlparse(url).path.lower()
    m = re.search(r"(?<![a-z0-9])(senior|sr|staff|principal|lead|architect|head-of|director|intern|internship|trainee)(?![a-z0-9])", slug)
    if m:
        return None, f"Seniority in URL ('{m.group(1)}')"

    # ── 1. full JD ─────────────────────────────────────────────────────────
    jd = job.get("jd_text") or ""
    md, links = "", []
    if len(jd) < 300 and job.get("jd_wd"):
        d = {}
        for attempt in range(4):                                   # polite: one request per second per tenant, back off on 429
            _host_gap(_netloc(job["jd_wd"]), 1.1)
            d = http_get(job["jd_wd"], timeout=25, quiet=True)
            if isinstance(d, dict) and d.get("jobPostingInfo"):
                break
            time.sleep(3 + attempt * 4)
        info_wd = d.get("jobPostingInfo", {}) if isinstance(d, dict) else {}
        if not info_wd:
            return None, "full JD unreadable (Workday rate limit — retried next scan)"
        if info_wd.get("jobDescription"):
            jd = _html_to_text(htmllib.unescape(info_wd["jobDescription"]))
            extra_loc = " ; ".join(x for x in [info_wd.get("location", "")] + list(info_wd.get("additionalLocations") or []) if x)
            if extra_loc and not job.get("location"):
                job["location"] = extra_loc[:140]
            if info_wd.get("jobReqId"):
                jd += f"\nJob ID: {info_wd['jobReqId']}"
            if info_wd.get("externalUrl"):
                job["direct_apply_url"] = info_wd["externalUrl"]
                url = job["direct_apply_url"]
    if len(jd) < 300 and job.get("jd_api"):
        d = http_get(job["jd_api"], timeout=25)
        txt = d.get("content", "") if isinstance(d, dict) else ""
        if txt:
            jd = _html_to_text(htmllib.unescape(txt))
    if len(jd) < 300 or job.get("need_fetch"):
        md, links = _fetch_page(url)
        if len(jd) < 300:
            jd = md

    # ── 2. still active? + best apply link ─────────────────────────────────
    link_type, status, locked, board_url = "direct", "active", False, ""
    if is_aggregator(url):                                   # a job-board page
        st, locked, text, final, html_apply = check_page(url)
        if st == "dead":
            return None, "posting closed / not found (board page)"
        status = st
        if len(jd) < 300 and text:
            jd = text
        direct = job.get("apply_hint") if job.get("apply_hint") and not is_aggregator(job["apply_hint"]) else None
        direct = direct or md_apply_link(md, url) or html_apply
        if not direct and final and not is_aggregator(final) and _netloc(final) != _netloc(url):
            direct = final                                    # e.g. Working Nomads /job/go/<id> redirect
        if not direct and links:
            direct = pick_apply_from_links(links, url)
        if direct and _url_level_conflict(job.get("title", ""), direct):
            direct = None                                     # link points at a different (senior / intern) role
        if direct and not bad_apply_target(direct):
            st2, *_ = check_page(direct)
            if st2 == "dead":
                return None, "posting closed (company page)"
            if st2 == "active":                      # an unreachable / unverifiable hint is NOT trusted
                board_url, url, link_type, locked, status = job["direct_apply_url"], direct, "direct", False, "active"
            else:
                link_type = "board"
        else:
            link_type = "board"          # resolved (ATS lookup / apply e-mail) after the JD passes the filters
    elif not src.startswith("ats:"):                         # ATS API lists only open jobs
        st, _l, text, _f, _a = check_page(url)
        if st == "dead":
            return None, "posting closed / not found"
        status = st
        if len(jd) < 300 and text:
            jd = text
    if len(jd) < 300:
        return None, "full JD unreadable"

    # ── 3. details the listing didn't give us ──────────────────────────────
    if job.get("title_from_page") and md:
        m = re.search(r"^#\s+(.+)$", md, re.M)
        if m:
            job["title"] = re.sub(r"\s+", " ", m.group(1)).strip()[:140]
        if not job.get("company") or job["company"] == "Unknown":
            c = re.search(r"^#\s+.+\n\s*\n?\[?([^\]\n(]{2,60})\]?", md, re.M)
            if c:
                job["company"] = c.group(1).strip()
    if not job.get("company"):
        job["company"] = company_from_url(url)
    if not job.get("location"):
        job["location"] = extract_location(jd)
    if not job.get("location") and not src.startswith("ats:") and \
            not re.search(r"\bindia\b|\bworldwide\b|anywhere in the world|work from anywhere|global(?:ly)? remote|\bapac\b|\basia\b", jd[:8000].lower()):
        return None, "no location stated and JD never mentions India / worldwide"

    # ── 4. every rule, on the full JD ──────────────────────────────────────
    ok, reason, info = analyze_jd(job["title"], job["company"], jd, rules,
                                  job.get("location", ""), job.get("salary", ""), job["direct_apply_url"])
    if not ok:
        return None, reason
    apply_email = ""
    if link_type == "board":
        # third-party board page: only keep the role if we can reach the company's own posting, or the JD says "email us"
        res = resolve_direct(job["company"], job["title"], rules) or resolve_workday(job["company"], job["title"], rules) or \
            resolve_bigco(job["company"], job["title"], rules) or resolve_via_site(job["company"], job["title"], rules)
        if not res and "linkedin.com/jobs" in job["direct_apply_url"]:
            res = resolve_by_search(job["company"], job["title"], rules)
        if res and _url_level_conflict(job["title"], res[0]):
            res = None
        if res:
            direct2, ats_loc = res
            ptxt = _posting_text(direct2)
            key = (_slug_candidates(job["company"]) or [""])[-1]
            if ptxt and key and key not in re.sub(r"[^a-z0-9]+", "", (ptxt + direct2).lower()):
                res = None                                   # ATS board belongs to a different company
            elif ptxt and len(ptxt) >= 300:                  # re-run every rule on the COMPANY's posting (its real location/JD)
                ok2, why2, info2 = analyze_jd(job["title"], job["company"], ptxt, rules, ats_loc, job.get("salary", ""), direct2)
                if not ok2:
                    return None, f"company posting: {why2}"
                info, jd = info2, ptxt
            elif not location_ok(ats_loc, rules)[0]:
                return None, f"company posting location not India-eligible ({ats_loc})"
        if res:
            board_url, url, link_type, locked, status = job["direct_apply_url"], res[0], "direct", False, "active"
            job["location"] = res[1] or job.get("location", "")
        else:
            em = find_apply_email(jd, own_host=_netloc(job["direct_apply_url"]))
            if em and rules.get("links", {}).get("allow_email_apply", True):
                board_url, link_type, locked, apply_email = job["direct_apply_url"], "email", False, em
            else:
                return None, "board-only: no company link and no apply e-mail"
    job.update(direct_apply_url=url, link_type=link_type, board_url=board_url, apply_email=apply_email,
               link_status=status, locked=1 if locked else 0, experience_req=info["exp"])
    if not job.get("salary") and info["salary"]:
        job["salary"] = info["salary"]
    job["match_score"] = _score(job["title"], jd, rules, info)
    stack = ("JS stack: " + ", ".join(info["core"][:4])) if info["tier"] != "other" else \
            ("other stack: " + (", ".join(info["wrong"][:3]) or "not JS"))
    job["match_reason"] = (f"JD read ({len(jd):,} chars) · {stack} · exp: {info['exp']}"
                           + (f" · via {job['board']}" if job.get("board") else ""))
    opened = posting_date(url, job.get("posted_date", "") or job.get("date", ""), src)
    if not opened and src == "linkedin_jobs" and board_url:      # company page has no date → LinkedIn's own listing date
        opened = (job.get("posted_date", "") or job.get("date", ""))[:10]
    max_days = rules.get("freshness", {}).get("max_age_days", 40)
    if not opened:
        return None, "opening date could not be verified"
    age = days_ago(opened)
    if age is not None and age > max_days:
        return None, f"opened {opened} ({age} days ago) — older than {max_days} days"
    job["posted_date"] = opened
    job["_sig"] = hashlib.md5(re.sub(r"\W+", "", jd.lower())[:800].encode()).hexdigest()
    return job, "ok"


def collapse_same_day(jobs: list[dict]) -> list[dict]:
    """Identical company+title+JD posted once per city / cross-posted → one listing, combined locations.
    Direct links win over board links."""
    groups: dict[tuple, dict] = {}
    for j in sorted(jobs, key=lambda x: x.get("link_type") != "direct"):
        key = (re.sub(r"\W+", "", j["company"].lower()), re.sub(r"\W+", "", j["title"].lower()), j.get("_sig", ""))
        if key not in groups:
            groups[key] = j
            j["_alt"] = []
        else:
            g = groups[key]
            loc = j.get("location", "")
            if loc and loc.lower() not in g.get("location", "").lower():
                g["location"] = (g.get("location", "") + "; " + loc).strip("; ")
            g["_alt"].append(j["direct_apply_url"])
    return list(groups.values())


# ═══════════════════════════════════════════════════════════════════════════
#  CONTACTS  (Founder · Co-founder · Recruiter · HR · Referral engineer)
#  Real profile discovery needs Firecrawl SEARCH (cloud-only). Without it we
#  generate one-click LinkedIn people-search links instead — never invented profiles.
# ═══════════════════════════════════════════════════════════════════════════

_CONTACT_ROLES = [
    ("Founder",     'founder OR CEO',
     "Founders reply most to concise, specific notes"),
    ("Co-founder",  'co-founder OR CTO',
     "Technical co-founders often handle engineering hiring directly"),
    ("Recruiter",   'recruiter OR "talent acquisition"',
     "In-house recruiters usually answer within a few days while the role is open (unverified)"),
    ("HR",          '"HR" OR "people operations" OR "human resources"',
     "HR replies are slower and less likely than recruiters (unverified)"),
    ("Referral",    'software engineer OR developer',
     "Pick an active engineer with ~500–2000 connections — check this yourself, not auto-verified"),
]


# ── real LinkedIn profiles scraped from the company's OWN website (about / team / contact pages) ──
_LI_PROFILE = re.compile(r'<a\b[^>]*?href=["\'](https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[^"\'?#\s]+)[^"\']*["\'][^>]*>(.*?)</a>', re.I | re.S)
_LI_COMPANY = re.compile(r'href=["\'](https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/[^"\'?#\s/]+)', re.I)
_SITE_CACHE: dict[str, dict] = {}
_ROLE_PATTERNS = [
    ("Co-founder", re.compile(r"co-?founder|\bcto\b|chief technology", re.I)),
    ("Founder", re.compile(r"\bfounder\b|\bceo\b|chief executive|owner|managing director", re.I)),
    ("Recruiter", re.compile(r"recruit|talent|sourcer", re.I)),
    ("HR", re.compile(r"\bhr\b|human resources|people (?:ops|operations|partner|team)|head of people", re.I)),
    ("Referral", re.compile(r"engineer|developer|\bsde\b|tech lead|architect", re.I)),
]


def _find_company_site(company: str) -> tuple[str, str] | None:
    first = (_slug_candidates(company) or [""])[-1] if _slug_candidates(company) else ""
    words = [w for w in re.sub(r"[^a-z0-9 ]+", " ", company.lower()).split() if w not in _SLUG_STOP]
    key = (words[0] if words else first)
    for slug in _slug_candidates(company)[:2]:
        for tld in (".com", ".io", ".ai", ".co"):
            url = f"https://{slug}{tld}"
            h = http_get_text(url, timeout=5, quiet=True)
            tm = re.search(r"<title[^>]*>(.*?)</title>", h or "", re.I | re.S)
            title = re.sub(r"[^a-z0-9 ]+", " ", (tm.group(1) if tm else "").lower())
            if h and len(h) > 800 and key and key in title.split():
                return url, h
    return None


def site_profiles(company: str) -> dict:
    """{'profiles': [{role, name, url}], 'company_page': url} from the company's own site. Never invented."""
    k = company.strip().lower()
    if k in _SITE_CACHE:
        return _SITE_CACHE[k]
    res = {"profiles": [], "company_page": ""}
    try:
        found = _find_company_site(company)
        if found:
            base, home = found
            pages = [home]
            for path in ("/about", "/team", "/company"):
                h = http_get_text(base.rstrip("/") + path, timeout=5, quiet=True)
                if h:
                    pages.append(h)
            seen: set[str] = set()
            for h in pages:
                m = _LI_COMPANY.search(h)
                if m and not res["company_page"]:
                    res["company_page"] = m.group(1)
                for m in _LI_PROFILE.finditer(h):
                    url = m.group(1).rstrip("/")
                    if url.lower() in seen:
                        continue
                    seen.add(url.lower())
                    ctx = re.sub(r"<[^>]+>", " ", h[max(0, m.start() - 160): m.end() + 160])
                    ctx = re.sub(r"\s+", " ", ctx)
                    name = re.sub(r"<[^>]+>|\s+", " ", m.group(2)).strip()
                    if not re.fullmatch(r"[A-Z][a-zA-Z.'\-]+(?: [A-Z][a-zA-Z.'\-]+){1,3}", name):
                        name = re.sub(r"\s*[0-9a-f]{6,}$", "", url.rsplit("/", 1)[-1].replace("-", " ")).strip().title()
                    role = next((r for r, pat in _ROLE_PATTERNS if pat.search(ctx)), "Team")
                    res["profiles"].append({"role": role, "name": name[:50], "url": url})
    except Exception as exc:
        print(f"  [site-profiles ✗] {company}: {exc}")
    _SITE_CACHE[k] = res
    return res


def build_contacts(conn: sqlite3.Connection, company: str, use_search: bool = True) -> list[dict]:
    key = company.strip().lower()
    row = conn.execute("SELECT data, real, updated_at FROM company_contacts WHERE company=?", (key,)).fetchone()
    if row and (row["real"] or not (use_search and fc_search_available())):
        if row["real"] or (row["updated_at"] and (days_ago(row["updated_at"]) or 0) < 7):
            return json.loads(row["data"])
    contacts: list[dict] = []
    real = False
    searching = use_search and fc_search_available()
    sp = site_profiles(company)
    for role, kw, note in _CONTACT_ROLES:
        found = 0
        for p in [p for p in sp["profiles"] if p["role"] == role][:2]:
            contacts.append({"role": role, "name": p["name"], "url": p["url"], "note": note + " — from the company's own website",
                             "kind": "profile"})
            found += 1
            real = True
        if searching:
            for r in fc_search(f'site:linkedin.com/in "{company}" {kw}', limit=3):
                u = r.get("url", "")
                if "linkedin.com/in/" not in u:
                    continue
                name = (r.get("title") or "").split(" - ")[0].split(" | ")[0].strip()[:60]
                contacts.append({"role": role, "name": name, "url": u.split("?")[0],
                                 "note": note, "kind": "profile"})
                found += 1
                real = True
                if found >= 2:
                    break
        if not found:
            q = urllib.parse.quote(f"{company} {kw.replace(' OR ', ' ').replace(chr(34), '')}")
            contacts.append({"role": role, "name": "", "kind": "search", "note": note,
                             "url": f"https://www.linkedin.com/search/results/people/?keywords={q}"})
    if sp["company_page"]:
        contacts.append({"role": "Company page", "name": "", "url": sp["company_page"], "kind": "profile",
                         "note": "Open → People tab to see employees"})
    conn.execute("INSERT OR REPLACE INTO company_contacts (company, data, real, updated_at) VALUES (?,?,?,?)",
                 (key, json.dumps(contacts), 1 if real else 0, now_utc()))
    conn.commit()
    return contacts


# ═══════════════════════════════════════════════════════════════════════════
#  RE-VETTING STORED RESULTS  (still live? still passes every rule? direct link found?)
# ═══════════════════════════════════════════════════════════════════════════

def _company_key(name: str) -> str:
    words = [w for w in re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).split() if w not in _SLUG_STOP]
    return "".join(words) or re.sub(r"\W+", "", (name or "").lower())


MAX_ROLES_PER_COMPANY = int(os.environ.get("MAX_ROLES_PER_COMPANY", "2"))


def enforce_company_cap(conn: sqlite3.Connection, cap: int = MAX_ROLES_PER_COMPANY) -> int:
    """At most `cap` roles per company (best match score first; saved/applied rows are never removed)."""
    rows = conn.execute("SELECT id, company, status, match_score FROM jobs").fetchall()
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(_company_key(r["company"]), []).append(r)
    removed = 0
    for rs in groups.values():
        if len(rs) <= cap:
            continue
        rs.sort(key=lambda r: (r["status"] in ("saved", "applied"), r["match_score"], r["id"]), reverse=True)
        for r in rs[cap:]:
            if r["status"] in ("saved", "applied"):
                continue
            conn.execute("DELETE FROM jobs WHERE id=?", (r["id"],))
            removed += 1
    conn.commit()
    return removed


def ingest_posts(conn: sqlite3.Connection, rules: dict, fns, per_source: dict | None = None) -> tuple[int, dict]:
    """Collect hiring posts, vet each one (genuine hirer, stack, experience, location) and store the survivors."""
    per_source = per_source if per_source is not None else {}
    posts_added = 0
    post_rejects: dict[str, int] = {}
    for fn in fns:
        try:
            found = fn(rules)
            per_source[fn.__name__.replace("collect_", "")] = len(found)
            for post in found:
                ok_post, why = vet_post(post, rules)
                head = post.get("_headline")
                if head is None and post.get("source") in ("LinkedIn", "X/Twitter") and post.get("poster_role") and post["poster_role"] != "Company page":
                    head = post["poster_role"]
                if ok_post and head and re.search(r"open to (?:new )?(?:opportunit|work|roles)|looking for (?:new |my next )?(?:opportunit|role|job)|seeking (?:new )?(?:opportunit|role)", head.lower()):
                    ok_post, why = False, "author is a job-seeker"
                if ok_post and head is not None and not _POSTER_TITLE.search(head):
                    ok_post, why = False, "author is not a founder / recruiter / engineer"
                if ok_post and head is not None and (_FEED_HEADLINE.search(head) or is_staffing_company(head, rules)):
                    ok_post, why = False, "job-feed / agency author"
                akey = post.get("poster_name", "") or post.get("poster_url", "")
                if ok_post and akey and conn.execute("SELECT COUNT(*) FROM posts WHERE poster_name=? AND source=?",
                                                     (akey, post.get("source", ""))).fetchone()[0] >= 3:
                    ok_post, why = False, "author already has 3 posts"
                if not ok_post:
                    post_rejects[why[:40]] = post_rejects.get(why[:40], 0) + 1
                    continue
                try:
                    if insert_post(conn, post):
                        posts_added += 1
                except Exception:
                    pass
        except Exception as exc:
            print(f"  [!] {fn.__name__} failed: {exc}")
    print(f"[post rejects] {sorted(post_rejects.items(), key=lambda x: -x[1])[:8]}")
    return posts_added, post_rejects


def revet_existing_jobs(rules: dict, limit: int | None = None, stale_hours: float = 24) -> dict:
    conn = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=stale_hours)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute("SELECT * FROM jobs WHERE status!='applied' AND (checked_at IS NULL OR checked_at='' OR checked_at < ?) "
                        "ORDER BY checked_at LIMIT ?", (cutoff, limit or 100000)).fetchall()
    conn.execute("DELETE FROM jobs WHERE link_type='board' AND status!='applied' AND IFNULL(apply_email,'')=''")
    conn.commit()
    conn.close()
    if not rows:
        return {"checked": 0, "removed": 0, "updated": 0}

    def work(r):
        okh, whyh = passes_hard_filters(r["title"], r["company"], r["direct_apply_url"], "", rules)
        if not okh:
            return r, None, whyh
        if r["posted_date"] and (days_ago(r["posted_date"]) or 0) > rules.get("freshness", {}).get("max_age_days", 40):
            return r, None, f"stale posting (opened {r['posted_date']})"
        loc = r["location"] if r["location"] and r["location"] != "Remote / not stated" else ""
        j = _job(r["company"], r["title"], r["direct_apply_url"], "revet", rules, location=loc, need_fetch=True,
                 salary=r["salary"], date=r["posted_date"])
        try:
            return r, *verify_candidate(j, rules)
        except Exception as exc:
            return r, None, f"full JD unreadable ({exc})"

    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(work, rows))
    conn = get_db()
    removed = updated = 0
    reasons: dict[str, int] = {}
    stamp = now_utc()
    for r, job, reason in results:
        if job is None:
            if "unreadable" in reason or "unverified" in reason:
                conn.execute("UPDATE jobs SET checked_at=? WHERE id=?", (stamp, r["id"]))
                continue
            reasons[re.sub(r"\d+", "N", reason)[:50]] = reasons.get(re.sub(r"\d+", "N", reason)[:50], 0) + 1
            if r["status"] == "saved":
                conn.execute("UPDATE jobs SET notes=?, checked_at=? WHERE id=?",
                             (f"⚠ no longer passes filters: {reason}", stamp, r["id"]))
            else:
                conn.execute("DELETE FROM jobs WHERE id=?", (r["id"],))
                conn.execute("INSERT OR REPLACE INTO rejected_urls (url, reason, at) VALUES (?,?,?)",
                             (norm_url(r["direct_apply_url"]), reason[:200], stamp))
                removed += 1
            continue
        new_url = job["direct_apply_url"]
        h = make_hash(norm_url(new_url))
        try:
            conn.execute("""UPDATE jobs SET direct_apply_url=?, job_hash=?, link_type=?, board_url=?, apply_email=?,
                            link_status=?, locked=?, experience_req=?, match_score=?, match_reason=?, location=?,
                            salary=?, checked_at=? WHERE id=?""",
                         (new_url, h, job["link_type"], job.get("board_url", ""), job.get("apply_email", ""),
                          job["link_status"], job["locked"], job["experience_req"], job["match_score"],
                          job["match_reason"], job.get("location") or r["location"], job.get("salary", ""), stamp, r["id"]))
            conn.execute("INSERT OR IGNORE INTO seen_jobs (url, first_seen) VALUES (?,?)", (norm_url(new_url), stamp))
            updated += 1
        except sqlite3.IntegrityError:                    # same posting already stored under its direct URL
            conn.execute("DELETE FROM jobs WHERE id=?", (r["id"],))
            removed += 1
    conn.execute("DELETE FROM jobs WHERE link_type='board' AND status!='applied' AND IFNULL(apply_email,'')=''")
    conn.commit()
    conn.close()
    print(f"[re-vet] checked {len(rows)} · removed {removed} · updated {updated} · why: {reasons}")
    return {"checked": len(rows), "removed": removed, "updated": updated, "why": reasons}


def purge_posts_once(conn: sqlite3.Connection, tag: str = "posts_purge_2026_11_24") -> None:
    """User asked to wipe every stored post once so only posts passing the full-text rules come back."""
    if conn.execute("SELECT 1 FROM kv WHERE k=?", (tag,)).fetchone():
        return
    n = conn.execute("DELETE FROM posts").rowcount
    conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?,?)", (tag, now_utc()))
    conn.commit()
    print(f"[purge] removed all {n} stored posts (one-time clean slate)")


def revet_existing_posts(rules: dict) -> int:
    conn = get_db()
    rows = conn.execute("SELECT * FROM posts WHERE status='new'").fetchall()
    gone = 0
    for r in rows:
        ok, _ = vet_post(dict(r), rules)
        head = r["poster_role"] or ""
        if ok and r["source"] in ("LinkedIn", "X/Twitter") and head and head != "Company page":
            if (not _POSTER_TITLE.search(head)) or _FEED_HEADLINE.search(head) or is_staffing_company(head, rules) \
                    or re.search(r"executive search|consultanc|staffing|recruitment (?:firm|agency)|open to (?:new )?opportunit", head.lower()):
                ok = False
        if not ok:
            conn.execute("DELETE FROM posts WHERE id=?", (r["id"],))
            gone += 1
    conn.commit()
    conn.close()
    print(f"[re-vet] posts removed: {gone} of {len(rows)}")
    return gone



# ═══════════════════════════════════════════════════════════════════════════
#  STATIC, PASSWORD-ENCRYPTED DASHBOARD  (deploy the output folder to Vercel / Cloudflare Pages / any static host)
#  index.html is public but contains no data; data.enc.json is AES-GCM encrypted with DASH_PASSWORD and is decrypted
#  in the browser. Applied / Saved marks are kept in the browser's localStorage.
# ═══════════════════════════════════════════════════════════════════════════
_STATIC_SHIM = r"""<style>#scanAllBtn,#syncBtn,button[onclick="toggleScan()"]{display:none!important}
#jhLock{position:fixed;inset:0;background:#0b0f19;z-index:99999;display:flex;align-items:center;justify-content:center;font-family:system-ui}
#jhLock form{background:#111827;border:1px solid #374151;border-radius:14px;padding:28px;width:min(360px,90vw);text-align:center;color:#e5e7eb}
#jhLock input{width:100%;padding:11px;margin:14px 0 8px;border-radius:8px;border:1px solid #4b5563;background:#0b0f19;color:#fff;box-sizing:border-box}
#jhLock button{width:100%;padding:11px;border:0;border-radius:8px;background:#6d28d9;color:#fff;font-weight:600;cursor:pointer}
#jhLock .err{color:#f87171;font-size:13px;min-height:18px}</style>
<div id="jhLock"><form id="jhForm"><div style="font-size:30px">🎯</div><h3 style="margin:6px 0 0">Job Hunter</h3>
<input id="jhPw" type="password" placeholder="Password" autocomplete="current-password" autofocus><div class="err" id="jhErr"></div>
<button>Unlock</button></form></div>
<script>
(function(){
  const LS=localStorage, origFetch=window.fetch.bind(window); let DATA=null;
  const b64=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
  async function decrypt(pw){
    const enc=await (await origFetch('data.enc.json?'+Date.now())).json();
    const km=await crypto.subtle.importKey('raw',new TextEncoder().encode(pw),'PBKDF2',false,['deriveKey']);
    const key=await crypto.subtle.deriveKey({name:'PBKDF2',salt:b64(enc.salt),iterations:enc.iter,hash:'SHA-256'},km,{name:'AES-GCM',length:256},false,['decrypt']);
    const pt=await crypto.subtle.decrypt({name:'AES-GCM',iv:b64(enc.iv)},key,b64(enc.ct));
    return JSON.parse(new TextDecoder().decode(pt));
  }
  const ready=new Promise(resolve=>{
    const lock=document.getElementById('jhLock'), form=document.getElementById('jhForm'), err=document.getElementById('jhErr');
    async function attempt(pw){
      try{ DATA=await decrypt(pw); LS.setItem('jh_pw',pw); lock.style.display='none'; resolve(); return true; }
      catch(e){ LS.removeItem('jh_pw'); err.textContent='Wrong password (or data not published yet)'; return false; }
    }
    form.addEventListener('submit',ev=>{ ev.preventDefault(); attempt(document.getElementById('jhPw').value); });
    const saved=LS.getItem('jh_pw'); if(saved) attempt(saved);
  });
  const marks=()=>JSON.parse(LS.getItem('jh_marks')||'{}');
  const json=o=>new Response(JSON.stringify(o),{headers:{'Content-Type':'application/json'}});
  window.__jhNote=()=>DATA?('cloud data from '+new Date(DATA.meta.generated_at).toLocaleString()+' · refreshes about every 30 min'):'';
  window.fetch=async function(url,opts){
    const u=String(url);
    if(!u.startsWith('/api/')) return origFetch(url,opts);
    await ready; const m=marks();
    if(u==='/api/jobs'){ const del=new Set(JSON.parse(LS.getItem('jh_deleted_urls')||'[]'));
      return json(DATA.jobs.map(j=>({...j,status:del.has(j.direct_apply_url)?'deleted':(m['j'+j.job_hash]||'new')}))); }
    if(u==='/api/posts') return json(DATA.posts.map(p=>({...p,status:m['p'+p.post_hash]||'new'})));
    if(u==='/api/startups') return json((DATA.startups||[]).map(s=>({...s,status:m['s'+s.key]||'new'})));
    if(u==='/api/meta') return json(DATA.meta.discovery||{});
    if(u==='/api/status'){ const b=JSON.parse(opts.body);
      if(b.type==='startup'){ m['s'+b.id]=b.status; LS.setItem('jh_marks',JSON.stringify(m)); return json({ok:true}); }
      const row=(b.type==='post'?DATA.posts:DATA.jobs).find(r=>r.id===b.id);
      if(row){ m[(b.type==='post'?'p'+row.post_hash:'j'+row.job_hash)]=b.status; LS.setItem('jh_marks',JSON.stringify(m));
        if(b.type!=='post'){ const d=new Set(JSON.parse(LS.getItem('jh_deleted_urls')||'[]'));
          if(b.status==='deleted') d.add(row.direct_apply_url); else d.delete(row.direct_apply_url);
          LS.setItem('jh_deleted_urls',JSON.stringify([...d])); } } return json({ok:true}); }
    if(u==='/api/stats') return json({total_jobs:DATA.jobs.length,last_scan:{finished_at:DATA.meta.generated_at},scan:{running:false}});
    if(u==='/api/scan-status') return json({running:false,stage:''});
    return json({ok:true,started:false});
  };
})();
</script>
"""
_STATIC_SHIM_OPEN = r"""<style>#scanAllBtn,#syncBtn,button[onclick="toggleScan()"]{display:none!important}
#jhLock{position:fixed;inset:0;background:#0b0f19;z-index:99999;display:flex;align-items:center;justify-content:center;font-family:system-ui}
#jhLock form{background:#111827;border:1px solid #374151;border-radius:14px;padding:28px;width:min(360px,90vw);text-align:center;color:#e5e7eb}
#jhLock input{width:100%;padding:11px;margin:14px 0 8px;border-radius:8px;border:1px solid #4b5563;background:#0b0f19;color:#fff;box-sizing:border-box}
#jhLock button{width:100%;padding:11px;border:0;border-radius:8px;background:#6d28d9;color:#fff;font-weight:600;cursor:pointer}
#jhLock .err{color:#f87171;font-size:13px;min-height:18px}</style>
<button>Unlock</button></form></div>
<script>
(function(){
  const LS=localStorage, origFetch=window.fetch.bind(window); let DATA=null;
  const ready=origFetch('data.json?'+Date.now()).then(r=>r.json()).then(d=>{DATA=d;});
  const marks=()=>JSON.parse(LS.getItem('jh_marks')||'{}');
  const json=o=>new Response(JSON.stringify(o),{headers:{'Content-Type':'application/json'}});
  window.__jhNote=()=>DATA?('cloud data from '+new Date(DATA.meta.generated_at).toLocaleString()+' · refreshes about every 30 min'):'';
  window.fetch=async function(url,opts){
    const u=String(url);
    if(!u.startsWith('/api/')) return origFetch(url,opts);
    await ready; const m=marks();
    if(u==='/api/jobs'){ const del=new Set(JSON.parse(LS.getItem('jh_deleted_urls')||'[]'));
      return json(DATA.jobs.map(j=>({...j,status:del.has(j.direct_apply_url)?'deleted':(m['j'+j.job_hash]||'new')}))); }
    if(u==='/api/posts') return json(DATA.posts.map(p=>({...p,status:m['p'+p.post_hash]||'new'})));
    if(u==='/api/startups') return json((DATA.startups||[]).map(s=>({...s,status:m['s'+s.key]||'new'})));
    if(u==='/api/meta') return json(DATA.meta.discovery||{});
    if(u==='/api/status'){ const b=JSON.parse(opts.body);
      if(b.type==='startup'){ m['s'+b.id]=b.status; LS.setItem('jh_marks',JSON.stringify(m)); return json({ok:true}); }
      const row=(b.type==='post'?DATA.posts:DATA.jobs).find(r=>r.id===b.id);
      if(row){ m[(b.type==='post'?'p'+row.post_hash:'j'+row.job_hash)]=b.status; LS.setItem('jh_marks',JSON.stringify(m));
        if(b.type!=='post'){ const d=new Set(JSON.parse(LS.getItem('jh_deleted_urls')||'[]'));
          if(b.status==='deleted') d.add(row.direct_apply_url); else d.delete(row.direct_apply_url);
          LS.setItem('jh_deleted_urls',JSON.stringify([...d])); } } return json({ok:true}); }
    if(u==='/api/stats') return json({total_jobs:DATA.jobs.length,last_scan:{finished_at:DATA.meta.generated_at},scan:{running:false}});
    if(u==='/api/scan-status') return json({running:false,stage:''});
    return json({ok:true,started:false});
  };
})();
</script>
"""


def _encrypt_payload(obj: object, password: str) -> dict:
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv, iters = os.urandom(16), os.urandom(12), 250_000
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters, 32)
    ct = AESGCM(key).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode("utf-8"), None)
    b = lambda x: base64.b64encode(x).decode()
    return {"v": 1, "iter": iters, "salt": b(salt), "iv": b(iv), "ct": b(ct)}


def export_static(outdir: str) -> bool:
    """Write index.html + data.enc.json (+ vercel.json / robots.txt) to `outdir`."""
    password = os.environ.get("DASH_PASSWORD", "")      # empty → open (unencrypted) dashboard
    conn = get_db()
    jobs = [dict(r) for r in conn.execute("SELECT * FROM jobs WHERE link_type!='board' ORDER BY match_score DESC, id DESC")]
    posts = [dict(r) for r in conn.execute("SELECT * FROM posts ORDER BY id DESC")]
    startups = [{**json.loads(r["data"]), "key": r["key"], "score": r["score"]} for r in conn.execute(
        "SELECT key, data, score, funded_on FROM startups ORDER BY score DESC, funded_on DESC LIMIT 8000")
        if startup_visible(json.loads(r["data"]), r["funded_on"])]
    last = conn.execute("SELECT finished_at FROM scan_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    meta = {"generated_at": now_utc(), "last_scan": (last["finished_at"] if last else ""), "jobs": len(jobs), "posts": len(posts),
            "discovery": discovery_meta()}
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    payload = {"jobs": jobs, "posts": posts, "startups": startups, "meta": meta}
    html = DASHBOARD_HTML.replace("<script>\n", (_STATIC_SHIM if password else _STATIC_SHIM_OPEN) + "<script>\n", 1)
    (out / "index.html").write_text(html, encoding="utf-8")
    if password:
        (out / "data.enc.json").write_text(json.dumps(_encrypt_payload(payload, password)), encoding="utf-8")
    else:
        (out / "data.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (out / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    (out / "vercel.json").write_text(json.dumps({"headers": [{"source": "/(.*)", "headers": [
        {"key": "X-Robots-Tag", "value": "noindex, nofollow"}, {"key": "Cache-Control", "value": "no-cache"}]}]}), encoding="utf-8")
    print(f"[export] {len(jobs)} jobs + {len(posts)} posts ({'encrypted' if password else 'open'}) → {out}")
    return True


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN SCAN ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════

_SCAN_LOCK = threading.Lock()
SCAN_STATE: dict = {"running": False, "stage": "", "started": None, "summary": None}
MAX_VERIFY_PER_SCAN = int(os.environ.get("MAX_VERIFY_PER_SCAN", "400"))
SCAN_MODE = os.environ.get("SCAN_MODE", "full").lower()      # "light" = job APIs only (fast); "full" = + LinkedIn, careers pages, Apify
REJECT_RECHECK_DAYS = 3


def _stage(msg: str) -> None:
    SCAN_STATE["stage"] = msg
    print(f"[stage] {msg}")


def _collect_manual(urls: list[str], rules: dict) -> list[dict]:
    out: list[dict] = []
    for base_url in urls:
        links = [base_url]
        if not looks_like_job_page(base_url) and not is_aggregator(base_url):
            links = [l for l in fc_map(base_url, limit=80) if looks_like_job_page(l)][:40] or [base_url]
        elif is_aggregator(base_url) and looks_like_listing(base_url):
            sc = fc_scrape(base_url)
            links = [u for _, u in md_links((sc.get("markdown") or "") if isinstance(sc, dict) else "")
                     if not looks_like_listing(u) and _netloc(u) == _netloc(base_url)][:60] or [base_url]
        for link in links:
            out.append(_job(company_from_url(link), "Software Engineer", link, f"manual:{base_url[:35]}",
                            rules, need_fetch=True, title_from_page=True, reason="Manual scan"))
    return out


def run_full_scan(custom_urls: list[str] | None = None) -> dict:
    if not _SCAN_LOCK.acquire(blocking=False):
        conn = get_db()
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        conn.close()
        print("[scan] already running — skipped")
        return {"jobs_added": 0, "duplicates_skipped": 0, "posts_added": 0,
                "total_jobs_in_db": total, "busy": True}
    SCAN_STATE.update(running=True, started=now_utc(), summary=None, stage="starting")
    try:
        summary = _run_full_scan(custom_urls)
        SCAN_STATE["summary"] = summary
        return summary
    except Exception as exc:
        SCAN_STATE["summary"] = {"error": str(exc)}
        raise
    finally:
        SCAN_STATE.update(running=False, stage="")
        _SCAN_LOCK.release()


def start_scan_async(custom_urls: list[str] | None = None) -> bool:
    if SCAN_STATE["running"]:
        return False
    threading.Thread(target=lambda: run_full_scan(custom_urls), daemon=True).start()
    time.sleep(0.3)
    return True


def _interleave(jobs: list[dict]) -> list[dict]:
    """Round-robin across sources so one huge board can't starve the others under the per-scan cap."""
    buckets: dict[str, list[dict]] = {}
    for j in jobs:
        buckets.setdefault(j.get("discovery_source", "").split(":")[0], []).append(j)
    out: list[dict] = []
    lists = list(buckets.values())
    for i in range(max((len(b) for b in lists), default=0)):
        for b in lists:
            if i < len(b):
                out.append(b[i])
    return out


def _run_full_scan(custom_urls: list[str] | None) -> dict:
    _SCRAPE_BUDGET["left"] = int(os.environ.get("SCRAPE_BUDGET", "80"))
    rules = load_rules()
    conn = get_db()
    if _kv_get("pay_fix") != "v1":                     # undo pay judgements made on too-generic company names
        for tok in ("imc", "safe", "plane", "agency", "diagrid", "truveta", "xometry", "broccoli", "tether", "tru"):
            conn.execute("DELETE FROM kv WHERE k=?", ("sal:" + tok,))
            conn.execute("DELETE FROM seen_jobs WHERE url LIKE ?", (f"%/{tok}/%",))
            conn.execute("DELETE FROM seen_jobs WHERE url LIKE ?", (f"%/{tok}-%",))
        conn.commit()
        _kv_set("pay_fix", "v1")
    if _kv_get("rules_version") != "2026-11-25-py-java":    # stack / experience rules changed → re-judge URLs rejected under the old rules
        conn.execute("DELETE FROM rejected_urls")
        conn.commit()
        _kv_set("rules_version", "2026-11-25-py-java")
    cur = conn.execute("INSERT INTO scan_runs (started_at) VALUES (?)", (now_utc(),))
    run_id = cur.lastrowid
    conn.commit()
    per_source: dict[str, int] = {}

    # 1. gather candidates ------------------------------------------------------
    if custom_urls:
        _stage("manual URL scan")
        candidates = _collect_manual(custom_urls, rules)
    else:
        collectors = [
            collect_ats_boards,          # Greenhouse / Lever / Ashby (direct, full JD in API)
            collect_workday,             # Workday career sites (public JSON API, full JD)
            collect_huntyourtribe, collect_urbanodx, collect_jsguru,
            collect_remoteok, collect_remotive, collect_himalayas, collect_jobicy, collect_weworkremotely,
            collect_linkedin_jobs,       # LinkedIn search → the company's own careers page (light: last 24 h only)
        ]
        if SCAN_MODE == "full":
            collectors += [
                collect_company_careers,     # grows the ATS board list + finds roles on career pages (browser)
                collect_matcha, collect_hn_jobs, collect_working_nomads, collect_arbeitnow,
                collect_firecrawl_global, collect_career_pages,
            ]

        def run_collector(fn):
            try:
                return fn.__name__, fn(rules)
            except Exception as exc:
                print(f"  [!] {fn.__name__} failed: {exc}")
                return fn.__name__, []

        _stage(f"collecting from {len(collectors)} sources")
        candidates = []
        with ThreadPoolExecutor(5) as ex:
            for name, found in ex.map(run_collector, collectors):
                per_source[name.replace("collect_", "")] = len(found)
                candidates.extend(found)

    # 2. drop what we've already seen / recently rejected -----------------------
    fresh: list[dict] = []
    seen_now: set[str] = set()
    skipped_seen = 0
    for j in candidates:
        nu = norm_url(j["direct_apply_url"])
        if nu in seen_now:
            continue
        seen_now.add(nu)
        if conn.execute("SELECT 1 FROM seen_jobs WHERE url=?", (nu,)).fetchone():
            skipped_seen += 1
            continue
        rj = conn.execute("SELECT at, reason FROM rejected_urls WHERE url=?", (nu,)).fetchone()
        if rj and (days_ago(rj["at"]) or 0) < (0.5 if "unreadable" in (rj["reason"] or "") else REJECT_RECHECK_DAYS):
            continue
        j["_nu"] = nu
        fresh.append(j)
    try:
        for j in candidates:
            if not str(j.get("discovery_source", "")).startswith("ats:") and j.get("company") and len(j["company"]) > 2:
                _disc_add(conn, j["company"], "", "jobboards", {})
        conn.commit()
    except Exception as exc:
        print(f"  [!] job-board companies not recorded: {exc}")
    fresh = _interleave(fresh)
    if len(fresh) > MAX_VERIFY_PER_SCAN:
        print(f"[verify] capping {len(fresh)} → {MAX_VERIFY_PER_SCAN} this scan (rest next scan)")
        fresh = fresh[:MAX_VERIFY_PER_SCAN]

    # 3. open every job, check it's active, read the FULL JD, apply every rule ---
    _stage(f"opening {len(fresh)} job pages & reading full JDs")
    accepted: list[dict] = []
    rejects: dict[str, int] = {}
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda j: (j, *verify_candidate(j, rules)), fresh))
    for j, ok_job, reason in results:
        if ok_job:
            accepted.append(ok_job)
        else:
            key = re.sub(r"\d+", "N", reason)[:60]
            rejects[key] = rejects.get(key, 0) + 1
            conn.execute("INSERT OR REPLACE INTO rejected_urls (url, reason, at) VALUES (?,?,?)",
                         (j["_nu"], reason[:200], now_utc()))
    conn.commit()
    for j in accepted:
        register_board(conn, j["direct_apply_url"], j.get("discovery_source", ""))
        register_workday(conn, j["direct_apply_url"], j.get("discovery_source", ""))
    accepted = collapse_same_day(accepted)

    # 4. save ------------------------------------------------------------------
    _stage(f"saving {len(accepted)} jobs")
    jobs_added = 0
    dupes = skipped_seen
    contact_searches = 0
    by_type = {"direct": 0, "board": 0, "email": 0}
    # warm the company-website contact cache in parallel (time-boxed) so saving isn't one slow lookup per company
    _stage(f"looking up contacts for {len({_company_key(j['company']) for j in accepted})} companies")
    _t0 = time.time()
    with ThreadPoolExecutor(10) as ex:
        futs = [ex.submit(site_profiles, c) for c in {j["company"] for j in accepted}
                if not conn.execute("SELECT 1 FROM company_contacts WHERE company=?", (c.strip().lower(),)).fetchone()]
        for f in futs:
            try:
                f.result(timeout=max(1, 150 - (time.time() - _t0)))
            except Exception:
                pass
    _stage(f"saving {len(accepted)} jobs")
    if SERPER_API_KEY or TAVILY_API_KEY:
        conn.commit()                                  # pay lookups write to kv on their own connection
        keep = []
        for job in accepted:
            india = not re.search(r"remote|worldwide|anywhere", job.get("location", "") or "", re.I) or "india" in (job.get("location", "") or "").lower()
            if india and not job.get("salary"):
                est = company_sde_salary(job["company"])
                if est is not None and est < SALARY_FLOOR_EST:
                    rejects[f"company pays ~{int(est)} LPA (below {int(SALARY_FLOOR_EST)})"] = rejects.get(f"company pays ~{int(est)} LPA (below {int(SALARY_FLOOR_EST)})", 0) + 1
                    continue
                if est is not None:
                    job["notes"] = (job.get("notes", "") + f" Typical SDE pay at {job['company']}: ~{est} LPA (from search results).").strip()
            keep.append(job)
        accepted = keep
    for job in accepted:
        try:
            company_contacts = build_contacts(conn, job["company"], use_search=contact_searches < 12)
            if fc_search_available():
                contact_searches += 1
            if job.get("li_poster"):
                lp = job["li_poster"]
                company_contacts = [{"role": "Job poster (recruiter)", "name": lp["name"], "url": lp["url"], "kind": "profile",
                                     "note": "The person who posted this exact role on LinkedIn — message them first"}] + \
                                   [c for c in company_contacts if c.get("url") != lp["url"]]
            job["contacts"] = json.dumps(company_contacts)
            alts = [u for u in job.get("_alt", []) if not is_aggregator(u)]
            if alts:
                job["notes"] = "Same role also posted at: " + " | ".join(alts[:8])
            if insert_job(conn, job):
                jobs_added += 1
                by_type[job.get("link_type", "direct")] += 1
            else:
                dupes += 1
        except Exception as exc:
            print(f"  [insert ✗] {job.get('title')} → {exc}")

    capped = enforce_company_cap(conn)
    if capped:
        print(f"[cap] removed {capped} extra roles (max {MAX_ROLES_PER_COMPANY} per company)")
        jobs_added = max(0, jobs_added - capped)

    # 5. posts (founders / recruiters / TA — never staffing feeds) --------------
    _stage("collecting hiring posts")
    post_fns = (collect_techreferrals, collect_tal_club, collect_hiring_sheet, collect_hn_hiring, collect_reddit_posts, collect_linkedin_posts)
    if SCAN_MODE == "full":
        post_fns += (collect_linkedin_companies, collect_linkedin_search, collect_linkedin_apify)
    purge_posts_once(conn)
    if _due("startups_revalidated_at", 20):            # once a day: roles must still be open on the live board
        try:
            revalidate_startups(rules)
            _kv_set("startups_revalidated_at", now_utc())
        except Exception as exc:
            print(f"  [!] startup re-validation failed: {exc}")
    posts_added, post_rejects = ingest_posts(conn, rules, post_fns, per_source)

    if not custom_urls and (SCAN_MODE == "discovery" or (SCAN_MODE == "full" and _due("discovery_run", 23))):
        _stage("discovery: finding new companies")
        try:
            per_source["discovery"] = run_discovery(rules).get("boards_added", 0)
        except Exception as exc:
            print(f"  [!] discovery failed: {exc}")

    if SCAN_MODE == "full" and not custom_urls:
        _stage("funded startups")
        try:
            per_source["funded_startups"] = collect_funded_startups(rules)
        except Exception as exc:
            print(f"  [!] funded startups failed: {exc}")

    # 6. keep stored jobs honest: re-open the stalest ones (closed? filter changed? direct link now resolvable?)
    _stage("re-checking stored jobs")
    try:
        if _kv_get("jobs_rejudged") != "v-date-java":
            revet_existing_jobs(rules, limit=100000, stale_hours=0)     # every stored job, once, with the new rules
            _kv_set("jobs_rejudged", "v-date-java")
        else:
            revet_existing_jobs(rules, limit=60 if SCAN_MODE == "full" else 30, stale_hours=24)
        conn.commit()
        revet_existing_posts(rules)                     # posts are re-judged with the current rules on every run
        conn = get_db()
        cut = 0
        for r in conn.execute("SELECT id, experience_req, title FROM jobs WHERE status NOT IN ('applied','saved')").fetchall():
            if re.search(r"java\s*(?:/|\+|&|and)?\s*(?:react\s*)?full[\s-]?stack|java\s*fullstack|full[\s-]?stack[\s\-–(:/,]*java\b", (r["title"] or "").lower()):
                conn.execute("DELETE FROM jobs WHERE id=?", (r["id"],))      # Java full-stack titles — never your stack
                cut += 1
                continue
            mm = re.match(r"\s*(\d+)", r["experience_req"] or "")
            if mm and int(mm.group(1)) > rules.get("experience", {}).get("max_min_years", 3):
                conn.execute("DELETE FROM jobs WHERE id=?", (r["id"],))
                cut += 1
        conn.commit()
        conn.close()
        print(f"[prune] removed {cut} stored jobs whose minimum experience is above your limit")
        c3 = get_db()
        undated = c3.execute("SELECT id, direct_apply_url, discovery_source, notes FROM jobs WHERE status NOT IN ('applied','saved') "
                             "AND IFNULL(posted_date,'')='' LIMIT 200").fetchall()
        c3.close()
        tries = json.loads(_kv_get("date_tries") or "{}")
        found = [(r, posting_date(r["direct_apply_url"], "", r["discovery_source"] or "")) for r in undated]
        c3 = get_db()
        dated = dropped = 0
        maxd = rules.get("freshness", {}).get("max_age_days", 40)
        for r, d in found:
            k = str(r["id"])
            if d:
                if (days_ago(d) or 0) > maxd:
                    c3.execute("DELETE FROM jobs WHERE id=?", (r["id"],))
                    dropped += 1
                else:
                    c3.execute("UPDATE jobs SET posted_date=? WHERE id=?", (d, r["id"]))
                    dated += 1
                tries.pop(k, None)
            else:
                tries[k] = tries.get(k, 0) + 1
                if tries[k] >= 3:                         # three scans in a row with no verifiable date → drop
                    c3.execute("DELETE FROM jobs WHERE id=?", (r["id"],))
                    dropped += 1
                    tries.pop(k, None)
        c3.commit()
        c3.close()
        _kv_set("date_tries", json.dumps(tries))
        print(f"[dates] undated {len(undated)} · dated {dated} · removed {dropped} (old or unverifiable 3×)")
        if SERPER_API_KEY or TAVILY_API_KEY:                  # stored India jobs with no stated pay: check the company
            c2 = get_db()
            rows = c2.execute("SELECT id, company, location, notes FROM jobs WHERE status NOT IN ('applied','saved') "
                              "AND IFNULL(salary,'')='' AND IFNULL(notes,'') NOT LIKE '%Typical SDE pay%' AND IFNULL(notes,'') NOT LIKE '%pay: unknown%' LIMIT 300").fetchall()
            c2.close()
            decisions = []                                    # look everything up first (lookups write to kv) …
            for r in rows:
                loc = (r["location"] or "").lower()
                if re.search(r"remote|worldwide|anywhere", loc) and "india" not in loc:
                    continue
                decisions.append((r, company_sde_salary(r["company"])))
            c2 = get_db()                                     # … then write in one transaction
            low_pay = 0
            for r, est in decisions:
                if est is not None and est < SALARY_FLOOR_EST:
                    c2.execute("DELETE FROM jobs WHERE id=?", (r["id"],))
                    low_pay += 1
                elif est is not None:
                    c2.execute("UPDATE jobs SET notes=? WHERE id=?", (((r["notes"] or "") + f" Typical SDE pay at {r['company']}: ~{est} LPA.").strip(), r["id"]))
                else:
                    c2.execute("UPDATE jobs SET notes=? WHERE id=?", (((r["notes"] or "") + " Company pay: unknown.").strip(), r["id"]))
            c2.commit()
            c2.close()
            print(f"[pay] checked {len(decisions)} · removed {low_pay} stored jobs at companies paying under {SALARY_FLOOR_EST:g} LPA")
        conn = get_db()
        conn.execute("DELETE FROM jobs WHERE link_type='board' AND status!='applied' AND IFNULL(apply_email,'')=''")
        conn.commit()
    except Exception as exc:
        print(f"  [!] re-vet failed: {exc}")

    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    top = sorted(rejects.items(), key=lambda x: -x[1])[:8]
    conn.execute(
        "UPDATE scan_runs SET finished_at=?,jobs_added=?,duplicates_skipped=?,posts_added=?,notes=? WHERE id=?",
        (now_utc(), jobs_added, dupes, posts_added,
         json.dumps({"candidates": len(candidates), "verified": len(fresh), "sources": per_source,
                     "rejects": dict(top)})[:3000], run_id))
    conn.commit()
    conn.close()
    print(f"[source counts] {per_source}")
    print(f"[reject breakdown] {top}")
    summary = {"jobs_added": jobs_added, "direct_links": by_type["direct"], "email_apply": by_type["email"],
               "duplicates_skipped": dupes, "posts_added": posts_added, "total_jobs_in_db": total,
               "candidates": len(candidates), "jd_read": len(fresh), "sources": per_source,
               "rejected": dict(top), "post_rejects": post_rejects}
    print(f"[scan complete] {summary}")
    return summary


# ─────────────────────────── background discovery loop ──────────────────────

# ═══════════════════════════════════════════════════════════════════════════
#  FUNDED STARTUPS  (Inc42 weekly "Funding Galore" tables → Inc42 profile → company website / ATS / LinkedIn page)
#  For every Indian startup funded in the last ~6 months: round, amount, investors, founders, website, careers page,
#  open developer roles (direct ATS links), recent LinkedIn hiring posts, and e-mails published on its own site.
# ═══════════════════════════════════════════════════════════════════════════
_INC42_FEED = "https://inc42.com/tag/funding-galore/feed/"
STARTUP_ENRICH_PER_RUN = int(os.environ.get("STARTUP_ENRICH_PER_RUN", "120"))
_TECH_SECTORS = re.compile(r"ai|saas|enterprise|fintech|software|developer|deeptech|edtech|healthtech|ecommerce|logistics|"
                           r"proptech|insurtech|mobility|gaming|media|martech|hrtech|legaltech|cyber|cloud|data|b2b", re.I)
_GOOD_MAIL_LOCAL = re.compile(r"^(careers?|jobs?|hr|talent|people|hiring|recruit\w*|join\w*|work|founders?|ceo|cto|hello|contact|info|team|hi)$", re.I)


def _txt(x: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", x or ""))).strip()


def _skey(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _inc42_funding_rows(max_days: int = 183) -> list[dict]:
    """Every startup row from the weekly Funding Galore tables of the last `max_days`."""
    seen_articles = set(json.loads(_kv_get("inc42_articles") or "[]"))
    rows: list[dict] = []
    new_articles: list[str] = []
    for page in (1, 2):
        x = http_get_text(_INC42_FEED + (f"?paged={page}" if page > 1 else ""), 25, quiet=True)
        for link, pub in re.findall(r"<item>.*?<link>(.*?)</link>.*?<pubDate>(.*?)</pubDate>", x or "", re.S):
            link = link.strip()
            try:
                d = datetime.strptime(pub.strip()[5:16], "%d %b %Y")
            except ValueError:
                continue
            if (datetime.now() - d).days > max_days or link in seen_articles:
                continue
            art = http_get_text(link, 30, quiet=True)
            if not art or "<table" not in art:
                continue
            new_articles.append(link)
            table = art[art.find("<table"): art.find("</table>") + 8]
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S):
                c = [_txt(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
                if len(c) < 7 or not c[1] or c[1].lower() == "name":
                    continue
                try:
                    fd = datetime.strptime(c[0], "%d %b %Y").strftime("%Y-%m-%d")
                except ValueError:
                    fd = d.strftime("%Y-%m-%d")
                if fd > datetime.now().strftime("%Y-%m-%d") or fd < (d - timedelta(days=30)).strftime("%Y-%m-%d"):
                    fd = d.strftime("%Y-%m-%d")                       # typo in the source table (e.g. "2926") → report date
                dash = lambda v: "" if v in ("–", "-", "") else v
                rows.append({"name": c[1], "funded_on": fd, "sector": c[2], "subsector": c[3], "model": c[4],
                             "amount": c[5].replace("*", "").strip(), "round": dash(c[6]), "investors": dash(c[7] if len(c) > 7 else ""),
                             "lead": dash(c[8] if len(c) > 8 else ""), "article": link, "source": "Inc42 Funding Galore"})
            time.sleep(0.6)
    if new_articles:
        _kv_set("inc42_articles", json.dumps(sorted(seen_articles | set(new_articles))))
    print(f"[startups] {len(new_articles)} new weekly funding reports → {len(rows)} funded startups")
    return rows


def _inc42_profile(name: str) -> dict:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    url = f"https://inc42.com/company/{slug}/"
    h = http_get_text(url, 25, quiet=True)
    if not h or "Funding, Revenue" not in h:
        return {}
    vals = dict(re.findall(r'"label":"([^"]+)","value":"([^"]{1,400})"', h))
    web = [w.replace("\\/", "/") for w in re.findall(r'"(?:website|websiteUrl|companyWebsite|url)":"(https?:[^"]+)"', h) if "inc42" not in w]
    li = [x for x in re.findall(r'linkedin\.com/company/([^"/\\?]+)', h) if x.lower() != "inc42"]
    desc = re.search(r'"description":"(Founded in[^"]{0,500})"', h)
    return {"inc42": url, "founders": [f.strip() for f in vals.get("Founders", "").split(",") if f.strip()],
            "hq": vals.get("Headquarters", ""), "founded": vals.get("Founding Year", ""),
            "website": web[0] if web else "", "linkedin": f"https://www.linkedin.com/company/{li[0]}" if li else "",
            "li_slug": li[0] if li else "", "description": desc.group(1).replace("\\u0026", "&") if desc else ""}


def _site_contacts(website: str, founders: list[str]) -> dict:
    """E-mails, careers page and LinkedIn profiles published on the startup's own website (nothing guessed)."""
    base = website if website.startswith("http") else "https://" + website
    base = base.rstrip("/")
    dom = _netloc(base)
    stem = dom.split(".")[0] if dom else ""
    pages: dict[str, str] = {}
    for path in ("", "/careers", "/jobs", "/contact", "/contact-us", "/about", "/about-us", "/team"):
        h = http_get_text(base + path, 10, quiet=True)
        if h and len(h) > 500:
            pages[base + path] = h
    emails: dict[str, str] = {}
    careers, profiles = "", []
    fnames = [f.lower().split() for f in founders]
    for page_url, h in pages.items():
        for e in _EMAIL_RE.findall(h):
            e = e.lower().rstrip(".")
            local, _, edom = e.partition("@")
            if (stem and stem in edom) and not any(b in e for b in _BAD_MAIL) and not e.endswith((".png", ".jpg", ".svg", ".webp")):
                emails.setdefault(e, "hiring" if re.match(r"careers?|jobs?|hr|talent|people|hiring|recruit|join", local) else
                                     "founder" if re.match(r"founders?|ceo|cto", local) else
                                     "general" if _GOOD_MAIL_LOCAL.match(local) else "person")
        for href, txt in re.findall(r'<a\b[^>]*?href="([^"#]+)"[^>]*>(.*?)</a>', h, re.S | re.I):
            u = urllib.parse.urljoin(page_url, htmllib.unescape(href))
            t = _txt(txt)
            if not careers and (ats_board_from_url(u) or workday_board_from_url(u) or
                                re.search(r"careers?|jobs|join us|we.?re hiring|open (?:roles|positions)", t + " " + u, re.I)) \
                    and u.startswith("http") and not is_aggregator(u):
                careers = u
        for m in _LI_PROFILE.finditer(h):
            url = m.group(1).rstrip("/")
            slugtxt = url.rsplit("/", 1)[-1].replace("-", " ").lower()
            anchor = _txt(m.group(2)).lower()
            who = next((" ".join(f).title() for f in fnames if f and (f[0] in slugtxt or f[0] in anchor) and (f[-1] in slugtxt or f[-1] in anchor)), "")
            if url not in [p["url"] for p in profiles]:
                profiles.append({"name": who or anchor.title()[:40] or slugtxt.title(), "url": url, "role": "Founder" if who else "Team"})
    return {"emails": [{"email": e, "type": t} for e, t in emails.items()][:8], "careers": careers, "profiles": profiles[:8]}


def _startup_roles(name: str, website: str, careers: str, rules: dict) -> dict:
    """Open roles straight from the startup's own ATS board (direct links), developer roles first."""
    stems = _slug_candidates(name)
    dom = _netloc(website) if website else ""
    if dom:
        stems.append(dom.split(".")[0])
    boards = []
    ab = ats_board_from_url(careers or "")
    if ab:
        boards.append(ab)
    for st in dict.fromkeys(stems):
        for kind in ("greenhouse", "lever", "ashby", "workable", "recruitee", "smartrecruiters", "bamboohr", "breezy", "keka", "zoho"):
            boards.append((kind, st))
    for kind, token in boards:
        if not board_belongs(name, website, f"{kind}:{token}"):
            continue
        posts = _ats_postings(kind, token)
        if not posts:
            continue
        dev = [{"title": t, "url": u, "location": loc, "posted": _ATS_DATES.get(u, "")} for t, u, loc in posts
               if u and title_relevant(t) and passes_hard_filters(t, name, u, "", rules)[0]
               and not re.search(r"analyst|\btest\b|qa\b|sdet|support|sales|designer", t, re.I)
               and not (_ATS_DATES.get(u) and (days_ago(_ATS_DATES[u]) or 0) > 365)]
        conn = get_db()
        try:
            if kind in ("greenhouse", "lever", "ashby", "keka", "zoho"):
                conn.execute("INSERT OR IGNORE INTO ats_boards (kind, token, source, added_at) VALUES (?,?,?,?)", (kind, token, "funded-startup", now_utc()))
                conn.commit()
        finally:
            conn.close()
        return {"board": f"{kind}:{token}", "total": len(posts), "dev": dev[:8]}
    return {"board": "", "total": 0, "dev": []}


def _startup_score(d: dict) -> int:
    sc = 0
    age = days_ago(d.get("funded_on", "")) or 999
    sc += 3 if age <= 30 else 2 if age <= 90 else 1 if age <= 183 else 0
    sc += 3 if d.get("roles", {}).get("dev") else (1 if d.get("roles", {}).get("total") else 0)
    sc += 2 if d.get("hiring_posts") else 0
    sc += 1 if d.get("emails") else 0
    d["tech"] = bool(_TECH_SECTORS.search(f"{d.get('sector', '')} {d.get('subsector', '')} {d.get('description', '')[:200]}"))
    sc += 2 if d["tech"] else 0
    sc += 1 if re.search(r"seed|series a|series b|pre-series", d.get("round", ""), re.I) else 0
    n = (d.get("size") or {}).get("count") or 0
    d["small"] = bool(n and n <= 50)
    d["tiny"] = bool(n and n < 30)
    sc += 2 if d["tiny"] else 1 if d["small"] else 0
    return min(sc, 10)


def _enrich_startup(d: dict, rules: dict) -> dict:
    prof = _inc42_profile(d["name"])
    d.update({k: v for k, v in prof.items() if v})
    if not d.get("website"):
        found = _find_company_site(d["name"])
        if found:
            d["website"] = found[0]
    if d.get("website"):
        d.update(_site_contacts(d["website"], d.get("founders", [])))
    d["roles"] = _startup_roles(d["name"], d.get("website", ""), d.get("careers", ""), rules)
    d["hiring_posts"] = []
    if d.get("li_slug"):
        _li_register_company(d["li_slug"], d["name"])
        posts, _code = _li_company_page_posts(d["li_slug"])
        if d["li_slug"] in _LI_SIZE:
            d["size"] = _LI_SIZE[d["li_slug"]]
        for p in posts or []:
            if _HIRING_RE.search(p["text"][:600]) and re.search(r"engineer|developer|sde|full[\s-]?stack|front[\s-]?end|back[\s-]?end", p["text"], re.I):
                d["hiring_posts"].append({"url": p["post_url"], "date": p["date"], "text": p["text"][:280]})
        d["hiring_posts"] = d["hiring_posts"][:3]
    # founders without a published profile → an honest one-click LinkedIn search (never a guessed URL)
    have = {p["name"].lower() for p in d.get("profiles", []) if p.get("role") == "Founder"}
    d["founder_links"] = [{"name": f, "url": next((p["url"] for p in d.get("profiles", []) if p["name"].lower() == f.lower()), ""),
                           "search": "https://www.linkedin.com/search/results/people/?keywords=" + urllib.parse.quote(f"{f} {d['name']}")}
                          for f in d.get("founders", [])]
    d["recruiter_search"] = ("https://www.linkedin.com/search/results/people/?keywords=" +
                             urllib.parse.quote(f"{d['name']} recruiter OR talent acquisition OR HR"))
    d["score"] = _startup_score(d)
    return d


def collect_funded_startups(rules: dict) -> int:
    rows = _inc42_funding_rows()
    conn = get_db()
    if _kv_get("startups_enrich_version") != "2":          # Keka / Zoho Recruit added → re-check every startup once
        conn.execute("UPDATE startups SET enriched_at=''")
        conn.commit()
        _kv_set("startups_enrich_version", "2")
    for r in rows:
        k = _skey(r["name"])
        if not k:
            continue
        old = conn.execute("SELECT data FROM startups WHERE key=?", (k,)).fetchone()
        if old:
            d = json.loads(old["data"])
            if r["funded_on"] > d.get("funded_on", ""):                   # newer round → update the funding facts
                d.update(r)
                conn.execute("UPDATE startups SET data=?, funded_on=?, enriched_at='' WHERE key=?", (json.dumps(d), r["funded_on"], k))
        else:
            conn.execute("INSERT INTO startups (key, name, funded_on, data, created_at) VALUES (?,?,?,?,?)",
                         (k, r["name"], r["funded_on"], json.dumps(r), now_utc()))
    conn.commit()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    todo = conn.execute("SELECT key, data FROM startups WHERE (enriched_at='' OR enriched_at < ?) AND funded_on != '' ORDER BY (enriched_at='') DESC, funded_on DESC LIMIT ?",
                        (cutoff, STARTUP_ENRICH_PER_RUN)).fetchall()
    conn.close()

    def work(row):
        try:
            return row["key"], _enrich_startup(json.loads(row["data"]), rules)
        except Exception as exc:
            print(f"  [startup ✗] {row['key']}: {exc}")
            return row["key"], None

    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(work, todo))
    conn = get_db()
    for k, d in results:
        if d:
            conn.execute("UPDATE startups SET data=?, score=?, enriched_at=? WHERE key=?", (json.dumps(d), d["score"], now_utc(), k))
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM startups WHERE funded_on >= ?", ((datetime.now() - timedelta(days=183)).strftime("%Y-%m-%d"),)).fetchone()[0]
    conn.close()
    print(f"[startups] enriched {sum(1 for _, d in results if d)} · {total} funded startups (last 6 months) in the list")
    return total


# ═══════════════════════════════════════════════════════════════════════════
#  TECHREFERRALS.IN  — hiring posts by real employees / recruiters / founders (LinkedIn + X), with the poster's
#  profile and/or e-mail. Public pages (robots.txt allows them; /api/ and /go/ are never touched).
# ═══════════════════════════════════════════════════════════════════════════
TR_BASE = "https://techreferrals.in"
_TR_TITLE_OUT = re.compile(r"\b(?:senior|sr\.?|staff|principal|lead|architect|manager|head|director|vp|intern(?:ship)?|trainee|fresher|"
                           r"data scientist|devops|sre|qa|test|ios|android|flutter|\.net|golang|php|salesforce|sap|mainframe|"
                           r"security|network|embedded|hardware|designer|analyst|support|sales|marketing|recruiter)\b", re.I)
_TR_FOUNDERS = ("harshit-jain", "akshita-kejriwal")


def _tr_get(path: str) -> str:
    time.sleep(0.5)
    return http_get_text(TR_BASE + path, 25, quiet=True) or ""


def _tr_list(category: str, pages: int, seen: set) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for pg in range(1, pages + 1):
        h = _tr_get(f"/?category={urllib.parse.quote_plus(category)}&page={pg}")
        found = re.findall(r'"url":"https://techreferrals\.in/p/([0-9a-f-]{36})","name":"([^"]+)"', h)
        if not found:
            break
        fresh = [(i, n) for i, n in found if i not in seen]
        out.extend(fresh)
    return out


def _tr_post(pid: str, name: str, rules: dict) -> dict | None:
    h = _tr_get(f"/p/{pid}")
    if not h:
        return None
    node = None
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', h, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        for n in (d.get("@graph") if isinstance(d, dict) and "@graph" in d else [d]):
            if isinstance(n, dict) and n.get("@type") in ("JobPosting", "SocialMediaPosting"):
                node = n
    if not node:
        return None
    page = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>|<style.*?</style>", "", h, flags=re.S)))
    seg = page[page.find("Posted by"):] if "Posted by" in page else ""
    seg = seg.split("Open original post")[0].split("Check it’s real")[0]
    glance = re.search(r"At a glance (.*?)(?:Posted \d|Total Experience|Looking for|Check it|How to reach out)", page)
    reach = re.search(r"How to reach out:\s*(.{10,400}?)(?:\s{2,}|Open original post|Contributed by| [A-Z][\w.+#]+(?: [A-Z][\w.+#]+){0,2} [A-Z][\w.+#]+ [A-Z][\w.+#]+ )", page)
    desc = _html_to_text(htmllib.unescape(node.get("description", "") or ""))
    org = ((node.get("hiringOrganization") or node.get("about") or {}) or {}).get("name", "")
    headline = node.get("headline") or node.get("title") or name
    au = node.get("author") or {}
    poster_name = htmllib.unescape(au.get("name") or "").strip()
    prof = (au.get("url") or "").strip()
    if not prof:
        links = re.findall(r'href="(https://www\.linkedin\.com/in/[^"]+)"', h)
        prof = next((u for u in links if not any(f in u for f in _TR_FOUNDERS)), "")
    if not poster_name:
        wm = re.search(r"Who posted this opening\s+\S\s+(.+?)\s+Posted by", page)
        poster_name = wm.group(1).strip() if wm else ""
    if not poster_name:
        pm = re.search(r"Posted by (?:an? )?(.{3,60}?) (?:Verified|at |Human|Talent|Recruit|HR|Founder|Co-?founder|CEO|CTO|Engineering|Senior|Software)", page)
        poster_name = pm.group(1).strip() if pm else ""
    role = ""
    if "Who posted this opening" in page and not au.get("name"):
        rm = re.search(r"Posted by (an? [^.]{3,50}?) (?:Verified|Hiring contact|Email|$)", page[page.find("Who posted this opening"):])
        role = (rm.group(1) if rm else "employee")
    elif poster_name and poster_name in seg:
        role = seg.split(poster_name, 1)[1].strip()[:70]
        role = re.split(r"\s{1,}(?:%s|is hiring|are hiring|At a glance)" % re.escape(org or "@@"), role)[0].strip()
    skills = node.get("keywords") or ""
    emails = [e for e in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", (reach.group(1) if reach else "") + " " + desc + " " + seg)
              if not e.lower().endswith(("techreferrals.in", ".png", ".webp"))]
    orig = node.get("isBasedOn") or next(iter(re.findall(r'href="(https://www\.linkedin\.com/feed/update/[^"]+)"', h)), "")
    parts = [f"{headline}.", desc]
    if glance:
        parts.append("At a glance: " + glance.group(1).replace("Experience ", "Experience: ").replace("Pay ", "Pay: ").replace("Where ", "Location: ").strip())
    if poster_name:
        parts.append(f"Posted by {poster_name}" + (f", {role}" if role else "") + ".")
    reach_txt = (reach.group(1).strip() if reach else "")
    if reach_txt:
        parts.append("How to reach out: " + reach_txt)
    if emails and not any(e in reach_txt for e in emails):
        parts.append(f"Email: {emails[0]}")
    if prof:
        parts.append(f"Reach out to {poster_name or 'the poster'} on LinkedIn: {prof}")
    if skills:
        parts.append("Skills: " + skills)
    text = "\n".join(x for x in parts if x)
    mloc = re.search(r"Location: ([^\n]+?)(?: Looking for|$)", text)
    role = htmllib.unescape(role)
    return {"source": "TechReferrals", "poster_name": poster_name or org, "poster_role": role or "Employee",
            "poster_url": prof, "post_url": orig or f"{TR_BASE}/p/{pid}", "post_text": text[:3500],
            "skills_found": _extract_skills(text, rules), "location": (mloc.group(1) if mloc else "")[:60],
            "posted_at": (node.get("datePublished") or node.get("datePosted") or "")[:10],
            "_headline": None, "_tr_org": org, "_tr_id": pid}


def collect_techreferrals(rules: dict) -> list[dict]:
    if _kv_get("tr_version") != "3":                 # filters changed → look at every TechReferrals post again
        _kv_set("tr_seen", "[]")
        _kv_set("tr_version", "3")
    try:
        seen = set(json.loads(_kv_get("tr_seen") or "[]"))
    except ValueError:
        seen = set()
    pages = int(os.environ.get("TR_PAGES", "40")) if not seen else int(os.environ.get("TR_PAGES_DAILY", "6"))
    cands = []
    for cat in ("Software Engineering",):
        cands.extend(_tr_list(cat, pages, seen))
    keep = [(i, n) for i, n in cands if not _TR_TITLE_OUT.search(n.split(" at ")[0])]
    print(f"[techreferrals] {len(cands)} new posts listed → {len(keep)} after the title filter")
    cap = int(os.environ.get("TR_DETAIL_CAP", "400"))
    out: list[dict] = []
    with ThreadPoolExecutor(2) as ex:
        for p in ex.map(lambda x: _tr_post(x[0], x[1], rules), keep[:cap]):
            if p:
                out.append(p)
    done = {i for i, _ in keep[:cap]}
    seen.update(i for i, _ in cands if i in done or (i, _) not in keep)
    _kv_set("tr_seen", json.dumps(sorted(seen)[-6000:]))
    print(f"[techreferrals] {len(out)} readable posts")
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  DISCOVERY ENGINE  (once a day: find NEW companies, detect their job board, add it to the polling list)
#  Sources: YC directory (companies hiring India/remote), remote-company list (remoteintech), funding news,
#  every company seen on LinkedIn Jobs, and weekly a Common Crawl sweep of ATS hostnames.
# ═══════════════════════════════════════════════════════════════════════════
DISCOVERY_PER_DAY = int(os.environ.get("DISCOVERY_COMPANIES_PER_DAY", "3000"))


def _disc_add(conn: sqlite3.Connection, name: str, website: str, source: str, info: dict | None = None) -> bool:
    key = _skey(name) or _netloc(website).replace(".", "")
    if not key:
        return False
    cur = conn.execute("INSERT OR IGNORE INTO discovered_companies (key, name, website, source, info, first_seen) VALUES (?,?,?,?,?,?)",
                       (key, name.strip()[:120], (website or "").strip()[:300], source, json.dumps(info or {}), now_utc()))
    return cur.rowcount > 0


def _discover_yc() -> list[tuple[str, str, dict]]:
    """Every Y Combinator company that is hiring (any country), queried batch by batch."""
    h = http_get_text("https://www.ycombinator.com/companies", 25, quiet=True)
    m = re.search(r"window\.AlgoliaOpts\s*=\s*(\{.*?\});", h or "")
    if not m:
        return []
    o = json.loads(m.group(1))
    out = []
    seen_slugs: set = set()
    batches = [f"{season} {year}" for year in range(2026, 2004, -1) for season in ("Winter", "Spring", "Summer", "Fall")]
    for batch in batches + [None]:
        flt = f'isHiring:true AND batch:"{batch}"' if batch else "isHiring:true"
        for page in range(0, 10):
            body = json.dumps({"requests": [{"indexName": "YCCompany_production",
                                             "params": f"hitsPerPage=100&page={page}&filters={urllib.parse.quote(flt)}"}]}).encode()
            try:
                req = urllib.request.Request(f"https://{o['app'].lower()}-dsn.algolia.net/1/indexes/*/queries", data=body, method="POST",
                                             headers={"x-algolia-application-id": o["app"], "x-algolia-api-key": o["key"],
                                                      "Content-Type": "application/json", "User-Agent": BROWSER_UA})
                with urllib.request.urlopen(req, timeout=25) as r:
                    res = json.loads(r.read())["results"][0]
            except Exception:
                break
            for hit in res.get("hits", []):
                sl = hit.get("slug", "")
                if sl in seen_slugs:
                    continue
                seen_slugs.add(sl)
                out.append((hit.get("name", ""), hit.get("website", ""),
                            {"yc": hit.get("batch", ""), "locations": hit.get("all_locations", ""), "team": hit.get("team_size", ""),
                             "about": hit.get("one_liner", ""), "yc_url": f"https://www.ycombinator.com/companies/{sl}"}))
            if page >= res.get("nbPages", 1) - 1:
                break
    return out


def _discover_hn() -> list[tuple[str, str, dict]]:
    """Companies from the last 3 monthly 'Who is hiring?' threads — real, currently hiring startups."""
    top = http_get("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=12", timeout=20)
    threads = [h for h in (top.get("hits", []) if isinstance(top, dict) else []) if "who is hiring" in (h.get("title") or "").lower()][:3]
    out, seen = [], set()
    for th in threads:
        tid = th.get("objectID", "")
        for page in range(0, 8):
            data = http_get(f"https://hn.algolia.com/api/v1/search_by_date?tags=comment,story_{tid}&hitsPerPage=500&page={page}", timeout=30)
            hits = data.get("hits", []) if isinstance(data, dict) else []
            for c in hits:
                if str(c.get("parent_id")) != str(tid):
                    continue
                raw = c.get("comment_text", "") or ""
                text = _hn_text(raw)
                first = text.split("\n")[0]
                name = re.split(r"\s*[|(\u2013\u2014]\s*", first)[0].strip(" .:-")[:60]
                if not name or len(name) < 2 or name.lower() in seen or len(name.split()) > 5 or \
                        re.search(r"seeking|freelance|early[- ]stage|stealth|hiring|startup\b|remote$|full[- ]?time|\bwanted\b", name, re.I):
                    continue
                seen.add(name.lower())
                url = next((u for u in re.findall(r'href="(https?://[^"]+)"', raw) if "ycombinator" not in u and "linkedin" not in u), "")
                out.append((name, url, {"about": first[:160], "hn": True}))
            if len(hits) < 500:
                break
    return out


def _discover_lists() -> list[tuple[str, str, dict]]:
    """Curated public company lists on GitHub (README tables / bullet lists)."""
    out = []
    for url in ("https://raw.githubusercontent.com/poteto/hiring-without-whiteboards/master/README.md",
                "https://raw.githubusercontent.com/yanirs/established-remote/master/README.md",
                "https://raw.githubusercontent.com/lukasz-madon/awesome-remote-job/master/README.md"):
        md = http_get_text(url, 30, quiet=True) or ""
        for name, link in re.findall(r"\[([^\]]{2,50})\]\((https?://[^)\s]+)\)", md):
            pu = urllib.parse.urlparse(link)
            if any(x in link for x in ("github.com", "twitter.com", "linkedin.com", "medium.com", "youtube.com", "wikipedia", "ycombinator.com",
                                       "archive.org", "reddit.com", "notion.", "docs.google")) or \
                    len([p for p in pu.path.split("/") if p]) > 2 or re.search(r"\d{4}|how[- ]|hire|interview|blog|guide", name + pu.path, re.I):
                continue
            out.append((name.strip(), link, {"list": url.split("/")[4]}))
    return out


def _discover_layoffsandhires() -> list[tuple[str, str, dict]]:
    """Company names with open jobs on their own career sites (layoffsandhires.com sitemap — public, allowed by robots.txt)."""
    x = http_get_text("https://layoffsandhires.com/sitemaps/sitemap/companies-0.xml", 30, quiet=True) or ""
    out = []
    for slug in re.findall(r"/jobs/at/([a-z0-9-]+)</loc>", x):
        out.append((slug.replace("-", " ").title(), "", {"lh_slug": slug}))
    return out


def _discover_remoteintech() -> list[tuple[str, str, dict]]:
    import io
    import tarfile
    try:
        req = urllib.request.Request("https://codeload.github.com/remoteintech/remote-jobs/tar.gz/refs/heads/main", headers={"User-Agent": "job-hunter"})
        with urllib.request.urlopen(req, timeout=90) as r:
            tf = tarfile.open(fileobj=io.BytesIO(r.read()), mode="r:gz")
    except Exception:
        return []
    out = []
    for m in tf.getmembers():
        if "/src/companies/" not in m.name or not m.name.endswith(".md"):
            continue
        fm = tf.extractfile(m).read().decode("utf-8", "replace").split("---")
        meta = fm[1] if len(fm) > 2 else ""
        g = lambda k: (re.search(rf"^{k}:\s*\"?([^\"\n]+)\"?\s*$", meta, re.M) or [None, ""])[1].strip()
        region = g("region").lower()
        if region and not re.search(r"worldwide|global|asia|india|apac|anywhere", region):
            continue                                                       # Europe-only / US-only companies are skipped
        out.append((g("title"), g("website"), {"careers": g("careers_url"), "region": region}))
    return out


def _discover_news() -> list[tuple[str, str, dict]]:
    out = []
    for feed in ("https://economictimes.indiatimes.com/tech/funding/rssfeeds/78570540.cms", "https://yourstory.com/feed"):
        x = http_get_text(feed, 25, quiet=True)
        for t in re.findall(r"<item>.*?<title>(.*?)</title>", x or "", re.S):
            t = htmllib.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", t)).strip()
            m = re.search(r"^(?:[\w\-]+\s+(?:startup|platform|firm|company|player|unicorn)\s+)?([A-Z][\w.&'\-]*(?:\s+[A-Z][\w.&'\-]*){0,3})\s+"
                          r"(?:raises|secures|bags|nets|closes|gets)\b", t)
            if m and len(m.group(1)) > 2:
                out.append((m.group(1), "", {"news": t[:140]}))
    return out


_CC_DOMAINS = {"keka": "keka.com", "zoho": "zohorecruit.in", "ashby": "jobs.ashbyhq.com", "lever": "jobs.lever.co",
               "greenhouse": "boards.greenhouse.io", "workday": "myworkdayjobs.com"}


def _discover_commoncrawl() -> list[str]:
    """Weekly best effort: every ATS / Workday URL Common Crawl saw → board URLs (its free index is often overloaded)."""
    try:
        coll = json.loads(http_get_text("https://index.commoncrawl.org/collinfo.json", 30, quiet=True) or "[]")[0]["id"]
    except Exception:
        return []
    urls: list[str] = []
    for kind, dom in _CC_DOMAINS.items():
        for attempt in range(2):
            t = http_get_text(f"https://index.commoncrawl.org/{coll}-index?url={urllib.parse.quote(dom)}&matchType=domain"
                              f"&output=json&fl=url&limit=5000", 120, quiet=True)
            if t and t.lstrip().startswith("{"):
                urls += [json.loads(l)["url"] for l in t.splitlines() if l.startswith("{")]
                break
            time.sleep(20)
    return urls


_VC_BOARD = re.compile(r"vc$|-vc\b|ventures?|capital|partners|portfolio|talent|network|accelerator|collective|fund\b|labs-jobs|combinator", re.I)


def board_belongs(name: str, website: str, board: str) -> bool:
    """True when the ATS token is the company's own (e.g. 'acme' for Acme Inc / acme.io) — not a VC portfolio board."""
    kind, _, tok = (board or "").partition(":")
    if kind == "workday":
        return True
    t = re.sub(r"[^a-z0-9]", "", tok.lower())
    if not t or _VC_BOARD.search(tok):
        return False
    stems = {re.sub(r"[^a-z0-9]", "", x.lower()) for x in _slug_candidates(name)}
    if website:
        dom = _netloc(website if website.startswith("http") else "https://" + website).split(".")
        stems.add(re.sub(r"[^a-z0-9]", "", dom[0] if dom[0] not in ("www", "app") else (dom[1] if len(dom) > 1 else "")))
    stems = {x for x in stems if len(x) >= 3}
    return any(t == x or t.startswith(x) or x.startswith(t) and len(t) >= 4 for x in stems)


def detect_board(name: str, website: str, careers: str = "") -> str:
    """'kind:token' / 'workday:host|tenant|site' for the company's job board, or '' — careers link first, then name slugs."""
    for u in [careers] if careers else []:
        ab, wb = ats_board_from_url(u), workday_board_from_url(u)
        if ab and board_belongs(name, website, f"{ab[0]}:{ab[1]}"):
            return f"{ab[0]}:{ab[1]}"
        if wb:
            return "workday:" + "|".join(wb)
    if website:
        base = (website if website.startswith("http") else "https://" + website).rstrip("/")
        for path in ("/careers", "/jobs", ""):
            h = http_get_text(base + path, 10, quiet=True)
            for href in re.findall(r'href="([^"#]+)"', h or ""):
                u = urllib.parse.urljoin(base + path, htmllib.unescape(href))
                ab, wb = ats_board_from_url(u), workday_board_from_url(u)
                if ab and board_belongs(name, website, f"{ab[0]}:{ab[1]}"):
                    return f"{ab[0]}:{ab[1]}"
                if wb:
                    return "workday:" + "|".join(wb)
    stems = list(dict.fromkeys(_slug_candidates(name)[:2] + ([_netloc(website).split(".")[0]] if website else [])))
    for st in stems:
        for kind in ("greenhouse", "lever", "ashby", "keka", "zoho", "workable", "recruitee", "smartrecruiters"):
            if _ats_postings(kind, st):                                     # board exists AND has open roles
                return f"{kind}:{st}"
    return ""


def _register_detected(conn: sqlite3.Connection, board: str, source: str) -> None:
    kind, _, tok = board.partition(":")
    if kind == "workday":
        host, tenant, site = tok.split("|")
        conn.execute("INSERT OR IGNORE INTO workday_boards (host, tenant, site, source) VALUES (?,?,?,?)", (host, tenant, site, source))
    elif kind in ("greenhouse", "lever", "ashby", "keka", "zoho"):
        conn.execute("INSERT OR IGNORE INTO ats_boards (kind, token, source, added_at) VALUES (?,?,?,?)", (kind, tok, source, now_utc()))
    # workable / recruitee / smartrecruiters are used for link resolution only (no bulk poller yet)


_SIMPLE_ATS = ("greenhouse", "lever", "ashby", "workable", "recruitee", "smartrecruiters", "bamboohr", "breezy", "keka", "zoho")


def _startup_from_board(name: str, website: str, source: str, info: dict, board: str, rules: dict) -> dict | None:
    """A startup row for the Startups tab: only when its own job board lists a developer role that fits you.
    Location: India / remote / worldwide / unstated roles stay; roles locked to another country are dropped."""
    if not board_belongs(name, website, board):
        return None
    kind, _, token = board.partition(":")
    posts = _ats_postings(kind, token)
    if not posts:
        return None
    sen = re.compile(r"\b(?:senior|sr\.?|staff|principal|lead|architect|manager|head|director|vp|intern(?:ship)?|trainee|iii|iv|sde[\s-]?3|data scientist|"
                     r"devops|sre|qa|ios|android|designer|analyst|support|sales|marketing|recruiter)\b", re.I)
    dev = []
    for t, u, loc in posts:
        if not (u and title_relevant(t)) or sen.search(t):
            continue
        posted = _ATS_DATES.get(u, "")
        if posted and (days_ago(posted) or 0) > 365:          # still listed but open > 1 year = evergreen / ghost posting
            continue
        if not re.search(r"engineer|developer|sde|programmer|full[\s-]?stack|front[\s-]?end|back[\s-]?end|software", t, re.I):
            continue
        ok_loc = bool(location_ok(loc or "", rules)[0])
        dev.append({"title": t, "url": u, "location": loc, "remote_or_india": ok_loc, "posted": _ATS_DATES.get(u, "")})
    if not dev:
        return None
    dev.sort(key=lambda r: not r["remote_or_india"])
    team = info.get("team")
    try:
        team = int(team) if team not in (None, "") else 0
    except (TypeError, ValueError):
        team = 0
    d = {"name": name, "website": website, "source": source, "description": info.get("about", ""), "round": info.get("yc", ""),
         "hq": info.get("locations", ""), "funded_on": "", "tech": True, "yc_url": info.get("yc_url", ""),
         "size": {"count": team, "range": ""} if team else {}, "roles": {"board": board, "total": len(posts), "dev": dev[:8]},
         "emails": [], "profiles": [], "founders": [], "founder_links": [], "hiring_posts": []}
    d["recruiter_search"] = "https://www.linkedin.com/search/results/people/?keywords=" + urllib.parse.quote(f"{name} recruiter OR talent acquisition OR HR")
    d["any_remote"] = any(r["remote_or_india"] for r in dev)
    d["score"] = _startup_score(d) + (1 if d["any_remote"] else 0)
    d["score"] = min(d["score"], 10)
    return d


SALARY_FLOOR_EST = float(os.environ.get("SALARY_FLOOR_EST", "10"))     # typical SDE pay at the company, LPA


def company_sde_salary(company: str) -> float | None:
    """Typical software-engineer pay at `company` in India (LPA), from Google result snippets (Serper / Tavily — no
    site is scraped). Cached 30 days. None = unknown (no key, or nothing found)."""
    if not company or not (SERPER_API_KEY or TAVILY_API_KEY):
        return None
    words = re.findall(r"[a-z0-9]+", company.lower())
    if len(words) == 1 and len(words[0]) < 7:          # 'IMC', 'Safe', 'Plane' … too generic to search reliably
        return None
    ck = "sal:" + _skey(company)
    cached = _kv_get(ck)
    if cached:
        try:
            v, at = json.loads(cached)
            if (days_ago(at) or 0) < 30:
                return v
        except ValueError:
            pass
    q = f'{company} software engineer salary India'
    snippets = ""
    try:
        if SERPER_API_KEY:
            req = urllib.request.Request("https://google.serper.dev/search", method="POST",
                                         data=json.dumps({"q": q, "gl": "in"}).encode(),
                                         headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                res = json.loads(r.read().decode())
            snippets = [(x.get("title", "") + " | " + x.get("snippet", "")) for x in res.get("organic", [])]
        else:
            req = urllib.request.Request("https://api.tavily.com/search", method="POST",
                                         data=json.dumps({"query": q, "max_results": 8, "search_depth": "basic"}).encode(),
                                         headers={"Authorization": f"Bearer {TAVILY_API_KEY}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.loads(r.read().decode())
            snippets = [(x.get("title", "") + " | " + x.get("content", "")) for x in res.get("results", [])]
    except Exception as exc:
        print(f"  [salary ✗] {company}: {str(exc)[:60]}")
        return None
    first = re.sub(r"[^a-z0-9]", "", " ".join(w for w in company.split() if not re.fullmatch(r"(?i)inc\.?|ltd\.?|llc|pvt\.?|private|limited|technologies|solutions|software|labs?|ai|tech", w)) .lower()) \
        or re.sub(r"[^a-z0-9]", "", company.lower())
    vals = []
    for res_txt in snippets:
        if first not in re.sub(r"[^a-z0-9 ]", "", res_txt.lower()).replace(" ", ""):
            continue
        if not re.search(r"salar", res_txt, re.I) or re.search(r"\bjobs? (?:in|vacanc)|hiring|years of experience|\d+\s*(?:-|to)\s*\d+\s*yrs", res_txt, re.I):
            continue                                   # salary pages only — not job listings (those are senior-skewed)
        for seg in re.split(r";|\s\|\s|\n|\.\s+(?=[A-Z])", res_txt):
            if re.search(r"senior|sr\.|lead|manager|architect|principal|staff|director|head", seg, re.I):
                continue
            for a_, b_ in re.findall(r"₹?\s*(\d{1,2}(?:\.\d{1,2})?)\s*(?:L\b|LPA|lakhs?|L/yr)?\s*(?:-|–|to)\s*₹?\s*(\d{1,2}(?:\.\d{1,2})?)\s*(?:L\b|LPA|lakhs?|L/yr)", seg, re.I):
                lo, hi = float(a_), float(b_)
                if 1 <= lo <= hi <= 80:
                    vals.append((lo + hi) / 2)
    v = round(sorted(vals)[len(vals) // 2], 1) if vals else None
    _kv_set(ck, json.dumps([v, now_utc()]))
    return v


def startup_visible(d: dict, funded_on: str) -> bool:
    """User rule: show a startup only if it was funded in the last 6 months, OR it has a developer role that is OPEN RIGHT
    NOW on its OWN job board (any location — founders can be cold-mailed). Roles are re-checked against the live board
    every day; closed ones disappear. Big-company lists are not startups."""
    if d.get("source") == "layoffsandhires":
        return False
    if d.get("pay_est") is not None and d["pay_est"] < SALARY_FLOOR_EST:
        return False
    if funded_on and (days_ago(funded_on) or 999) <= 183:
        return True
    return bool((d.get("roles") or {}).get("dev"))


def revalidate_startups(rules: dict) -> dict:
    """One pass over every stored startup: drop roles from boards that aren't the company's own (VC portfolio boards),
    drop postings older than 6 months, then delete startups that no longer meet the visibility rule."""
    conn = get_db()
    rows = conn.execute("SELECT key, name, funded_on, data FROM startups").fetchall()
    conn.close()

    def work(r):
        d = json.loads(r["data"])
        if d.get("source") == "layoffsandhires":
            return r["key"], None
        board = (d.get("roles") or {}).get("board", "")
        if r["funded_on"]:
            if board or d.get("website"):
                d["roles"] = _startup_roles(d["name"], d.get("website", ""), d.get("careers", ""), rules)
        elif board:
            info = {"team": (d.get("size") or {}).get("count", ""), "about": d.get("description", ""), "yc": d.get("round", ""),
                    "locations": d.get("hq", ""), "yc_url": d.get("yc_url", "")}
            fresh = _startup_from_board(d["name"], d.get("website", ""), d.get("source", ""), info, board, rules)
            if not fresh:
                return r["key"], None
            for k in ("emails", "profiles", "founders", "founder_links", "company_linkedin", "careers"):
                if d.get(k):
                    fresh[k] = d[k]
            d = fresh
        if r["funded_on"] or re.search(r"india|bengaluru|bangalore|hyderabad|pune|mumbai|delhi|gurugram|noida|chennai", d.get("hq", "") or "", re.I):
            d["pay_est"] = company_sde_salary(d["name"])
        d["score"] = _startup_score(d)
        return r["key"], d if startup_visible(d, r["funded_on"]) else None

    with ThreadPoolExecutor(10) as ex:
        res = list(ex.map(work, rows))
    conn = get_db()
    kept = removed = 0
    for k, d in res:
        if d is None:
            conn.execute("DELETE FROM startups WHERE key=?", (k,))
            removed += 1
        else:
            conn.execute("UPDATE startups SET data=?, score=? WHERE key=?", (json.dumps(d), d["score"], k))
            kept += 1
    conn.commit()
    conn.close()
    print(f"[startups] re-validated: kept {kept}, removed {removed}")
    return {"kept": kept, "removed": removed}


def _enrich_discovered(d: dict) -> dict:
    """Founders (+ LinkedIn) from the YC page, hiring e-mails / team profiles from the company site."""
    if d.get("yc_url"):
        h = http_get_text(d["yc_url"], 25, quiet=True) or ""
        m = re.search(r'data-page="([^"]+)"', h)
        if m:
            try:
                c = json.loads(htmllib.unescape(m.group(1)))["props"]["company"]
                d["founder_links"] = [{"name": f.get("full_name", ""), "url": f.get("linkedin_url") or "", "role": f.get("title", ""),
                                       "search": "https://www.linkedin.com/search/results/people/?keywords=" + urllib.parse.quote(f"{f.get('full_name', '')} {d['name']}")}
                                      for f in (c.get("founders") or []) if f.get("full_name")]
                d["founders"] = [f["name"] for f in d["founder_links"]]
                d["description"] = d.get("description") or c.get("one_liner", "")
                if c.get("team_size") and not d.get("size"):
                    d["size"] = {"count": int(c["team_size"]), "range": ""}
                d["company_linkedin"] = c.get("linkedin_url", "")
            except Exception:
                pass
    if d.get("website"):
        d.update(_site_contacts(d["website"], d.get("founders", [])))
    d["score"] = _startup_score(d)
    return d


def enrich_discovered_startups(limit: int = 150) -> int:
    conn = get_db()
    rows = conn.execute("SELECT key, data FROM startups WHERE enriched_at='' AND funded_on='' ORDER BY score DESC LIMIT ?", (limit,)).fetchall()
    conn.close()

    def work(r):
        try:
            return r["key"], _enrich_discovered(json.loads(r["data"]))
        except Exception as exc:
            print(f"  [enrich ✗] {r['key']}: {exc}")
            return r["key"], None

    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(work, rows))
    conn = get_db()
    for k, d in res:
        if d:
            conn.execute("UPDATE startups SET data=?, score=?, enriched_at=? WHERE key=?", (json.dumps(d), d["score"], now_utc(), k))
        else:
            conn.execute("UPDATE startups SET enriched_at=? WHERE key=?", (now_utc(), k))
    conn.commit()
    conn.close()
    return len(res)


def run_discovery(rules: dict) -> dict:
    t0 = time.time()
    conn = get_db()
    before_boards = conn.execute("SELECT COUNT(*) FROM ats_boards").fetchone()[0] + conn.execute("SELECT COUNT(*) FROM workday_boards").fetchone()[0]
    added: dict[str, int] = {}
    for src, fn in (("yc", _discover_yc), ("remoteintech", _discover_remoteintech), ("news", _discover_news),
                    ("hn", _discover_hn), ("lists", _discover_lists), ("layoffsandhires", _discover_layoffsandhires)):
        try:
            rows = fn()
        except Exception as exc:
            print(f"  [discovery ✗] {src}: {exc}")
            rows = []
        added[src] = sum(_disc_add(conn, n, w, src, i) for n, w, i in rows if n)
    li = conn.execute("SELECT slug, name FROM li_companies").fetchall()
    added["linkedin"] = sum(_disc_add(conn, r["name"] or r["slug"], "", "linkedin", {"li_slug": r["slug"]}) for r in li if (r["name"] or r["slug"]))
    conn.commit()
    if _due("discovery_commoncrawl", 24 * 7):
        urls = _discover_commoncrawl()
        n = 0
        for u in urls:
            ab, wb = ats_board_from_url(u), workday_board_from_url(u)
            if ab and ab[0] in ("greenhouse", "lever", "ashby", "keka", "zoho"):
                n += conn.execute("INSERT OR IGNORE INTO ats_boards (kind, token, source, added_at) VALUES (?,?,?,?)",
                                  (ab[0], ab[1], "discovery:commoncrawl", now_utc())).rowcount
            elif wb:
                n += conn.execute("INSERT OR IGNORE INTO workday_boards (host, tenant, site, source) VALUES (?,?,?,?)",
                                  (*wb, "discovery:commoncrawl")).rowcount
        conn.commit()
        added["commoncrawl_boards"] = n
        if urls:
            _kv_set("discovery_commoncrawl", now_utc())
    todo = conn.execute("""SELECT key, name, website, source, info FROM discovered_companies WHERE status='new'
                           ORDER BY CASE source WHEN 'yc' THEN 0 WHEN 'remoteintech' THEN 1 WHEN 'hn' THEN 2 WHEN 'news' THEN 3 WHEN 'jobboards' THEN 4 WHEN 'lists' THEN 5 WHEN 'linkedin' THEN 6 ELSE 7 END, first_seen
                           LIMIT ?""", (DISCOVERY_PER_DAY,)).fetchall()
    conn.close()

    def work(r):
        try:
            info = json.loads(r["info"] or "{}")
            board = detect_board(r["name"], r["website"], info.get("careers", ""))
            su = None
            if board and r["source"] in ("yc", "remoteintech", "news", "linkedin", "hn", "lists", "jobboards") and board.split(":")[0] in _SIMPLE_ATS:
                su = _startup_from_board(r["name"], r["website"], r["source"], info, board, rules)
            return r["key"], r["source"], board, su
        except Exception:
            return r["key"], r["source"], "", None

    with ThreadPoolExecutor(14) as ex:
        results = list(ex.map(work, todo))
    conn = get_db()
    found = 0
    new_startups = 0
    for key, src, board, su in results:
        if su:
            sk = _skey(su["name"])
            if sk and not conn.execute("SELECT 1 FROM startups WHERE key=?", (sk,)).fetchone():
                conn.execute("INSERT INTO startups (key, name, funded_on, data, score, enriched_at, created_at) VALUES (?,?,?,?,?,?,?)",
                             (sk, su["name"], "", json.dumps(su), su.get("score", 0), "", now_utc()))
                new_startups += 1
            elif sk:
                old = json.loads(conn.execute("SELECT data FROM startups WHERE key=?", (sk,)).fetchone()["data"])
                old["roles"] = su["roles"]
                conn.execute("UPDATE startups SET data=?, score=? WHERE key=?", (json.dumps(old), _startup_score(old), sk))
        if board:
            _register_detected(conn, board, f"discovery:{src}")
            found += 1
        conn.execute("UPDATE discovered_companies SET status=?, board=?, checked_at=? WHERE key=?",
                     ("board" if board else "none", board, now_utc(), key))
    conn.commit()
    after_boards = conn.execute("SELECT COUNT(*) FROM ats_boards").fetchone()[0] + conn.execute("SELECT COUNT(*) FROM workday_boards").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM discovered_companies WHERE status='new'").fetchone()[0]
    conn.close()
    summary = {"date": now_utc()[:10], "new_companies": added, "checked": len(todo), "boards_found": found,
               "boards_added": after_boards - before_boards, "boards_total": after_boards, "pending": pending,
               "startups_added": new_startups,
               "minutes": round((time.time() - t0) / 60, 1)}
    hist = json.loads(_kv_get("discovery_history") or "[]")[-29:] + [summary]
    _kv_set("discovery_history", json.dumps(hist))
    _kv_set("discovery_run", now_utc())
    try:
        summary["startups_enriched"] = enrich_discovered_startups(int(os.environ.get("STARTUP_ENRICH_DISCOVERED", "200")))
    except Exception as exc:
        print(f"  [!] startup enrichment failed: {exc}")
    print(f"[discovery] {summary}")
    return summary


def discovery_meta() -> dict:
    conn = get_db()
    try:
        boards = conn.execute("SELECT COUNT(*) FROM ats_boards").fetchone()[0]
        wd = conn.execute("SELECT COUNT(*) FROM workday_boards").fetchone()[0]
        week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        new7 = conn.execute("SELECT COUNT(*) FROM ats_boards WHERE added_at >= ?", (week,)).fetchone()[0]
        disc = conn.execute("SELECT COUNT(*) FROM discovered_companies").fetchone()[0]
        li = conn.execute("SELECT COUNT(*) FROM li_companies").fetchone()[0]
    finally:
        conn.close()
    hist = json.loads(_kv_get("discovery_history") or "[]")
    return {"boards": boards + wd, "new_7d": new7, "workday": wd, "companies_seen": disc, "li_companies": li,
            "last_discovery": _kv_get("discovery_run"), "last": hist[-1] if hist else {}}


# ═══════════════════════════════════════════════════════════════════════════
#  CLOUD → LOCAL SYNC  (GitHub Actions publishes its jobs.db to the `data` branch; we merge it in)
# ═══════════════════════════════════════════════════════════════════════════
_SYNC_TABLES = {"jobs": "job_hash", "posts": "post_hash", "startups": "key", "seen_jobs": "url", "rejected_urls": "url",
                "company_contacts": "company", "ats_boards": None, "careers_pages": "url", "workday_boards": None}


def sync_from_cloud() -> dict:
    """Merge the GitHub-runner database into the local one. Your Applied / Saved / Rejected marks are never touched;
    jobs the cloud later rejected (closed, filter change) are removed from your 'new' list."""
    import subprocess
    import tempfile
    try:
        remotes = subprocess.run(["git", "remote"], cwd=str(BASE_DIR), capture_output=True, text=True).stdout.split()
        remote = os.environ.get("SYNC_REMOTE") or ("live" if "live" in remotes else "origin")
        r = subprocess.run(["git", "fetch", "--quiet", remote, "data"], cwd=str(BASE_DIR), capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            return {"ok": False, "error": (r.stderr or "git fetch failed").strip()[:200]}
        blob = subprocess.run(["git", "show", f"{remote}/data:jobs.db"], cwd=str(BASE_DIR), capture_output=True, timeout=60)
        if blob.returncode != 0 or len(blob.stdout) < 1000:
            return {"ok": False, "error": "no cloud database on the data branch yet"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.write(blob.stdout)
    tmp.close()
    conn = get_db()
    added_jobs = added_posts = removed = 0
    try:
        conn.execute("ATTACH DATABASE ? AS cloud", (tmp.name,))
        cloud_tables = {r[0] for r in conn.execute("SELECT name FROM cloud.sqlite_master WHERE type='table'")}
        before_jobs = conn.execute("SELECT COUNT(*) FROM main.jobs").fetchone()[0]
        before_posts = conn.execute("SELECT COUNT(*) FROM main.posts").fetchone()[0]
        for table in _SYNC_TABLES:
            if table not in cloud_tables:
                continue
            lc = [r[1] for r in conn.execute(f"PRAGMA main.table_info({table})")]
            cc = {r[1] for r in conn.execute(f"PRAGMA cloud.table_info({table})")}
            cols = [c for c in lc if c in cc and c != "id"]
            if not cols:
                continue
            cl = ",".join(cols)
            conn.execute(f"INSERT OR IGNORE INTO main.{table} ({cl}) SELECT {cl} FROM cloud.{table}")
        # cloud removed these (closed / now fail a filter) → drop them from the local 'new' list
        if "rejected_urls" in cloud_tables:
            for row in conn.execute("SELECT j.id, j.direct_apply_url FROM main.jobs j WHERE j.status='new'").fetchall():
                if conn.execute("SELECT 1 FROM cloud.rejected_urls WHERE url=?", (norm_url(row["direct_apply_url"]),)).fetchone() \
                        and not conn.execute("SELECT 1 FROM cloud.jobs WHERE job_hash=?", (make_hash(norm_url(row["direct_apply_url"])),)).fetchone():
                    conn.execute("DELETE FROM main.jobs WHERE id=?", (row["id"],))
                    removed += 1
        # same opening shared by several people → keep the oldest copy
        conn.execute("""DELETE FROM main.posts WHERE status='new' AND IFNULL(dedup_key,'')!='' AND id NOT IN
                        (SELECT MIN(id) FROM main.posts WHERE IFNULL(dedup_key,'')!='' GROUP BY dedup_key)""")
        conn.commit()
        added_jobs = conn.execute("SELECT COUNT(*) FROM main.jobs").fetchone()[0] - before_jobs + removed
        added_posts = conn.execute("SELECT COUNT(*) FROM main.posts").fetchone()[0] - before_posts
        conn.execute("DETACH DATABASE cloud")
    except Exception as exc:
        return {"ok": False, "error": f"merge failed: {exc}"}
    finally:
        conn.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    return {"ok": True, "new_jobs": max(0, added_jobs), "new_posts": max(0, added_posts), "removed": removed}


def _sync_loop() -> None:
    """While the dashboard is open, quietly merge in whatever the GitHub runner found (every 10 minutes)."""
    if not (BASE_DIR / ".git").exists() or os.environ.get("AUTO_SYNC", "1") == "0":
        return
    time.sleep(6)
    while True:
        res = sync_from_cloud()
        print(f"[sync] {res}")
        time.sleep(600)


def _last_scan_age_secs() -> float:
    conn = get_db()
    try:
        r = conn.execute("SELECT finished_at FROM scan_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    if not r or not r["finished_at"]:
        return float("inf")
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(r["finished_at"])).total_seconds()
    except Exception:
        return float("inf")


def notify(title: str, message: str) -> None:
    """macOS desktop notification (no-op elsewhere)."""
    try:
        import platform
        import subprocess
        if platform.system() == "Darwin":
            subprocess.run(["osascript", "-e", f'display notification "{message}" with title "{title}" sound name "Glass"'],
                           timeout=10, capture_output=True)
    except Exception:
        pass


def _background_loop() -> None:
    """24/7 runner: scan now if the last scan is stale (e.g. after a reboot / the Mac waking up), then keep scanning
    every SCAN_INTERVAL measured from each scan's start. A crashed scan never stops the loop."""
    time.sleep(4)  # let the HTTP server bind first
    if SCAN_INTERVAL <= 0:
        print("[loop] local scanning is off (SCAN_INTERVAL_MINUTES=0) — the GitHub runner does the 24/7 scanning")
        return
    while True:
        age = _last_scan_age_secs()
        if age >= SCAN_INTERVAL - 30:
            print(f"[loop] last scan {('never' if age == float('inf') else f'{int(age // 60)} min ago')} → scanning now")
            t0 = time.time()
            try:
                s = run_full_scan()
                if not s.get("busy") and (s.get("jobs_added") or s.get("posts_added")):
                    notify("Job Hunter", f"{s.get('jobs_added', 0)} new jobs · {s.get('posts_added', 0)} new posts")
            except Exception as exc:
                print(f"[loop] scan crashed: {exc}")
            wait = max(300, SCAN_INTERVAL - (time.time() - t0))
        else:
            wait = max(60, SCAN_INTERVAL - age)
        print(f"[loop] next scan in {int(wait // 60)} min")
        time.sleep(wait)


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
.src-ats{background:#0b1f1a;color:#5eead4;border-color:#0f766e;}
.contacts{display:flex;flex-wrap:wrap;gap:5px;align-items:center;margin-top:5px;}
.contacts .clabel{font-size:11px;color:var(--muted);}
a.chip{font-size:11px;padding:2px 8px;border-radius:999px;text-decoration:none;
       background:#10203a;color:#93c5fd;border:1px solid #1d4ed8;}
a.chip.search{background:#161b26;color:#94a3b8;border-color:#334155;}
.notes{font-size:11px;color:var(--muted);word-break:break-all;}
.badge.direct{color:#86efac;border-color:#15803d;background:#052e16;}
.badge.board{color:#fcd34d;border-color:#92400e;background:#2d1a04;}
.badge.lock{color:#fca5a5;border-color:#991b1b;background:#2d0808;}
a.apply-btn.board{background:#92400e;}
#scanBanner{display:none;background:#1e1b4b;border:1px solid #4338ca;color:#c7d2fe;border-radius:9px;
            padding:9px 13px;font-size:13px;margin-bottom:12px;}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div>
    <h1>🎯 Job Hunter</h1>
    <div class="sub" id="lastScan">GitHub scans in the cloud: light every 4 h, full twice a day · only direct company links or apply-by-email are shown</div>
  </div>
  <div class="hbtns">
    <button class="sec" onclick="toggleScan()">+ Manual Scan</button>
    <button id="scanAllBtn" onclick="triggerAutoScan()">🔍 Scan Now</button>
    <button class="sec" onclick="syncCloud()" id="syncBtn">☁️ Sync Cloud</button>
    <button class="sec" onclick="refresh()">↻ Refresh</button>
  </div>
</header>

<div id="scanBanner"></div>
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
  <button class="tab" id="tabStartups" onclick="switchTab('startups')">💰 Funded startups</button>
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
      <option value="deleted">Deleted</option>
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
      <option value="huntyourtribe">HuntYourTribe</option>
      <option value="urbanodx">RoleFinder (UrbanoDX)</option>
      <option value="jsgurujobs">JSGuruJobs</option>
      <option value="matcha">Matcha</option>
      <option value="ats">ATS boards (Greenhouse/Lever/Ashby)</option>
      <option value="firecrawl">Firecrawl</option>
      <option value="career">Career Pages</option>
      <option value="manual">Manual Scan</option>
    </select>
    <select id="linkFilter" onchange="renderJobs()">
      <option value="all">All Links</option>
      <option value="direct">Direct company links</option>
      <option value="email">Apply by email</option>
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

<!-- ─── FUNDED STARTUPS PANEL ─────────────────────────────────── -->
<div id="panelStartups" style="display:none">
  <div class="stats" id="stStats"></div>
  <div class="controls">
    <input type="text" id="sq" placeholder="Search startup, sector, investor, city…" oninput="renderStartups()"/>
    <select id="sTech" onchange="renderStartups()">
      <option value="tech" selected>Tech startups</option>
      <option value="tiny">👥 Tiny: under 30 people</option>
      <option value="small">👥 Small: 50 people or fewer</option>
      <option value="all">All sectors</option>
    </select>
    <select id="sHiring" onchange="renderStartups()">
      <option value="all">Any hiring signal</option>
      <option value="dev">Open developer roles</option>
      <option value="any">Roles or hiring posts</option>
    </select>
    <select id="sAge" onchange="renderStartups()">
      <option value="99999" selected>All startups (worldwide)</option>
      <option value="183">Funded in last 6 months</option>
      <option value="90">Last 3 months</option>
      <option value="30">Last 30 days</option>
    </select>
    <select id="sStFilter" onchange="renderStartups()">
      <option value="all">All statuses</option>
      <option value="new" selected>New</option>
      <option value="saved">Saved</option>
      <option value="contacted">Contacted</option>
    </select>
  </div>
  <div class="grid" id="startups"><div class="loader">Loading funded startups…</div></div>
</div>
</div>

<script>
let allJobs=[], allPosts=[], allStartups=[], META={}, tab='jobs';

const SRC_CLASS = {
  remoteok:'src-remoteok', remotive:'src-remotive', arbeitnow:'src-arbeitnow',
  himalayas:'src-himalayas', jobicy:'src-jobicy', weworkremotely:'src-wwr', ats:'src-ats',
  themuse:'src-muse', firecrawl:'src-firecrawl',
};

function renderContacts(s){
  let arr=[]; try{ arr=JSON.parse(s||'[]'); }catch(e){}
  if(!arr.length) return '';
  return `<div class="contacts"><span class="clabel">👥 Contacts:</span>`+arr.map(c=>{
    const lbl = c.kind==='profile' ? `${esc(c.role)}: ${esc(c.name||'profile')}` : `${esc(c.role)} (search)`;
    return `<a class="chip ${c.kind==='search'?'search':''}" href="${esc(c.url)}" target="_blank" rel="noopener" title="${esc(c.note)}">${lbl} ↗</a>`;
  }).join('')+`</div>`;
}

function srcClass(src){
  for(const [k,v] of Object.entries(SRC_CLASS)) if(src.includes(k)) return v;
  if(src.includes('career')) return 'src-career';
  return '';
}

function switchTab(t){
  tab=t;
  try{ localStorage.setItem('jh_tab',t); if(location.hash!=='#'+t) history.replaceState(null,'','#'+t); }catch(e){}
  ['jobs','posts','startups'].forEach(x=>{
    document.getElementById('panel'+x[0].toUpperCase()+x.slice(1)).style.display = x===t?'':'none';
    document.getElementById('tab'+x[0].toUpperCase()+x.slice(1)).className='tab'+(x===t?' active':'');
  });
}

function toggleScan(){ document.getElementById('scanBox').classList.toggle('open'); }

async function syncCloud(){
  const btn=document.getElementById('syncBtn');
  btn.textContent='⏳ Syncing…'; btn.disabled=true;
  try{
    const r=await fetch('/api/sync',{method:'POST'});
    const d=await r.json();
    btn.textContent = d.ok ? ('✅ '+(d.output||'Synced')) : ('⚠️ '+(d.error||'Sync failed'));
    setTimeout(()=>{ btn.textContent='☁️ Sync Cloud'; btn.disabled=false; },5000);
  }catch(e){
    btn.textContent='☁️ Sync Cloud'; btn.disabled=false;
  }
  await refresh();
}

async function loadStartups(){
  try{ const r=await fetch('/api/startups'); allStartups=await r.json(); }catch(e){ allStartups=[]; }
  renderStartups();
}
function daysSince(d){ return d ? Math.floor((Date.now()-new Date(d).getTime())/86400000) : 999; }
function renderStartups(){
  const q=(document.getElementById('sq').value||'').toLowerCase(), tech=document.getElementById('sTech').value,
        hire=document.getElementById('sHiring').value, age=+document.getElementById('sAge').value, st=document.getElementById('sStFilter').value;
  const f=allStartups.filter(s=>{
    if(tech==='tech' && s.tech===false) return false;
    if(tech==='tiny' && !s.tiny) return false;
    if(tech==='small' && !s.small) return false;
    if(daysSince(s.funded_on)>age) return false;
    if(st!=='all' && (s.status||'new')!==st) return false;
    const dev=(s.roles&&s.roles.dev||[]).length;
    if(hire==='dev' && !dev) return false;
    if(hire==='any' && !dev && !(s.roles&&s.roles.total) && !(s.hiring_posts||[]).length) return false;
    if(q){ const hay=[s.name,s.sector,s.subsector,s.investors,s.lead,s.hq,s.description,(s.founders||[]).join(' ')].join(' ').toLowerCase(); if(!hay.includes(q)) return false; }
    return true;
  });
  const withDev=allStartups.filter(s=>(s.roles&&s.roles.dev||[]).length).length;
  document.getElementById('stStats').innerHTML=`
    <div class="stat"><div class="num">${allStartups.length}</div><div class="lbl">Startups</div></div>
    <div class="stat"><div class="num">${withDev}</div><div class="lbl">With open dev roles</div></div>
    <div class="stat"><div class="num">${allStartups.filter(s=>(s.hiring_posts||[]).length).length}</div><div class="lbl">Posted hiring on LinkedIn</div></div>
    <div class="stat"><div class="num">${f.length}</div><div class="lbl">Shown</div></div>`;
  const el=document.getElementById('startups');
  if(!f.length){ el.innerHTML='<div class="post"><div class="sub">No startups match — they are enriched a batch at a time, more appear after each full scan.</div></div>'; return; }
  const link=(u,t)=>u?`<a class="view-btn" href="${esc(u)}" target="_blank" rel="noopener">${t} ↗</a>`:'';
  const stPage=pageSlice('startups',f,[q,tech,hire,age,st].join('|'));
  el.innerHTML=pagerHtml('startups',f.length,'renderStartups')+stPage.map(s=>{
    const dev=(s.roles&&s.roles.dev)||[];
    const founders=(s.founder_links||[]).map(x=> x.url ? `<a href="${esc(x.url)}" target="_blank">${esc(x.name)} (LinkedIn)</a>` : `${esc(x.name)} <a href="${esc(x.search)}" target="_blank" style="opacity:.7">(search)</a>`).join(' · ');
    const team=(s.profiles||[]).filter(p=>p.role!=='Founder').slice(0,4).map(p=>`<a href="${esc(p.url)}" target="_blank">${esc(p.name||'profile')}</a>`).join(' · ');
    const emails=(s.emails||[]).map(e=>`<a href="mailto:${esc(e.email)}">${esc(e.email)}</a> <span class="badge" style="font-size:10px">${esc(e.type)}</span>`).join(' ');
    return `<div class="job">
  <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">
    <div><div class="post-name">${esc(s.name)}</div>
      <div class="sub">${esc([s.sector,s.subsector,s.model].filter(Boolean).join(' · '))}${s.hq?' · 📍 '+esc(s.hq):''}${s.founded?' · since '+esc(s.founded):''}</div></div>
    <div class="badges"><span class="badge score">⭐ ${s.score||0}/10</span>
      ${s.funded_on?`<span class="badge direct">💰 ${esc(s.amount||'undisclosed')} ${esc(s.round||'')}</span><span class="badge">📅 ${esc(s.funded_on)}</span>`
                   :`<span class="badge direct">${s.source==='yc'?'🟧 YC '+esc(s.round||''):esc(s.source||'startup')}</span>`}${s.size&&s.size.count?`<span class="badge" title="people on LinkedIn">👥 ${s.size.count}${s.size.range?' ('+esc(s.size.range)+')':''}</span>`:''}${statusBadge(s.status||'new')}</div>
  </div>
  ${s.description?`<div class="reason">${esc(s.description.slice(0,260))}</div>`:''}
  <div class="meta">${s.investors?`<span>🏦 ${esc(s.investors)}</span>`:''}</div>
  <div class="meta" style="flex-direction:column;align-items:flex-start;gap:4px">
    ${founders?`<span>👤 Founders: ${founders}</span>`:''}
    ${team?`<span>🧑‍💻 Team on their site: ${team}</span>`:''}
    ${emails?`<span>✉ ${emails}</span>`:''}
    <span>🔎 <a href="${esc(s.recruiter_search||'')}" target="_blank">Find their recruiter / HR on LinkedIn</a></span>
  </div>
  ${dev.length?`<div class="notes">🟢 Open developer roles (${dev.length}${s.roles.total?` of ${s.roles.total} open`:''}): ${dev.map(r=>`<a href="${esc(r.url)}" target="_blank">${esc(r.title)}</a>${r.location?' ('+esc(r.location)+')':''}${r.posted?' <span style="opacity:.7">· posted '+esc(r.posted)+'</span>':''}`).join(' · ')}</div>`
             : (s.roles&&s.roles.total?`<div class="notes">${s.roles.total} open roles on their careers board (none match your stack yet)</div>`:'')}
  ${(s.hiring_posts||[]).map(p=>`<div class="notes">📢 Hiring post ${esc(p.date||'')}: ${esc(p.text.slice(0,160))}… <a href="${esc(p.url)}" target="_blank">View ↗</a></div>`).join('')}
  <div class="actions">
    ${link(s.careers,'Careers page')}${link(s.website,'Website')}${link(s.linkedin,'LinkedIn')}${link(s.inc42,'Profile')}${link(s.article,'Funding news')}
    <button class="ok"   onclick="setStartupStatus('${esc(s.key)}','contacted')">✉ Contacted</button>
    <button class="warn" onclick="setStartupStatus('${esc(s.key)}','saved')">★ Save</button>
    <button class="bad"  onclick="setStartupStatus('${esc(s.key)}','rejected')">✗ Hide</button>
  </div>
</div>`;}).join('')+pagerHtml('startups',f.length,'renderStartups');
}
async function setStartupStatus(key,s){
  await fetch('/api/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:key,status:s,type:'startup'})});
  await loadStartups();
}

async function loadMeta(){ try{ META=await (await fetch('/api/meta')).json(); }catch(e){ META={}; } }

async function refresh(){
  await loadMeta();
  await Promise.all([loadJobs(), loadPosts(), loadStartups()]);
  document.getElementById('lastScan').textContent =
    'Last refreshed: '+new Date().toLocaleTimeString()+' · '+(window.__jhNote ? window.__jhNote() : 'auto-syncs with the cloud every 10 min');
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
  const nDirect=allJobs.filter(j=>j.link_type==='direct').length;
  const nEmail=allJobs.filter(j=>j.link_type==='email').length;
  document.getElementById('jobStats').innerHTML=`
    <div class="stat"><div class="num">${nDirect}</div><div class="lbl">Direct links</div></div>
    <div class="stat"><div class="num">${nEmail}</div><div class="lbl">Apply by email</div></div>
    <div class="stat" title="Company job boards polled for new roles; grows every day through discovery"><div class="num">${META.boards||'–'}</div><div class="lbl">Companies tracked${META.new_7d?` (+${META.new_7d} this week)`:''}</div></div>
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
const PAGE_SIZE=20, PG={jobs:1,posts:1,startups:1}, PSIG={};
function pageSlice(key,arr,sig){
  if(PSIG[key]!==sig){ PSIG[key]=sig; PG[key]=1; }
  const pages=Math.max(1,Math.ceil(arr.length/PAGE_SIZE)); if(PG[key]>pages) PG[key]=pages;
  return arr.slice((PG[key]-1)*PAGE_SIZE, PG[key]*PAGE_SIZE);
}
function pagerHtml(key,total,fn){
  const pages=Math.max(1,Math.ceil(total/PAGE_SIZE)); if(pages<=1) return '';
  const p=PG[key];
  return `<div style="display:flex;gap:10px;align-items:center;justify-content:center;margin:12px 0">
    <button class="sec" ${p<=1?'disabled':''} onclick="goPage('${key}',-1,'${fn}')">← Prev</button>
    <span style="font-size:13px">Page ${p} of ${pages} · ${total} results</span>
    <button class="sec" ${p>=pages?'disabled':''} onclick="goPage('${key}',1,'${fn}')">Next →</button></div>`;
}
function goPage(key,d,fn){ PG[key]+=d; window[fn](); window.scrollTo({top:0,behavior:'smooth'}); }
function statusBadge(s){ return `<span class="badge s${s[0]}">${esc(s.toUpperCase())}</span>`; }

function renderJobs(){
  const q   = document.getElementById('q').value.toLowerCase();
  const st  = document.getElementById('stFilter').value;
  const cat = document.getElementById('catFilter').value;
  const src = document.getElementById('srcFilter').value;
  const minScore = parseInt(document.getElementById('scoreFilter').value)||0;
  const lk  = document.getElementById('linkFilter').value;

  const f = allJobs.filter(j=>{
    if(j.status==='deleted' && st!=='deleted') return false;
    if(st!=='all' && j.status!==st) return false;
    if(cat!=='all' && j.role_category!==cat) return false;
    if(src!=='all' && !j.discovery_source.toLowerCase().includes(src)) return false;
    if(minScore && j.match_score < minScore) return false;
    if(lk!=='all' && (j.link_type||'direct')!==lk) return false;
    if(q){
      const hay=`${j.company} ${j.title} ${j.location} ${j.match_reason} ${j.discovery_source} ${j.salary}`.toLowerCase();
      if(!hay.includes(q)) return false;
    }
    return true;
  });

  const el=document.getElementById('jobs');
  if(!f.length){ el.innerHTML='<div class="job"><div class="sub">No jobs match current filter.</div></div>'; return; }

  const jobsPage=pageSlice('jobs',f,[q,st,cat,src,minScore,lk].join('|'));
  el.innerHTML=pagerHtml('jobs',f.length,'renderJobs')+jobsPage.map(j=>`
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
      <span class="badge ${j.link_type==='email'?'board':'direct'}">${j.link_type==='email'?'✉ apply by email':'✅ direct link'}</span>
      ${j.locked?'<span class="badge lock">🔒 may need login</span>':''}
      <span class="badge">${esc(j.role_category)}</span>
      ${statusBadge(j.status)}
      <span class="badge ${srcClass(j.discovery_source)}" style="font-size:10px">${esc(j.discovery_source.split(':')[0])}</span>
    </div>
  </div>
  <div class="meta">
    <span>📍 ${esc(j.location||'Remote')}</span>
    <span>🧑‍💻 ${esc(j.experience_req||'not stated')}</span>
    ${j.salary?`<span>💰 ${esc(j.salary)}</span>`:''}
    ${j.posted_date?`<span>📅 ${esc(j.posted_date)}</span>`:''}
  </div>
  <div class="reason">${esc(j.match_reason)}</div>
  ${renderContacts(j.contacts)}
  ${j.notes?`<div class="notes">${esc(j.notes)}</div>`:''}
  <div class="actions">
    ${j.link_type==='email'
      ? `<a class="apply-btn" href="mailto:${esc(j.apply_email)}?subject=${encodeURIComponent('Application: '+j.title)}">✉ Email ${esc(j.apply_email)}</a>`
      : `<a class="apply-btn" href="${esc(j.direct_apply_url)}" target="_blank" rel="noopener">Direct Apply ↗</a>${j.apply_email?`<a class="view-btn" href="mailto:${esc(j.apply_email)}">✉ ${esc(j.apply_email)}</a>`:''}`}
    ${j.link_status==='unverified'?'<span class="badge">couldn\'t verify page is live</span>':''}
    <button class="ok"   onclick="setJobStatus(${j.id},'applied')">✓ Applied</button>
    <button class="warn" onclick="setJobStatus(${j.id},'saved')">★ Save</button>
    <button class="bad"  onclick="setJobStatus(${j.id},'rejected')">✗ Reject</button>
    <button class="sec"  onclick="setJobStatus(${j.id},'deleted')" title="Remove from the dashboard">🗑 Delete</button>
  </div>
</div>`).join('')+pagerHtml('jobs',f.length,'renderJobs');
}

function renderPosts(){
  const q   = document.getElementById('pq').value.toLowerCase();
  const src = document.getElementById('pSrcFilter').value;
  const st  = document.getElementById('pStFilter').value;

  const f = allPosts.filter(p=>{
    if(p.status==='deleted' && st!=='deleted') return false;
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

  const postsPage=pageSlice('posts',f,[q,src,st].join('|'));
  el.innerHTML=pagerHtml('posts',f.length,'renderPosts')+postsPage.map(p=>`
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
    <button class="sec"  onclick="setPStatus(${p.id},'deleted')" style="font-size:12px">🗑 Delete</button>
  </div>
</div>`).join('')+pagerHtml('posts',f.length,'renderPosts');
}

async function setJobStatus(id,s){
  await fetch('/api/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,status:s,type:'job'})});
  await loadJobs();
}
async function setPStatus(id,s){
  await fetch('/api/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,status:s,type:'post'})});
  await loadPosts();
}

async function pollScan(){
  const banner=document.getElementById('scanBanner');
  try{
    const r=await fetch('/api/scan-status'); const d=await r.json();
    if(d.running){
      banner.style.display='block';
      banner.textContent='⏳ Scan running — '+(d.stage||'working')+' … results appear here automatically.';
      await loadJobs(); await loadPosts();
      return true;
    }
    banner.style.display='none';
  }catch(e){}
  return false;
}
let scanTimer=null;
function watchScan(){
  if(scanTimer) return;
  scanTimer=setInterval(async()=>{
    const running=await pollScan();
    if(!running){
      clearInterval(scanTimer); scanTimer=null;
      document.getElementById('scanAllBtn').textContent='🔍 Scan Now';
      document.getElementById('scanAllBtn').disabled=false;
      const r=await fetch('/api/scan-status'); const d=await r.json();
      await refresh();
      if(d.summary && !d.summary.error){
        const m=d.summary;
        alert(`✅ Scan complete!\n• ${m.jobs_added} new jobs (${m.direct_links||0} direct, ${m.board_links||0} board links)\n• ${m.posts_added} new posts\n• ${m.duplicates_skipped} already seen\n• Total in DB: ${m.total_jobs_in_db}`);
      }
    }
  },3000);
}
async function triggerScan(){
  const st=document.getElementById('scanStatus');
  const raw=document.getElementById('customUrls').value.trim();
  const urls=raw?raw.split('\n').map(s=>s.trim()).filter(Boolean):null;
  const r=await fetch('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({urls})});
  const d=await r.json();
  st.textContent=d.started?'⏳ Scan started…':'A scan is already running';
  watchScan();
}
async function triggerAutoScan(){
  const btn=document.getElementById('scanAllBtn');
  btn.textContent='⏳ Scanning…'; btn.disabled=true;
  await fetch('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({urls:null})});
  watchScan();
}

{ let t0=''; try{ t0=(location.hash||'').slice(1)||localStorage.getItem('jh_tab')||''; }catch(e){}
  if(['jobs','posts','startups'].includes(t0)) switchTab(t0); }      // a refresh stays on the tab you were on
refresh();
pollScan().then(r=>{ if(r) watchScan(); });
setInterval(()=>{ if(!scanTimer) refresh(); }, 60000);   // auto-refresh so new jobs show up without a reload
</script>
</body>
</html>
"""


# ─────────────────────────── HTTP server ────────────────────────────────────

DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "")


class DashboardHandler(BaseHTTPRequestHandler):
    def _authorized(self) -> bool:
        """When DASH_PASSWORD is set (cloud deploy), every request needs HTTP Basic auth (any user name)."""
        if not DASH_PASSWORD or self.path.split("?")[0] == "/healthz":
            return True
        import base64
        import hmac
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                pw = base64.b64decode(hdr[6:]).decode("utf-8", "replace").split(":", 1)[-1]
                if hmac.compare_digest(pw, DASH_PASSWORD):
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Job Hunter"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _json(self, data: object, code: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/healthz":
            self._json({"ok": True})
            return
        if not self._authorized():
            return

        if path == "/api/jobs":
            conn = get_db()
            rows = conn.execute(
                "SELECT * FROM jobs WHERE link_type!='board' ORDER BY match_score DESC, id DESC"
            ).fetchall()
            conn.close()
            self._json([dict(r) for r in rows])
            return

        if path == "/api/meta":
            self._json(discovery_meta())
            return

        if path == "/api/startups":
            conn = get_db()
            rows = conn.execute("SELECT key, name, funded_on, data, score, status FROM startups "
                                "ORDER BY score DESC, funded_on DESC LIMIT 8000").fetchall()
            conn.close()
            self._json([{**json.loads(r["data"]), "key": r["key"], "score": r["score"], "status": r["status"]} for r in rows
                        if startup_visible(json.loads(r["data"]), r["funded_on"])])
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
            self._json({"total_jobs": total, "last_scan": dict(last) if last else {},
                        "scan": {k: SCAN_STATE[k] for k in ("running", "stage", "started")}})
            return

        if path == "/api/scan-status":
            self._json(dict(SCAN_STATE))
            return

        # Serve dashboard for all other GET paths
        body = DASHBOARD_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if not self._authorized():
            return
        origin = self.headers.get("Origin", "")
        if origin and not re.match(r"https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$", origin) and SERVE_HOST in ("127.0.0.1", "localhost"):
            self._json({"error": "forbidden origin"}, 403)
            return
        length  = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode() or "{}")

        if self.path == "/api/status":
            if payload.get("status") not in ("new", "saved", "applied", "rejected", "contacted", "deleted"):
                self._json({"error": "bad status"}, 400)
                return
            conn = get_db()
            rec_type = payload.get("type","job")
            if rec_type == "startup":
                conn.execute("UPDATE startups SET status=? WHERE key=?", (payload["status"], str(payload["id"])))
            elif rec_type == "post":
                conn.execute("UPDATE posts SET status=? WHERE id=?",
                             (payload["status"], int(payload["id"])))
            else:
                if payload["status"] == "deleted":
                    r = conn.execute("SELECT direct_apply_url FROM jobs WHERE id=?", (int(payload["id"]),)).fetchone()
                    if r:
                        conn.execute("INSERT OR IGNORE INTO seen_jobs (url, first_seen) VALUES (?,?)", (norm_url(r["direct_apply_url"]), now_utc()))
                conn.execute("UPDATE jobs SET status=? WHERE id=?",
                             (payload["status"], int(payload["id"])))
            conn.commit(); conn.close()
            self._json({"ok": True})
            return

        if self.path == "/api/sync":
            res = sync_from_cloud()
            if res.get("ok"):
                self._json({"ok": True, "output": f"+{res['new_jobs']} jobs, +{res['new_posts']} posts, {res['removed']} closed/removed"})
            else:
                self._json({"ok": False, "error": res.get("error", "sync failed")})
            return

        if self.path == "/api/scan":
            started = start_scan_async(payload.get("urls"))
            self._json({"started": started, "running": SCAN_STATE["running"]})
            return

        self._json({"error": "Not found"}, 404)

    def log_message(self, *_) -> None:
        return   # silence access logs


# ─────────────────────────── entry point ────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Job Hunter v3")
    parser.add_argument("--init-db",    action="store_true")
    parser.add_argument("--scan",       action="store_true", help="Run one-off scan and exit")
    parser.add_argument("--mode",       choices=["light", "full", "discovery"], help="With --scan: light = job APIs only, full = everything")
    parser.add_argument("--serve",      action="store_true", help="Start dashboard + discovery loop")
    parser.add_argument("--export-static", metavar="DIR", help="Write the encrypted static dashboard (for Vercel) to DIR and exit")
    parser.add_argument("--discover",   action="store_true", help="Run the daily discovery step now and exit")
    parser.add_argument("--startups",   action="store_true", help="Collect + enrich funded startups and exit")
    parser.add_argument("--posts",      action="store_true", help="Pull + vet hiring posts only (LinkedIn via Apify) and exit")
    parser.add_argument("--port",       type=int, default=SERVE_PORT)
    parser.add_argument("--host",       default=SERVE_HOST,
                        help="Bind host (use 0.0.0.0 for cloud/public access)")
    args = parser.parse_args()

    init_db()

    if args.export_static:
        sys.exit(0 if export_static(args.export_static) else 1)

    if args.discover:
        run_discovery(load_rules())
        return

    if args.startups:
        collect_funded_startups(load_rules())
        return

    if args.posts:
        conn = get_db()
        added, rej = ingest_posts(conn, load_rules(), (collect_linkedin_apify,))
        print(f"posts added: {added}")
        return

    if args.mode:
        globals()["SCAN_MODE"] = args.mode
    if args.scan:
        summary = run_full_scan()
        print("Scan complete:", json.dumps(summary, indent=2))
        return

    if args.serve or (not args.init_db):
        t = threading.Thread(target=_background_loop, daemon=True)
        t.start()
        threading.Thread(target=_sync_loop, daemon=True).start()
        host = args.host
        port = args.port
        server = ThreadingHTTPServer((host, port), DashboardHandler)
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
