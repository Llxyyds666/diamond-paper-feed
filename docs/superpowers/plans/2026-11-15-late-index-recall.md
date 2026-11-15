# Delayed-indexing recall implementation plan

> **For agentic workers:** Execute the independent collection and RSS tasks in
> parallel, then review together before deploying. Use test-driven development
> for the changed collection behavior.

**Goal:** Recover the requested NCD paper and stop advancing date windows from
silently excluding late-indexed papers.

**Architecture:** Reuse the current collectors, state format, identity merging,
publication pipeline and NCD outbox. Add rolling overlap and Crossref index-date
queries, restore a validated direct journal feed, then merge the authoritative
recovery record and run existing publication workflows.

**Tech Stack:** Python 3.11+, pytest, feedparser, urllib/curl-cffi, GitHub Actions.

## Global constraints

- `collection.lookback_days` remains 30; `ai.daily_candidates` remains 100.
- No new paid service, credential, or AI model.
- No credential in a source file, report, committed output, or logged URL.
- Active cursor scans keep their original `from_date` until completion.
- Re-fetching unchanged accepted records must not enqueue them for AI again.
- Source lists and README database-only coverage must agree.

## Task 1: Repair collection discovery

Files: `src/diamond_feed/collect.py`, `src/diamond_feed/sources/crossref.py`,
`tests/test_state_collect_render.py`, `tests/test_academic_sources.py`.

- [x] Reproduce the 2026-11-01 publication / 2026-11-07 registration case;
      assert a successful 2026-11-15 watermark yields a 2026-10-16 start.
- [x] Assert Crossref URL query `filter` contains `from-index-date:2026-10-16`.
- [x] Run those tests before changing the implementation and capture failures.
- [x] Subtract `timedelta(days=lookback_days)` from a valid prior watermark;
      keep bootstrap and existing cursor resumption semantics.
- [x] Replace Crossref's `from-pub-date` query filter with `from-index-date`.
- [x] Verify repeat merging preserves existing AI decisions and leaves no
      duplicate pending key; run collection and source adapter tests.

## Task 2: Restore independently validated journal RSS

Files: `config/rss_sources.tsv`, `scripts/validate_rss_sources.py`, `README.md`,
`tests/test_acceptance.py`.

- [x] GET `https://rss.sciencedirect.com/publication/science/09259635` twice
      and verify successful XML with entries, using the project's fetch path.
- [x] Add the journal's exact URL to active sources if both requests succeed.
- [x] Remove only this journal from database-only notes and their acceptance
      expectations; change the active-source count from 156 to 157.
- [x] Document rolling overlap, Crossref index-date behavior and the lack of a
      guarantee that every publisher/index will expose every paper.
- [x] Run source-list and acceptance tests.

## Task 3: Deploy and recover

- [x] Run the full suite with TEMP/TMP on the same drive as test outputs.
- [x] Review the complete diff, then refresh remote main before fast-forward
      deployment; preserve any newer workflow-generated state.
- [x] Check the latest Crossref cursor before changing its query basis; if
      present, restart that original date at cursor `*` and preserve records.
- [x] Merge the verified requested DOI and original abstract into latest state
      with `merge_into_state`; publish the raw feed and state atomically.
- [x] Run the deployed collection, comprehensive digest and NCD workflows.
- [x] Verify exact DOI presence, AI relevance, public output, NCD decision,
      and Bark acceptance receipt; report counts and any incomplete work.

## Verified deployment results

- Clean baseline: 519 passed. New behavior first reproduced as 10 failures,
  then the complete repaired suite passed 528 tests locally and on Actions.
- Implementation commit: `b96e7c8`; verified paper recovery: `d68e143`.
- Summary run `43694405625`: processed 1, selected 1, requests 2; target DOI
  occurs exactly once in the public comprehensive feed with its online date.
- NCD run `43694491919`: processed 1, selected 1, requests 1; Bark accepted
  the target DOI on the first attempt and persisted `sent_at`.
- Collection run `43694571844`: success; DRM fetched successfully, active
  RSS sources 157. First overlap batch leaves 1,390 pending candidates and
  preserves Crossref/OpenAlex cursors starting at 2026-10-16. This is a queued
  historical recheck, not 1,390 AI-selected papers or 1,390 new AI calls.
- No legacy Crossref cursor existed at migration. The recovered record keeps
  its 1,756-character trimmed original abstract and remains AI-accepted after
  the next collection; the recovered DOI itself is no longer pending.
- External index updates can move records across pages. Overlap and direct
  publisher RSS reduce that risk; the implementation does not claim complete
  historical/global discovery. Remaining backfill continues on the existing
  six-hour collection schedule and normal bounded digest schedule.
