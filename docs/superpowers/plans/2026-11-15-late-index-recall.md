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

- [ ] Reproduce the 2026-11-01 publication / 2026-11-07 registration case;
      assert a successful 2026-11-15 watermark yields a 2026-10-16 start.
- [ ] Assert Crossref URL query `filter` contains `from-index-date:2026-10-16`.
- [ ] Run those tests before changing the implementation and capture failures.
- [ ] Subtract `timedelta(days=lookback_days)` from a valid prior watermark;
      keep bootstrap and existing cursor resumption semantics.
- [ ] Replace Crossref's `from-pub-date` query filter with `from-index-date`.
- [ ] Verify repeat merging preserves existing AI decisions and leaves no
      duplicate pending key; run collection and source adapter tests.

## Task 2: Restore independently validated journal RSS

Files: `config/rss_sources.tsv`, `scripts/validate_rss_sources.py`, `README.md`,
`tests/test_acceptance.py`.

- [ ] GET `https://rss.sciencedirect.com/publication/science/09259635` twice
      and verify successful XML with entries, using the project's fetch path.
- [ ] Add the journal's exact URL to active sources if both requests succeed.
- [ ] Remove only this journal from database-only notes and their acceptance
      expectations; change the active-source count from 156 to 157.
- [ ] Document rolling overlap, Crossref index-date behavior and the lack of a
      guarantee that every publisher/index will expose every paper.
- [ ] Run source-list and acceptance tests.

## Task 3: Deploy and recover

- [ ] Run the full suite with TEMP/TMP on the same drive as test outputs.
- [ ] Review the complete diff, then refresh remote main before fast-forward
      deployment; preserve any newer workflow-generated state.
- [ ] Check the latest Crossref cursor before changing its query basis; if
      present, restart that original date at cursor `*` and preserve records.
- [ ] Merge the verified requested DOI and original abstract into latest state
      with `merge_into_state`; publish the raw feed and state atomically.
- [ ] Run the deployed collection, comprehensive digest and NCD workflows.
- [ ] Verify exact DOI presence, AI relevance, public output, NCD decision,
      and Bark acceptance receipt; report counts and any incomplete work.
