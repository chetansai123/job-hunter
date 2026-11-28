# Faster applying: cold-email composer and auto-apply (researched 2026-11-28)

## Where your live jobs are (from jobs.db today)

| Where you apply | Live jobs |
|---|---|
| Workday | 149 |
| Other career pages | 127 |
| Zoho Recruit | 37 |
| Ashby | 30 |
| SmartRecruiters | 26 |
| Keka | 25 |
| Greenhouse | 16 |
| Lever | 12 |
| By email | 6 |
| Workable | 2 |

- Workday is the biggest group, about a third of all jobs.
- Greenhouse, Lever and Ashby fill well automatically, but together they have only 58 jobs.
- **Emails we already have:**
  - 7 jobs have an apply email.
  - 31 posts contain an email.
  - 0 company contacts have an email. They hold LinkedIn links only.

So for cold email you'll usually be pasting the address yourself.

## 1. Cold reach-out button (Gmail-style composer)

**Possible: yes. It needs no paid service.**

- **Where it appears:** a "Cold reach-out" button on every job, post and startup card. It opens a compose panel inside the dashboard.
- **The panel's top row:**
  - Job link: filled automatically.
  - Recipient type: Founder, Recruiter/HR or Referral.
  - First name.
  - Email: filled if we have one, otherwise you paste it.
- **Generate:** writes the subject and body using the exact rules in `~/ClaudeProjects/ColdEmail/CLAUDE.md`. That covers the Oracle intro line, the two feature bullets, the closing ask for each recipient type, the links line and under 150 words.
  - You can edit the text before sending.
  - From is fixed to your Gmail, and Chetan_CV.pdf is attached.
- **Send:** sends the email. The job is then marked "emailed", so you never email the same person twice.

### Who writes the email: two options

| Option | Pros | Cons |
|---|---|---|
| **`claude -p` on your Mac** (Claude Code is installed) | Uses your ColdEmail project's rules and CV file as they are. Researches the company the same way the project does now. No new key. | About 30–60 s per email. Uses your Claude plan's usage. |
| **Gemini (free key)** | About 5 s per email, free. | Weaker writing. Needs a Gemini key in `.env`. Its company research is shallower. |

### How it sends

- **Gmail App Password** (recommended): a 16-character password that Google gives you once 2-Step Verification is on. You paste it into `.env` yourself. It only lets the app send mail; it can't change your account. Emails go out from your real Gmail with the resume attached and links intact, and they show up in your Sent folder.
- **Save as Gmail draft:** the same password, but the email lands in Drafts and you press Send in Gmail. This is safer while you build trust in the writing.
- **On the GitHub Pages site:** a public static page can't safely hold a password. There, the button can only open Gmail's own compose window pre-filled, which means plain text, no attachment and no hyperlinks. The full composer works in the local dashboard (`start.command`).

Gmail allows about 500 emails a day, far more than you'd send. Keep it to real, personalised emails so they don't land in spam.

## 2. Auto-apply: how people actually do it

### No form-free shortcut exists

- Greenhouse, Lever and Ashby do have "submit application" APIs, but each one needs the **company's own private key** ([Greenhouse docs](https://developers.greenhouse.io/job-board.html), [Lever postings API](https://rubydoc.info/gems/lever_postings)).
- So every auto-apply tool works the same way: it opens the real application page in a browser and fills it.

### Three kinds of tools

1. **Autofill extensions** such as Simplify Copilot, SpeedyApply and Jobright, the kind you already use.
   - They fill the current page from your profile, and you press Submit.
   - Good on Greenhouse, Lever and Ashby. Partial on Workday: you still make the account and go page by page ([Simplify help](https://help.simplify.jobs/articles/2415391)).
2. **Scripted browser bots** (Playwright or Selenium, open source). They:
   - fill each page;
   - upload the resume;
   - use AI to answer the extra questions;
   - stop on the Review page so you click Submit.
3. **AI browser agents**, such as [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) or Claude driving Chrome.
   - They work on any site, but slowly (minutes per job) and they cost usage.
   - They also make mistakes on odd forms.

Open-source projects for the second kind:

- [workday-autofill](https://github.com/HarshadLambate/workday-autofill) (MIT): Workday, Lever and Greenhouse. It fills every page, asks you each new question once, and then stops for you to submit.
- [openapply](https://github.com/kadenhuang102007/openapply) and [workday_auto_apply](https://github.com/faisal6795/workday_auto_apply): both stop at Review.
- [apply-automation](https://github.com/B44ken/apply-automation) and [auto-apply](https://github.com/vincent1313-oss/auto-apply): both also create the Workday account. auto-apply reads the verification code from Gmail.

These descriptions come from the projects' own pages; none of them has been tested here.

### Workday, the hard one

- Every company's Workday site is separate, so you need a new account for each company. Logging in on one company's site doesn't carry over to another ([why](https://www.froghire.ai/blog/why-new-workday-account-each-employer)).
- Many companies also send a "verify your email" link before you can apply ([Workday admin docs](https://doc.workday.com/admin-guide/en-us/human-capital-management/recruiting/career-sites/gtv1538650489786.html)).
- **How people cope:**
  - They use the same email and one password for every Workday account, and a tool fills the sign-up form.
  - Where a verification email is required, they click the link, or a bot reads it from Gmail.
  - Then the tool fills the 5–7 pages: personal info, experience (from the resume), questions, voluntary disclosures and review.
- Workday's "Candidate Profile" is due in early 2027 ([Workday blog](https://blog.workday.com/en-us/workday-candidate-profile.html)). It replaces per-company passwords with one email code, which will make this much easier.

### Risks to know

- **Fully unattended submitting goes wrong in quiet ways.** The bot can guess at questions like notice period, salary or years of React. It can apply twice to one company, or tick the wrong voluntary-disclosure box.
  - Most tool authors deliberately stop before Submit for this reason.
  - Mass auto-submitting also gets candidates flagged ([example warning](https://github.com/HarshadLambate/workday-autofill)).
- **LinkedIn Easy Apply bots break LinkedIn's rules** and can get your account restricted ([LinkedIn help](https://www.linkedin.com/help/linkedin/answer/90586)).
  - This doesn't affect us, because we only use direct company links.
- **What I can't do myself:** type your passwords or press Submit for you in a browser.
  - So the auto-apply has to be your own tool running on your Mac. It fills in, and you confirm.
  - You'd create the Workday password and put it in `.env` or Keychain yourself.

## Recommendation

**A. Build the cold reach-out composer first.**
- It's the fastest win, and it is the part where your existing ColdEmail rules already work.
- Writer: `claude -p`. Sending: a Gmail App Password, starting in "save as draft" mode.

**B. Then an "Auto-fill" button in the local dashboard.** It opens a real Chrome window on the job. That window keeps its own saved profile, so Workday logins stay signed in.
- **Greenhouse, Lever, Ashby, SmartRecruiters and Workable:** it fills everything from one `profile.json` plus your resume.
  - Custom questions come from an answer bank. It asks you once per new question, then reuses the answer.
  - It stops at Submit.
- **Workday:** it fills the sign-up with your email and your stored password. You click the verification email the first time. Then it fills each page and stops at Review.
- Expect about 1–2 minutes per job instead of 10.
- Keep SpeedyApply for Keka, Zoho and other career pages.

**Cheaper first step for B:** try `workday-autofill` as it is on 3–4 of our Workday jobs before building our own. If it works, we just add a button that hands it the job link.
