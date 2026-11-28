"""
extra_sources.py -- extra public job sources for job_hunter.py (stdlib only).
Fetchers researched and live-tested 2026-11-27 (SmartRecruiters, Workable, Recruitee, Teamtailor, Personio,
Rippling, Gem, Breezy, Freshteam, Oracle HCM, Eightfold, Phenom, SuccessFactors, Workable search, Getro,
Remote Rocketship, Google Careers).

Every fetch_<kind>(token, **opts) returns a list of dicts:
    {company, title, url, location, posted_date ("YYYY-MM-DD" or ""), description, source}
`url` is always the company's own ATS / careers posting (never a job-board page), except
for the DISCOVERY-ONLY sources at the bottom (hirist / instahyre / cutshort / yc), which
return the board page in `url` and are meant to feed company names into the resolvers.

Verified live with curl on 2026-11-27 (see sources.md). All endpoints are public and
unauthenticated. Be polite: the main loop should rotate tokens, not poll all at once.

Token formats (string passed as `token`):
    workday          "host|tenant|site"            e.g. "bdx.wd1.myworkdayjobs.com|bdx|EXTERNAL_CAREER_SITE_INDIA"
    oracle           "host|siteNumber"             e.g. "jpmc.fa.oraclecloud.com|CX_1001"
    eightfold        "base_url|domain"             e.g. "https://apply.careers.microsoft.com|microsoft.com"
    phenom           "base_url|country|lang"       e.g. "https://jobs-ta.pwc.com|global|en"
    successfactors   "host"                        e.g. "southasiacareers.deloitte.com"
    getro            "collection_id|board_host"    e.g. "8672|jobs.accel.com"
    everything else  the plain slug                e.g. "Freshworks", "ai-accountant", "innovaccer"
"""
from __future__ import annotations

import gzip
import html as _html
import json
import re
import ssl
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ── HTTP ──────────────────────────────────────────────────────────────────────
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _ssl_ctx() -> ssl.SSLContext:
    """python.org macOS builds ship without CA certs -- same fix job_hunter.py uses."""
    cands = []
    try:
        import certifi  # type: ignore
        cands.append(certifi.where())
    except Exception:
        pass
    cands += ["/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/opt/homebrew/etc/ca-certificates/cert.pem"]
    for c in cands:
        try:
            return ssl.create_default_context(cafile=c)
        except Exception:
            continue
    return ssl.create_default_context()


_CTX = _ssl_ctx()


