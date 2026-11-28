# Hiring posts: free sources research (2026-11-27)

Four agents ran in parallel, and I re-tested the top two finds myself. All tests ran from this Mac on a home connection. Behaviour from GitHub Actions is still unverified until a test run.

The detailed reports, with exact URLs, sample counts and test scripts, are in the scratchpad folder `posts_research/`: `audit.md`, `linkedin.md`, `x_reddit_other.md` and `tools_mcp.md`.

## Why the Posts count doesn't move

- The last 12 cloud runs added **0 posts**. Light runs fetch about 350 posts each (HN 237, Reddit about 100, hiring sheet 12), and every one fails vetting.
- The 65 posts you saw earlier were wiped by a one-time cleanup on Nov 24. Most had come from Apify, and nothing free replaced them. 20 are live now.
- Two collectors never return anything in the cloud, because both depend on Firecrawl:
  - `collect_linkedin_posts` runs on every 30-minute scan.
  - `collect_tal_club` returns 0. tal.club serves plain HTML and could be read directly.
- LinkedIn post search via Serper runs only in full scans: 6 queries, with the same 6 on every run that day.
- The keyless fallback (Brave, Bing, DuckDuckGo) is dead. All three block scripts now.
- **Over-rejection.** All 9 genuine-hirer test posts were rejected:
  - "I'm looking for a React developer" is read as a job seeker.
  - "My team is hiring, DM for referral" is rejected outright.
  - "Freshers also welcome" is rejected, against CLAUDE.md rule 4.
- **Leaks the other way.** LinkedIn profile pages return HTTP 999 (a login wall), so the headline check is skipped and posts pass by default. A US-only post and a Nike repost got in that way.
- Rule 8b says `engine/authors.py` checks each author's posting history. That file doesn't exist; the only check is "author already has 3 posts".

## New sources, ranked (free, no login, no Apify, no Serper)

| # | Source | What it gives | Verified | Volume | Risk |
|---|---|---|---|---|---|
| 1 | **Google News RSS** with `site:linkedin.com/posts hiring react when:7d` | Real LinkedIn hiring posts with true dates. Up to 100 per query; `when:1d`, `OR` and `-word` all work. | Yes (re-tested: 100 posts dated Nov 21–27) | About 200–260 LinkedIn hiring posts a day, about 90 a day naming the JS stack | Links are Google IDs needing a decode step, rate-limited after about 70 fast decodes. Filter on the title first and decode slowly. |
| 2 | **Public LinkedIn post page** | Full text, date, author and follower count | Yes | — | Already used; works from datacenter IPs |
| 3 | **Only Hiring** (`onlyhiring…workers.dev/api/jobs`) | 1,337 Reddit `[Hiring]` posts, AI-filtered, each linking to the original | Yes (newest today) | About 95 mention React, Node or TS | One person's hobby project; could vanish |
| 4 | **Reddit via arctic-shift** (already used) | About 2 minutes behind live | Yes | About 4–7 genuine India/remote JS posts a week | Only the latest 100 posts per subreddit are read, so a failed run loses posts. Add r/techjobs, r/IndianDevelopers, r/WebDeveloperJobs, r/gurgaon. |
| 5 | **X via Nitter** (`nohup.cz` search, plus profile RSS on 2 instances) | Tweets, plus the author's history for the reposter check | Yes, slowly | About 1 a week | Volunteer-run instances; may block GitHub runners |
| 6 | **Hasjob** (`hasjob.co/feed`) | Indian startups posting their own jobs | Yes | Low | — |
| 7 | **outx.ai** public pages | Copies of LinkedIn posts | Yes | About 140 posts, many not hiring | Third party |

### Free with a one-time signup (no card; quota resets monthly)

These replace Serper sustainably. The keys go in as GitHub secrets, and the app rotates between them.

| API | Free each month | Notes |
|---|---|---|
| Exa | $10 credit, about 2,500 searches | — |
| Tavily | 1,000 searches | Code already supports it |
| Linkup | About 4,000 searches | Needs a work email |
| SerpApi | 250 searches | — |

Exa and Tavily together come to about 3,500 searches a month, roughly 110 a day.

### Checked and ruled out

