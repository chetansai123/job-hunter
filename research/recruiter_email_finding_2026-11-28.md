# Finding the recruiter / founder and a deliverable email (researched 2026-11-28)

All of the following was tested live on 2026-11-28 unless marked "from vendor/third-party pages".
The two worked examples are BlackRock (Software Engineer, Python/SQL/React, Mumbai) and NTT DATA (Full Stack Developer, GenAI, Bengaluru).

## 1. Who to contact

| Method | Result | Verdict |
|---|---|---|
| LinkedIn's public job page, "job poster" box | Shown on only 2 of 38 sampled React/Node jobs in India (Codewalla, plus a staffing firm). Not shown for BlackRock. LinkedIn rate-limited 22 of 60 requests. Most of our jobs come from company ATS pages, which never name the recruiter. | Rare. Use it when present. |
| Google search through Serper: `site:in.linkedin.com/in ("talent acquisition" OR recruiter) "<company>" <city>` | **BlackRock Mumbai:** 10 talent-acquisition people, including a Technical Recruiter, TA Partners and the Head of India TA. **NTT DATA Bengaluru:** 10, including TA Specialists and the Director of TA. One search per company. | **Works.** It finds the TA team in that city, not the exact recruiter for one role. |
| LinkedIn people-search link with filters | The company ID comes from the public company page (BlackRock = 4764, NTT DATA = 18535). India's location ID is 102713980. Both were checked on LinkedIn's public job search: they return only BlackRock jobs, and only Indian locations. | **Works**, free and unlimited. Opens in your own logged-in browser. |
| Knowing the exact recruiter for a specific role | Not available publicly for ATS jobs. | Not possible. |

Notes:
- Serper now returns most LinkedIn results as Google redirect links that need JavaScript. A script can't turn them into profile URLs, but they open fine in a browser.
- The result title gives the name and job title.
- Serper's free 2,500 queries are a one-time grant, not monthly (third-party pages). The key in `contact-finder/.env` has **354 left**.
- "Founder" makes no sense for BlackRock or NTT DATA. For big companies the right people are the in-house recruiter or TA team and the engineering manager. Founders apply to startups only.

## 2. Getting an email

| Option | Tested / found | Automatable for free? |
|---|---|---|
| ContactOut (your extension) | Free plan is about 5 emails a day, extension only. The API needs a paid Team/API plan (third-party pages; no official page found). | No. Stays manual. |
| Hunter.io | Free plan: 50 credits a month, shared by Email Finder and Verifier (hunter.io/pricing). Your key in `contact-finder/.env` is **dead** ("No user found for the API key"). | Yes, about 25–50 lookups a month, after you create a free account. |
| Snov.io | Your key in `reachOut/.env` works, but the free plan has **no API access** (Snov's own error: "FreePlan … upgrade your plan"). | No. |
| Apollo | Free plan about 75 email credits a month. Whether the free plan includes the API is contradictory on Apollo's own pages (third-party). No working key found. | Unclear. |

Company-domain email first, then personal:
- Hunter only returns company-domain emails.
- Personal emails come only from ContactOut-type tools, which can't be automated for free.

## 3. Is it deliverable?

| Method | Result |
|---|---|
| site24x7 Email Validator | The page has a captcha. Neither the app nor I may solve captchas, so it stays a manual step. |
| Checking the mailbox directly with the mail server (port 25) from your Mac | **blackrock.com and nttdata.com both run Proofpoint, which answered "554 Blocked" to your home IP.** Gmail answered normally. So a do-it-yourself check fails exactly for big-company domains. |
| Same check from GitHub's servers | Cloud providers generally block outbound port 25 (Microsoft's Azure docs). Not tested on a GitHub runner. |
| Hunter Verifier | Included in the 50 monthly credits. For Proofpoint- or Microsoft 365-protected domains, verifiers often return "accept-all / unknown" instead of yes or no (general behaviour, not tested on these two domains). |

## Bottom line

- **Possible and free:** one-click LinkedIn searches already filtered to the company and India.
- **Possible, small quota:** automatic named recruiters per company through Serper (about 350 companies on the current key).
- **Possible, very small quota:** company emails plus a verification status through Hunter's free 50 credits a month (new account needed).
- **Not possible for free:** automating ContactOut or site24x7 (captcha), guaranteed deliverability for big-company domains, and knowing the exact recruiter of each role.