def _req(url: str, data: dict | None = None, headers: dict | None = None, timeout: int = 25,
         method: str | None = None) -> tuple[int, str]:
    h = {"User-Agent": UA, "Accept": "application/json, text/html, application/xml;q=0.9, */*;q=0.8",
         "Accept-Encoding": "gzip"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    r = urllib.request.Request(url, data=body, headers=h, method=method or ("POST" if body else "GET"))
    try:
        with urllib.request.urlopen(r, timeout=timeout, context=_CTX) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception:
        return 0, ""


def _json(url: str, data: dict | None = None, headers: dict | None = None, timeout: int = 25):
    code, txt = _req(url, data, headers, timeout)
    if code != 200:
        return None
    try:
        return json.loads(txt)
    except Exception:
        return None


def _text(url: str, headers: dict | None = None, timeout: int = 25) -> str:
    code, txt = _req(url, None, headers, timeout)
    return txt if code == 200 else ""


# ── helpers ───────────────────────────────────────────────────────────────────
def html_to_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|li|div|h\d|tr)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def _d(v) -> str:
    """Anything date-like -> YYYY-MM-DD ('' if unknown). Accepts ISO strings, epoch s/ms."""
    if v in (None, "", 0):
        return ""
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        n = float(v)
        if n > 1e11:
            n /= 1000
        try:
            return datetime.fromtimestamp(n, timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return ""
    s = str(v).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%a %b %d %H:%M:%S %Z %Y", "%b %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except Exception:
            continue
    return ""


def _rel_date(s: str) -> str:
    """Workday-style 'Posted Today' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago' -> date ('' for 30+)."""
    s = (s or "").lower()
    today = datetime.now(timezone.utc).date()
    if "today" in s:
        return today.isoformat()
    if "yesterday" in s:
        return (today - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*days?", s)
    if m and "+" not in s:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    return ""


def _rec(company, title, url, location="", posted="", desc="", source="") -> dict:
    return {"company": (company or "").strip(), "title": (title or "").strip(), "url": url or "",
            "location": (location or "").strip(), "posted_date": posted or "",
            "description": desc or "", "source": source}


INDIA_RE = re.compile(r"\b(india|bengaluru|bangalore|hyderabad|pune|chennai|mumbai|gurgaon|gurugram|noida|delhi|"
                      r"kolkata|ahmedabad|kochi|jaipur|indore|coimbatore|trivandrum|thiruvananthapuram|chandigarh|"
                      r"mohali|nagpur|vadodara|surat|bhubaneswar|mysore|mysuru|visakhapatnam|IND|IN)\b", re.I)


def india_or_remote(loc: str) -> bool:
    """Cheap location gate for sources that cannot filter server-side. The main
    location_ok() in job_hunter.py still has the final say."""
    return bool(INDIA_RE.search(loc or "")) or "remote" in (loc or "").lower() or "anywhere" in (loc or "").lower()


# Keyword set used by the server-side-search sources (Workday / Oracle / Eightfold / Phenom / Google / Getro).
JS_QUERIES = ("react", "node", "javascript", "typescript", "frontend", "full stack", "software engineer")


# ══════════════════════════════════════════════════════════════════════════════
#  A. ATS PER-COMPANY FETCHERS  (direct apply links)
# ══════════════════════════════════════════════════════════════════════════════

# 1. SmartRecruiters -- public Posting API. country=in filters to India server-side.
def fetch_smartrecruiters(token: str, country: str = "in", with_jd: bool = False) -> list[dict]:
    out, offset = [], 0
    while offset < 1000:
        q = f"limit=100&offset={offset}" + (f"&country={country}" if country else "")
        d = _json(f"https://api.smartrecruiters.com/v1/companies/{urllib.parse.quote(token)}/postings?{q}")
        if not isinstance(d, dict):
            break
        items = d.get("content") or []
        for j in items:
            loc = (j.get("location") or {})
            loc_s = loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("country")) if x)
            if loc.get("remote"):
                loc_s += " / Remote"
            url = f"https://jobs.smartrecruiters.com/{token}/{j.get('id')}"
            desc = ""
            if with_jd:
                dj = _json(f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{j.get('id')}") or {}
                secs = ((dj.get("jobAd") or {}).get("sections") or {})
                desc = html_to_text(" ".join((secs.get(k) or {}).get("text", "") for k in
                                             ("jobDescription", "qualifications", "additionalInformation")))
                url = dj.get("applyUrl") or dj.get("postingUrl") or url
            out.append(_rec((j.get("company") or {}).get("name") or token, j.get("name"), url, loc_s,
                            _d(j.get("releasedDate")), desc, "ats:smartrecruiters"))
        offset += 100
        if offset >= (d.get("totalFound") or 0):
            break
    return out


# 2. Workable (one account) -- widget API, details=true includes full JD.
def fetch_workable(token: str) -> list[dict]:
    d = _json(f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true")
    if not isinstance(d, dict):
        return []
    out = []
    for j in d.get("jobs") or []:
        locs = j.get("locations") or []
        loc = " / ".join(", ".join(x for x in (l.get("city"), l.get("region"), l.get("country")) if x) for l in locs) \
            or ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        if j.get("telecommuting"):
            loc += " / Remote"
        out.append(_rec(d.get("name") or token, j.get("title"), j.get("shortlink") or j.get("url"), loc,
                        _d(j.get("published_on") or j.get("created_at")),
                        html_to_text(j.get("description", "")) + (f"\nExperience: {j['experience']}" if j.get("experience") else ""),
                        "ats:workable"))
    return out


# 3. Recruitee
def fetch_recruitee(token: str) -> list[dict]:
    d = _json(f"https://{token}.recruitee.com/api/offers/")
    out = []
    for j in (d or {}).get("offers") or []:
        if j.get("status") not in (None, "published"):
            continue
        loc = j.get("location") or ", ".join(x for x in (j.get("city"), j.get("country")) if x)
        if j.get("remote"):
            loc += " / Remote"
        out.append(_rec(j.get("company_name") or token, j.get("title"), j.get("careers_url"), loc,
                        _d(j.get("published_at") or j.get("created_at")),
                        html_to_text((j.get("description") or "") + " " + (j.get("requirements") or "")), "ats:recruitee"))
    return out


# 4. Teamtailor -- public RSS at {slug}.teamtailor.com/jobs.rss (custom career domains also serve /jobs.rss)
def fetch_teamtailor(token: str) -> list[dict]:
    host = token if "." in token else f"{token}.teamtailor.com"
    txt = _text(f"https://{host}/jobs.rss")
    if not txt.startswith("<?xml"):
        return []
    out = []
    try:
        root = ET.fromstring(txt)
    except ET.ParseError:
        return []
    company = (root.findtext("channel/title") or token).strip()
    for it in root.iter("item"):
        locs = [e.text or "" for e in it.iter() if e.tag.endswith("}city") or e.tag.endswith("}country")]
        remote = [e.text or "" for e in it.iter() if e.tag.endswith("}remoteStatus")]
        loc = ", ".join(x for x in locs if x)
        if remote and remote[0] and remote[0].lower() not in ("none", "onsite"):
            loc += " / Remote"
        out.append(_rec(company, it.findtext("title"), it.findtext("link"), loc, _d(it.findtext("pubDate")),
                        html_to_text(it.findtext("description") or ""), "ats:teamtailor"))
    return out


# 5. Personio -- public XML feed
def fetch_personio(token: str) -> list[dict]:
    txt = _text(f"https://{token}.jobs.personio.de/xml") or _text(f"https://{token}.jobs.personio.com/xml")
    if "<workzag-jobs" not in txt:
        return []
    try:
        root = ET.fromstring(txt)
    except ET.ParseError:
        return []
    out = []
    for p in root.iter("position"):
        pid = p.findtext("id")
        offices = [p.findtext("office") or ""] + [o.text or "" for o in p.findall("additionalOffices/office")]
        jd = " ".join(html_to_text(v.findtext("value") or "") for v in p.findall("jobDescriptions/jobDescription"))
        yrs = p.findtext("yearsOfExperience") or ""
        out.append(_rec(p.findtext("subcompany") or token, p.findtext("name"),
                        f"https://{token}.jobs.personio.com/job/{pid}", " / ".join(x for x in offices if x),
                        _d(p.findtext("createdAt")), jd + (f"\nExperience: {yrs} years" if yrs else ""), "ats:personio"))
    return out


# 6. Rippling ATS -- list has no date; detail call (only for title-relevant jobs) gives createdOn + JD.
def fetch_rippling(token: str, title_ok=None, with_detail: bool = True) -> list[dict]:
    d = _json(f"https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs")
    out = []
    for j in d if isinstance(d, list) else []:
        title = j.get("name", "")
        if title_ok and not title_ok(title):
            continue
        loc = (j.get("workLocation") or {}).get("label", "")
        posted, desc, company = "", "", token
        if with_detail:
            dj = _json(f"https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs/{j.get('uuid')}") or {}
            posted = _d(dj.get("createdOn"))
            dd = dj.get("description") or {}
            desc = html_to_text(" ".join(v for v in dd.values() if isinstance(v, str)) if isinstance(dd, dict) else str(dd))
            company = dj.get("companyName") or token
            loc = " / ".join(dj.get("workLocations") or []) or loc
        out.append(_rec(company, title, j.get("url"), loc, posted, desc, "ats:rippling"))
    return out


# 7. Gem job board (Greenhouse-shaped public API)
def fetch_gem(token: str) -> list[dict]:
    d = _json(f"https://api.gem.com/job_board/v0/{token}/job_posts/")
    out = []
    for j in d if isinstance(d, list) else []:
        loc = (j.get("location") or {}).get("name", "")
        if j.get("location_type") == "remote" and "remote" not in loc.lower():
            loc += " / Remote"
        out.append(_rec(token, j.get("title"), j.get("absolute_url"), loc,
                        _d(j.get("first_published_at") or j.get("created_at")),
                        j.get("content_plain") or html_to_text(j.get("content", "")), "ats:gem"))
    return out


# 8. Pinpoint -- postings.json (no posted date exposed; deadline_at only)
def fetch_pinpoint(token: str) -> list[dict]:
    d = _json(f"https://{token}.pinpointhq.com/postings.json")
    out = []
    for j in (d or {}).get("data") or []:
        loc = (j.get("location") or {}).get("name", "")
        if (j.get("workplace_type") or "") == "remote":
            loc += " / Remote"
        jd = " ".join(html_to_text(j.get(k) or "") for k in ("description", "key_responsibilities", "skills_knowledge_expertise"))
        out.append(_rec(token, j.get("title"), j.get("url"), loc, "", jd, "ats:pinpoint"))
    return out


# 9. Breezy HR
def fetch_breezy(token: str) -> list[dict]:
    d = _json(f"https://{token}.breezy.hr/json")
    out = []
    for j in d if isinstance(d, list) else []:
        l = j.get("location") or {}
        loc = l.get("name") or ", ".join(x for x in (l.get("city"), (l.get("country") or {}).get("name")) if x)
        if l.get("is_remote"):
            loc += " / Remote"
        out.append(_rec((j.get("company") or {}).get("name") or token, j.get("name"), j.get("url"), loc,
                        _d(j.get("published_date")), "", "ats:breezy"))
    return out


# 10. BambooHR -- list has no date; /detail gives datePosted + JD (call only for relevant titles).
def fetch_bamboohr(token: str, title_ok=None, with_detail: bool = True) -> list[dict]:
    d = _json(f"https://{token}.bamboohr.com/careers/list")
    out = []
    for j in (d or {}).get("result") or []:
        title = j.get("jobOpeningName", "")
        if title_ok and not title_ok(title):
            continue
        l = j.get("location") or {}
        loc = ", ".join(x for x in (l.get("city"), l.get("state")) if x)
        a = j.get("atsLocation") or {}          # where a REMOTE opening is limited to (e.g. Malta; Wooster, Ohio, United States)
        scope = ", ".join(x for x in (a.get("city"), a.get("state") or a.get("province"), a.get("country")) if x)
        if scope and scope.lower() not in loc.lower():
            loc = f"{loc}, {scope}".strip(", ")
        if j.get("isRemote") or str(j.get("locationType")) == "1":
            loc += " / Remote"
        posted, desc = "", ""
        if with_detail:
            dj = ((_json(f"https://{token}.bamboohr.com/careers/{j.get('id')}/detail") or {}).get("result") or {}).get("jobOpening") or {}
            posted = _d(dj.get("datePosted"))
            desc = html_to_text(dj.get("description", ""))
            c = (dj.get("location") or {}).get("addressCountry")
            if c:
                loc = f"{loc}, {c}".strip(", ")
        out.append(_rec(token, title, f"https://{token}.bamboohr.com/careers/{j.get('id')}", loc, posted, desc, "ats:bamboohr"))
    return out


# 11. Freshteam (Freshworks' ATS, popular with Indian SMBs)
def fetch_freshteam(token: str) -> list[dict]:
    d = _json(f"https://{token}.freshteam.com/hire/widgets/jobs.json")
    out = []
    branches = {b.get("id"): b for b in (d or {}).get("branches") or []} if isinstance(d, dict) else {}
    for j in (d or {}).get("jobs") or []:
        if j.get("deleted"):
            continue
        b = branches.get(j.get("branch_id")) or {}
        loc = ", ".join(x for x in (b.get("city"), b.get("country_code") or b.get("country")) if x)
        if j.get("remote"):
            loc += " / Remote"
        out.append(_rec(token, j.get("title"), j.get("url"), loc, _d(j.get("created_at")),
                        html_to_text(j.get("description", "")), "ats:freshteam"))
    return out


# 12. Workday -- same CXS API job_hunter.py already uses; here with a date parser and India text search.
def fetch_workday(token: str, queries=("India",), max_pages: int = 3) -> list[dict]:
    host, tenant, site = token.split("|")
    seen, out = set(), []
    for q in queries:
        for page in range(max_pages):
            d = _json(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                      {"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": q},
                      headers={"Accept": "application/json"})
            posts = (d or {}).get("jobPostings") if isinstance(d, dict) else None
            if not posts:
                break
            for j in posts:
                p = j.get("externalPath")
                if not p or p in seen:
                    continue
                seen.add(p)
                out.append(_rec(tenant.replace("-", " ").title(), j.get("title"), f"https://{host}/{site}{p}",
                                j.get("locationsText", ""), _rel_date(j.get("postedOn", "")), "", "ats:workday"))
            if len(posts) < 20:
                break
            time.sleep(0.3)
    return out


def workday_detail(url: str) -> tuple[str, str]:
    """(posted_date, jd_text) for a Workday posting URL -- startDate is exact."""
    m = re.match(r"https://([^/]+)/([^/]+)(/job/.+)", url)
    if not m:
        return "", ""
    host, site, path = m.groups()
    tenant = host.split(".")[0]
    d = _json(f"https://{host}/wday/cxs/{tenant}/{site}{path}") or {}
    ji = d.get("jobPostingInfo") or {}
    return _d(ji.get("startDate")), html_to_text(ji.get("jobDescription", ""))


ORACLE_NAMES = {"eeho": "Oracle", "jpmc": "JPMorgan Chase", "ibqbjb": "Honeywell"}


# 13. Oracle Cloud HCM (Candidate Experience REST) -- JPMorgan, Oracle, Honeywell, many banks/conglomerates.
def fetch_oracle(token: str, queries=JS_QUERIES, country: str = "IN") -> list[dict]:
    host, site = token.split("|")
    seen, out = set(), []
    for q in queries:
        url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
               f"&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={site},limit=50,"
               f"keyword=%22{urllib.parse.quote(q)}%22,sortBy=POSTING_DATES_DESC")
        d = _json(url)
        items = ((d or {}).get("items") or [{}])[0].get("requisitionList") or []
        for r in items:
            if r["Id"] in seen:
                continue
            locs = [r.get("PrimaryLocation") or ""] + [s.get("Name", "") for s in r.get("secondaryLocations") or []]
            if country and r.get("PrimaryLocationCountry") != country and not any(INDIA_RE.search(l) for l in locs) \
                    and "remote" not in (r.get("WorkplaceType") or "").lower():
                continue
            seen.add(r["Id"])
            out.append(_rec(ORACLE_NAMES.get(host.split(".")[0], host.split(".")[0].title()), r.get("Title"),
                            f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r['Id']}",
                            " / ".join(x for x in locs if x), _d(r.get("PostedDate")),
                            r.get("ShortDescriptionStr") or "", "ats:oracle"))
        time.sleep(0.3)
    return out