- **MCP servers and plugins:** every LinkedIn or X MCP found needs a login cookie, Apify, or a paid proxy. MCPs also run inside an AI chat, not in a cron job. None is usable here.
- **RSSHub:** its X routes need a logged-in cookie, and its LinkedIn routes are weak.
- **SearXNG:** no public instance returned JSON. Self-hosting only relays the same search engines, which block it.
- **Search engines scraped directly:** Bing ignores `site:`. DuckDuckGo, Mojeek, Startpage and Yandex show captchas. Brave returns 429. Google needs JavaScript.
- **Google's search API:** closed to new users; shuts down on Jan 1, 2027. **Brave's API** needs a card. **The X API** has no free tier.
- **Reddit's own endpoints:** `.json` and RSS are blocked, and RSS was switched off on Nov 13. PullPush refuses automated use.
- **Other X routes:** xcancel is suspended. twscrape and twikit need X accounts. snscrape is dead.
- **Bluesky and Mastodon:** work, but are mostly bots, with about 0 India posts.
- **Telegram channels:** all reposters, which rule 8b forbids.
- **Threads, Peerlist, Indie Hackers, Product Hunt:** blocked or empty.
- **Common Crawl and the Wayback Machine:** no recent LinkedIn posts.
- **Watching recruiters' profiles:** 11 of 12 profiles hit LinkedIn's login wall, so a profile watchlist doesn't work.

## Recommended plan (your call)

1. **Google News RSS as the LinkedIn feed, on every 30-minute scan.**
   - Rotate about 15 queries (stack words × "hiring"/"we are hiring"/"DM me"/"referral" × India/remote) with `when:1d`.
   - Filter on the title first, then decode and read only the posts that survive.
   - Run one test from GitHub Actions first, to confirm Google doesn't block runner IPs.
2. **Fix the vetting:**
   - Allow "my team is hiring / DM for referral" and "I'm looking for a React dev".
   - Accept "freshers also welcome" when the post states a real minimum.
   - When the author's headline can't be read, reject unless the text has first-person hirer wording.
   - Run the staffing check on every source.
3. **Implement rule 8b with the data we can actually get.**
   - An author or company that appears in many different posts across the Google News results is a job feed, so reject it.
   - On X, check the author's last 20 tweets via Nitter RSS.
4. **Sources that are cheap to fix:**
   - Read tal.club directly.
   - Remove the dead Firecrawl LinkedIn call.
   - Page through Reddit beyond the latest 100 posts, retry arctic-shift errors, and add 4 subreddits.
   - Add Only Hiring as a backup feed for Reddit.
5. **Optional:** sign up for Exa and Tavily (free, no card) and add the keys as GitHub secrets. The app then rotates Serper, Exa and Tavily so none runs out.
6. **Skip:** MCPs, RSSHub, SearXNG, Bluesky, Mastodon and Telegram.

**Honest yield estimate:** Google News supplies about 90 JS-stack LinkedIn posts a day. After the India/remote, 1–3 years, genuine-hirer and no-repost filters, I'd guess **5–15 new posts a day**. This is an estimate; the test run will show the real number. Reddit and X add a few a week.

## Firecrawl in the cloud? (researched 2026-11-28)

**What you run locally:** `firecrawl-local` 0.1.0, installed from crates.io. It scrapes pages with your local Chrome (tested: tal.club, jsgurujobs). It has no search of its own; search is passed to Firecrawl cloud and returns `CLOUD_ONLY` without a key. The local log shows the LinkedIn post search has always returned 0 because of that.

**Three options:**

**Firecrawl cloud, free plan.** 1,000 credits a month, resets monthly, no card. Each search costs 2 credits per 10 results, plus 1 credit for every result page it reads. The limit is 10 calls a minute.
- With today's code, one scan uses about 234 credits: 13 job searches × (2 + 8) and 13 LinkedIn post searches × (2 + 6). Contact lookups come on top.
- That empties the free month in about 4 scans, which is about 2 hours of the 30-minute schedule.
- Firecrawl doesn't read LinkedIn ("Firecrawl doesn't scrape LinkedIn"), so the post pages would still be read the way the app reads them now.
- Used carefully (search only, no page reads) it is about 16 searches a day: the same kind of results as Google News RSS, which is free and unlimited.

**Firecrawl cloud, Hobby plan.** $19 a month for 5,000 credits. That still only lasts about a day of scans.

**Self-hosting the full Firecrawl in GitHub Actions.** Its search sends queries to SearXNG or DuckDuckGo (from Firecrawl's source code, `apps/api/src/search/providers.ts`). Both are blocked for scripts, as our tests showed. So it costs nothing but would find nothing.

**Running `firecrawl-local` in GitHub Actions** (`cargo install firecrawl-local`; the runner already has Chrome and Rust) is free. It only adds page reading, which helps the sources that need JavaScript: jsgurujobs, matcha.fm, joblelo and some career pages. tal.club doesn't need it, because it serves plain HTML.
