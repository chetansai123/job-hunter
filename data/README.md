# data/board_tokens.json.gz

About 98,000 public company job-board identifiers that job_hunter.py polls in rotation. There is one list per job system: Workday, SmartRecruiters, Workable, Greenhouse, Lever, Ashby, Keka, Zoho Recruit, Recruitee, Teamtailor, Personio, Rippling, Gem, Breezy, Freshteam, BambooHR, Oracle HCM, Eightfold, Phenom and SuccessFactors.

The scan imports the file once, controlled by the kv key `token_pack`. Boards that are dead, or that have no India or remote openings, are paused after 3 empty polls and retried once a month.

The lists were built on 2026-11-27 from these sources:

- [Stapply ats-scrapers](https://github.com/kalil0321/ats-scrapers/tree/main/ats-companies), MIT licence.
- [latmay/ats-career-page-urls](https://huggingface.co/datasets/latmay/ats-career-page-urls), CC BY 4.0 (attribution required).
- Company links harvested from Getro VC-portfolio boards and Remote Rocketship.