def oracle_detail(host: str, site: str, req_id: str) -> str:
    d = _json(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
              f"&finder=ById;Id=%22{req_id}%22,siteNumber={site}") or {}
    r = (d.get("items") or [{}])[0]
    return html_to_text(" ".join(r.get(k) or "" for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")))


# 14. Eightfold PCSX (Microsoft, Qualcomm, Morgan Stanley, PayPal, Deloitte, Accenture, ...)
def fetch_eightfold(token: str, queries=JS_QUERIES, location: str = "India", with_jd: bool = False) -> list[dict]:
    base, domain = token.split("|")
    seen, out = set(), []
    for q in queries:
        for start in (0, 10):
            d = _json(f"{base}/api/pcsx/search?domain={domain}&query={urllib.parse.quote(q)}"
                      f"&location={urllib.parse.quote(location)}&start={start}&sort_by=timestamp")
            pos = ((d or {}).get("data") or {}).get("positions") or []
            for p in pos:
                if p["id"] in seen:
                    continue
                seen.add(p["id"])
                desc = ""
                if with_jd:
                    dj = (_json(f"{base}/api/pcsx/position_details?position_id={p['id']}&domain={domain}&hl=en") or {}).get("data") or {}
                    desc = html_to_text(dj.get("jobDescription", ""))
                loc = " / ".join(p.get("locations") or [])
                if (p.get("workLocationOption") or "") == "remote":
                    loc += " / Remote"
                r = _rec(domain.split(".")[0].title(), p.get("name"), f"{base}{p.get('positionUrl')}", loc,
                         _d(p.get("postedTs") or p.get("creationTs")), desc, "ats:eightfold")
                r["_domain"] = domain
                out.append(r)
            if len(pos) < 10:
                break
        time.sleep(0.3)
    return out


# 15. Phenom career sites -- GET search-results page embeds the first page of results (phApp.ddo).
def fetch_phenom(token: str, queries=("react", "javascript", "full stack", "node", "frontend"), location: str = "India") -> list[dict]:
    base, country, lang = (token.split("|") + ["global", "en"])[:3]
    seen, out = set(), []
    for q in queries:
        for frm in (0, 10):
            txt = _text(f"{base}/{country}/{lang}/search-results?keywords={urllib.parse.quote(q)}"
                        f"&location={urllib.parse.quote(location)}&from={frm}&s=1")
            m = re.search(r"phApp\.ddo\s*=\s*(\{.*?\});\s*phApp", txt, re.S)
            if not m:
                break
            try:
                rs = json.loads(m.group(1)).get("eagerLoadRefineSearch") or {}
            except Exception:
                break
            jobs = (rs.get("data") or {}).get("jobs") or []
            for j in jobs:
                jid = j.get("jobId") or j.get("reqId")
                if jid in seen:
                    continue
                seen.add(jid)
                url = j.get("applyUrl") or f"{base}/{country}/{lang}/job/{jid}"
                out.append(_rec(base.split("//")[1].split(".")[1].title(), j.get("title"), url,
                                j.get("cityStateCountry") or j.get("location", ""), _d(j.get("postedDate") or j.get("dateCreated")),
                                j.get("descriptionTeaser", ""), "ats:phenom"))
            if len(jobs) < 10:
                break
            time.sleep(0.5)
    return out


# 16. SAP SuccessFactors Recruiting Marketing -- public RSS at /sitemal.xml (sic); date from the job page.
JS_TITLE = re.compile(r"react|node|javascript|typescript|front.?end|full.?stack|mern|next\.?js|web dev|software (engineer|developer)|sde|ui (engineer|developer)", re.I)


def fetch_successfactors(token: str, title_ok=JS_TITLE.search, india_only: bool = True, with_date: bool = True,
                         max_date_fetches: int = 25) -> list[dict]:
    """The RSS has no posting date; the job page's itemprop=datePosted does. Page fetches are capped
    (default 25 per board) and only made for title-relevant India/remote rows."""
    host = token.replace("https://", "").strip("/")
    txt = _text(f"https://{host}/sitemal.xml", timeout=60)
    if "<rss" not in txt[:300]:
        return []
    try:
        root = ET.fromstring(txt)
    except ET.ParseError:
        return []
    g = "{http://base.google.com/ns/1.0}"
    out = []
    for it in root.iter("item"):
        title = re.sub(r"\s*\([^)]*\)\s*$", "", it.findtext("title") or "")
        loc = it.findtext(f"{g}location") or ""
        if india_only and not india_or_remote(loc):
            continue
        if title_ok and not title_ok(title):
            continue
        link = it.findtext("link") or ""
        posted = ""
        if with_date and max_date_fetches > 0:
            max_date_fetches -= 1
            page = _text(link, timeout=20)
            m = re.search(r'itemprop="datePosted" content="([^"]+)"', page)
            posted = _d(m.group(1)) if m else ""
        out.append(_rec(it.findtext(f"{g}employer") or host, title, link, loc, posted,
                        html_to_text(_html.unescape(it.findtext("description") or "")), "ats:successfactors"))
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  B. CROSS-COMPANY SEARCH SOURCES (one call -> many companies, links are direct ATS)
# ══════════════════════════════════════════════════════════════════════════════

# 17. Workable job marketplace (jobs.workable.com) -- global search across ~all Workable customers.
#     Its own /view/ pages are a board, so each hit is resolved to apply.workable.com/j/<shortcode>
#     by guessing the account slug from the company name and matching the title in the widget API.
_WK_SLUG_CACHE: dict[str, dict | None] = {}


def _workable_slug_candidates(name: str, website: str = "") -> list[str]:
    n = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    c = [n.replace(" ", "-"), n.replace(" ", ""), re.sub(r"\b(pvt|ltd|private|limited|inc|llp|llc|technologies|solutions)\b", "", n).strip().replace(" ", "-")]
    if website:
        h = (urllib.parse.urlparse(website).hostname or "").replace("www.", "").split(".")[0]
        c.append(h)
    out = []
    for x in c:
        x = re.sub(r"-+", "-", x).strip("-")
        if x and x not in out:
            out.append(x)
    return out


def _workable_account(name: str, website: str = "") -> dict | None:
    for slug in _workable_slug_candidates(name, website):
        if slug in _WK_SLUG_CACHE:
            if _WK_SLUG_CACHE[slug]:
                return _WK_SLUG_CACHE[slug]
            continue
        d = _json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
        if isinstance(d, dict) and d.get("jobs") is not None:
            d["_slug"] = slug
            _WK_SLUG_CACHE[slug] = d
            return d
        _WK_SLUG_CACHE[slug] = None
    return None


def fetch_workable_search(token: str = "react", location: str = "India", pages: int = 3, day_range: int = 40) -> list[dict]:
    """token = search query. Returns only hits resolved to a real apply.workable.com link."""
    out, page_tok = [], ""
    for _ in range(pages):
        q = f"query={urllib.parse.quote(token)}&location={urllib.parse.quote(location)}&day_range={day_range}"
        d = _json(f"https://jobs.workable.com/api/v1/jobs?{q}" + (f"&pageToken={page_tok}" if page_tok else ""))
        if not isinstance(d, dict):
            break
        for j in d.get("jobs") or []:
            comp = j.get("company") or {}
            acct = _workable_account(comp.get("title", ""), comp.get("website", ""))
            if not acct:
                continue
            norm = lambda s: re.sub(r"\W+", "", (s or "").lower())
            hit = next((x for x in acct.get("jobs") or [] if norm(x.get("title")) == norm(j.get("title"))), None)
            if not hit:
                continue
            loc = " / ".join(j.get("locations") or []).replace("TELECOMMUTE", "Remote")
            desc = html_to_text((j.get("description") or "") + " " + (j.get("requirementsSection") or ""))
            out.append(_rec(comp.get("title"), j.get("title"), hit.get("shortlink") or hit.get("url"), loc,
                            _d(j.get("created")), desc, "workable_search"))
        page_tok = d.get("nextPageToken") or ""
        if not page_tok:
            break
        time.sleep(0.5)
    return out


# 18. Getro VC-portfolio networks (Accel, 3one4, General Catalyst, Insight ...) -- India filter server-side.
def getro_collection_id(board_host: str) -> str:
    """Read the network id from a Getro-powered board's __NEXT_DATA__."""
    txt = _text(f"https://{board_host}/jobs")
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', txt, re.S)
    try:
        return str(json.loads(m.group(1))["props"]["pageProps"]["network"]["id"]) if m else ""
    except Exception:
        return ""


def fetch_getro(token: str, queries=("react", "node", "javascript", "typescript", "frontend", "full stack"),
                location: str = "India", pages: int = 3) -> list[dict]:
    cid, host = (token.split("|") + ["jobs.accel.com"])[:2]
    hdr = {"Accept": "application/json", "Origin": f"https://{host}", "Referer": f"https://{host}/"}
    seen, out = set(), []
    for q in queries:
        for page in range(pages):
            d = _json(f"https://api.getro.com/api/v2/collections/{cid}/search/jobs",
                      {"hitsPerPage": 50, "page": page, "filters": {"searchable_locations": [location]}, "query": q}, hdr)
            jobs = ((d or {}).get("results") or {}).get("jobs") or []
            for j in jobs:
                u = j.get("url") or ""
                if not u or u in seen or "linkedin.com" in u:      # LinkedIn-sourced rows are not direct links
                    continue
                seen.add(u)
                loc = " / ".join(j.get("locations") or j.get("searchable_locations") or [])
                if j.get("work_mode") == "remote":
                    loc += " / Remote"
                out.append(_rec((j.get("organization") or {}).get("name"), j.get("title"), u, loc,
                                _d(j.get("created_at")), "", f"getro:{host}"))
            if len(jobs) < 50:
                break
            time.sleep(0.4)
    return out


# 19. Remote Rocketship -- India-remote pages; every row carries the original ATS URL + seniority flags.
RR_SLUGS = ("software-engineer", "frontend-engineer", "backend-engineer", "fullstack-engineer",
            "javascript-developer", "node-js-developer")


def fetch_remoterocketship(token: str = "software-engineer", country: str = "india", pages: int = 3) -> list[dict]:
    out = []
    for page in range(1, pages + 1):
        txt = _text(f"https://www.remoterocketship.com/country/{country}/jobs/{token}/" + (f"?page={page}" if page > 1 else ""))
        m = re.search(r'__NEXT_DATA__" type="application/json">(.*?)</script>', txt, re.S)
        if not m:
            break
        try:
            jobs = json.loads(m.group(1))["props"]["pageProps"].get("initialJobOpenings") or []
        except Exception:
            break
        for j in jobs:
            if j.get("dateDeleted"):
                continue
            lvl = [k[2:] for k in ("isEntryLevel", "isJunior", "isMidLevel", "isSenior", "isLead") if j.get(k)]
            desc = " ".join(x for x in (j.get("twoLineJobDescriptionSummary") or "", "Tech: " + ", ".join(j.get("techStack") or []),
                                        "Level: " + "/".join(lvl)) if x)
            loc = (j.get("location") or "") + (" / Remote" if j.get("locationType") == "remote" else "")
            out.append(_rec((j.get("company") or {}).get("name"), j.get("roleTitle"), j.get("url"), loc,
                            _d(j.get("created_at")), desc, "remoterocketship"))
        if len(jobs) < 20:
            break
        time.sleep(0.5)
    return out


# 20. Google Careers -- server-rendered results (AF_initDataCallback ds:1); direct google.com apply pages.
def fetch_google(token: str = "software engineer", location: str = "India", levels=("EARLY", "MID"), pages: int = 2) -> list[dict]:
    out, seen = [], set()
    for lvl in levels:
        for page in range(1, pages + 1):
            txt = _text("https://www.google.com/about/careers/applications/jobs/results?"
                        + urllib.parse.urlencode({"location": location, "q": token, "target_level": lvl, "page": page}))
            m = re.search(r"AF_initDataCallback\(\{key: 'ds:1', hash: '\d+', data:(.*?), sideChannel: \{\}\}\);", txt, re.S)
            if not m:
                break
            try:
                rows = json.loads(m.group(1))[0] or []
            except Exception:
                break
            for r in rows:
                if r[0] in seen:
                    continue
                seen.add(r[0])
                slug = re.sub(r"[^a-z0-9]+", "-", r[1].lower()).strip("-")
                locs = " / ".join(x[0] for x in (r[9] or []) if x)
                jd = html_to_text(" ".join((x or [None, ""])[1] or "" for x in (r[10], r[3], r[4], r[19]) if isinstance(x, list)))
                out.append(_rec(r[7] or "Google", r[1], f"https://www.google.com/about/careers/applications/jobs/results/{r[0]}-{slug}",
                                locs, _d((r[12] or [0])[0]), jd, "bigco:google"))
            if len(rows) < 20:
                break
            time.sleep(0.5)
    return out




_URL_RULES = [
    ("workday", re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkday(?:jobs|site)\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)"),
     lambda m: f"{m.group(1)}.{m.group(2)}.myworkdayjobs.com|{m.group(1)}|{m.group(3)}"),
    ("oracle", re.compile(r"https?://([a-z0-9.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z]{2}/sites/([^/?#]+)"),
     lambda m: f"{m.group(1)}|{m.group(2)}"),
    ("eightfold", re.compile(r"https?://([a-z0-9-]+)\.eightfold\.ai"), lambda m: f"https://{m.group(1)}.eightfold.ai|{m.group(1)}.com"),
    ("smartrecruiters", re.compile(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"), lambda m: m.group(1)),
    ("workable", re.compile(r"https?://apply\.workable\.com/(?!j/|api/)([a-z0-9_-]+)"), lambda m: m.group(1)),
    ("recruitee", re.compile(r"https?://([a-z0-9-]+)\.recruitee\.com"), lambda m: m.group(1)),
    ("teamtailor", re.compile(r"https?://([a-z0-9-]+)\.teamtailor\.com"), lambda m: m.group(1)),
    ("personio", re.compile(r"https?://([a-z0-9-]+)\.jobs\.personio\.(?:de|com)"), lambda m: m.group(1)),
    ("rippling", re.compile(r"https?://ats\.rippling\.com/([a-z0-9_-]+)"), lambda m: m.group(1)),
    ("gem", re.compile(r"https?://jobs\.gem\.com/([a-z0-9_-]+)"), lambda m: m.group(1)),
    ("pinpoint", re.compile(r"https?://([a-z0-9-]+)\.pinpointhq\.com"), lambda m: m.group(1)),
    ("breezy", re.compile(r"https?://([a-z0-9-]+)\.breezy\.hr"), lambda m: m.group(1)),
    ("bamboohr", re.compile(r"https?://([a-z0-9-]+)\.bamboohr\.com"), lambda m: m.group(1)),
    ("freshteam", re.compile(r"https?://([a-z0-9-]+)\.freshteam\.com"), lambda m: m.group(1)),
    ("darwinbox", re.compile(r"https?://([a-z0-9-]+)\.darwinbox\.(?:in|com)"), lambda m: m.group(1)),
    ("greenhouse", re.compile(r"https?://(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)"), lambda m: m.group(1)),
    ("lever", re.compile(r"https?://jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)"), lambda m: m.group(1)),
    ("ashby", re.compile(r"https?://jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"), lambda m: m.group(1)),
    ("keka", re.compile(r"https?://([a-z0-9-]+)\.keka\.com/careers"), lambda m: m.group(1)),
]


def tokens_from_urls(urls) -> set[tuple[str, str]]:
    """Any ATS URL (from Getro / Remote Rocketship / Serper / LinkedIn resolver) -> (kind, token).
    Run this over every direct link the system sees: it is the cheapest way to grow tokens."""
    out = set()
    for u in urls:
        for kind, rx, fmt in _URL_RULES:
            m = rx.match(u or "")
            if m:
                out.add((kind, fmt(m)))
                break
    return out


# Serper (paid credits, ~2,500 total) -- budgeted query templates, ~10 results each.
SERPER_TOKEN_QUERIES = [
    'site:myworkdayjobs.com "India" (react OR "node.js" OR frontend)',
    'site:oraclecloud.com "CandidateExperience" India software',
    'site:eightfold.ai/careers India software engineer',
    'site:jobs.smartrecruiters.com India (react OR node)',
    'site:apply.workable.com India (react OR "full stack")',
    'site:darwinbox.in/ms/candidate careers',
    'site:freshteam.com/jobs India developer',
    'site:keka.com/careers developer',
]


# ══════════════════════════════════════════════════════════════════════════════
#  SEED TOKENS (each verified live on 2026-11-27)
# ══════════════════════════════════════════════════════════════════════════════
SEEDS = {
    "smartrecruiters": ["BoschGroup", "Freshworks"],
    "workable": ["ai-accountant", "abylle-solutions-pvt-ltd"],
    "recruitee": ["cloudsolutionsinternational", "bunq"],
    "teamtailor": ["bearingpointindia"],
    "personio": ["fieldfisher"],
    "rippling": ["ascentt-india", "cbtsindia"],
    "gem": ["multiplierhq"],
    "pinpoint": ["innovaccer"],
    "breezy": ["iqvia"],
    "bamboohr": ["wirex"],
    "freshteam": ["dojoy"],
    "workday": ["bdx.wd1.myworkdayjobs.com|bdx|EXTERNAL_CAREER_SITE_INDIA",
                "armaninollp.wd1.myworkdayjobs.com|armaninollp|Armanino_India",
                "alliancedata.wd5.myworkdayjobs.com|alliancedata|breadfinancial_india",
                "visa.wd5.myworkdayjobs.com|visa|Visa"],
    "oracle": ["jpmc.fa.oraclecloud.com|CX_1001", "eeho.fa.us2.oraclecloud.com|CX_45001", "ibqbjb.fa.ocs.oraclecloud.com|CX_1"],
    "eightfold": ["https://apply.careers.microsoft.com|microsoft.com", "https://careers.qualcomm.com|qualcomm.com",
                  "https://morganstanley.eightfold.ai|morganstanley.com", "https://paypal.eightfold.ai|paypal.com"],
    "phenom": ["https://jobs-ta.pwc.com|global|en"],
    "successfactors": ["southasiacareers.deloitte.com", "careers.hcltech.com"],
    "getro": ["8672|jobs.accel.com", "51275|jobs.3one4capital.com", "222|jobs.generalcatalyst.com", "246|jobs.insightpartners.com"],
    "remoterocketship": list(RR_SLUGS),
    "workable_search": ["react", "node", "javascript", "full stack", "frontend", "typescript"],
    "google": ["software engineer", "frontend", "full stack"],
}

FETCHERS = {k[6:]: v for k, v in globals().items() if k.startswith("fetch_") and callable(v)}




def detail_jd(kind: str, rec: dict) -> str:
    """Full JD for one row that already passed the title/location prefilter (list APIs without the JD)."""
    url = rec.get("url", "")
    try:
        if kind == "smartrecruiters":
            m = re.search(r"jobs\.smartrecruiters\.com/([^/]+)/(\d+)", url)
            if m:
                dj = _json(f"https://api.smartrecruiters.com/v1/companies/{m.group(1)}/postings/{m.group(2)}") or {}
                secs = ((dj.get("jobAd") or {}).get("sections") or {})
                return html_to_text(" ".join((secs.get(k) or {}).get("text", "") for k in
                                             ("companyDescription", "jobDescription", "qualifications", "additionalInformation")))
        elif kind == "oracle":
            m = re.search(r"https://([^/]+)/hcmUI/CandidateExperience/en/sites/([^/]+)/job/(\d+)", url)
            if m:
                return oracle_detail(*m.groups())
        elif kind == "eightfold":
            m = re.search(r"^(https://[^/]+).*?(\d{6,})", url)
            dom = rec.get("_domain", "")
            if m and dom:
                dj = (_json(f"{m.group(1)}/api/pcsx/position_details?position_id={m.group(2)}&domain={dom}&hl=en") or {}).get("data") or {}
                return html_to_text(dj.get("jobDescription", ""))
    except Exception:
        return ""
    return ""


# kinds polled per company in the fast rotation, and the heavier enterprise kinds (several searches per company)
FAST_KINDS = ("smartrecruiters", "workable", "recruitee", "teamtailor", "personio", "rippling", "gem", "breezy", "freshteam")
ENTERPRISE_KINDS = ("oracle", "eightfold", "phenom", "successfactors")
