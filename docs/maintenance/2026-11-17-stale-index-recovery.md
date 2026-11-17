# Historical reindexing regression recovery

## Cause and observed impact

The November 15 late-indexing repair changed Crossref to `from-index-date`
without retaining a publication-date constraint. Historical records updated
recently were admitted as new work. Collection, the oldest-first summary queue,
abstract enrichment, and NCD notifications had no independent age guards.

Compared with pre-regression recovery commit `d68e143`, snapshot `2f4c91d`
contained 4,916 newly introduced papers published before October 16, 2026;
all were from 2020 or earlier. Of these, 4,716 were pending, 144 had been accepted
by the comprehensive AI feed, and 56 rejected. NCD had sent 228 notifications
for these accidental historical imports; none remained unsent at audit time.

The November 16 and 17 comprehensive summaries each processed 100 historical
papers. Their combined usage was 24 requests and 104,862 tokens. This excludes
the separate NCD workflow. Real usage must remain in the audit ledger.

## Runtime repair

- Crossref combines indexed-date and publication-date lower bounds, frozen
  throughout a cursor scan.
- All source imports require a reliable publication date within the configured
  current UTC lookback. The cutoff date itself is included. Future issue dates
  remain eligible, as specified by the existing online-first policy.
- Missing RSS dates are not treated as evidence of recent publication. A later
  reliable adapter date replaces the legacy missing-date fallback.
- Summary pruning happens before budget selection and again after enrichment.
  Enrichment sees recent records only. Queue cleanup persists even if the daily
  limit is already exhausted or no eligible candidates remain.
- NCD checks publication eligibility before AI screening and before delivery.
  Stale unsent messages are removed; accepted receipts and review history stay.

## Reversible one-off recovery

The maintenance command `python -m scripts.repair_stale_index_imports` requires
an immutable baseline commit, publication cutoff and archive destination. It
defaults to a dry run; `--apply` archives the complete removed paper records and
their pending keys while regenerating all feeds and the HTML view transactionally.
It refuses to overwrite an existing archive.

The November 17 recovery uses baseline `d68e143`, cutoff `2026-10-16`, and archive
`archives/2026-11-17-stale-index-imports.json`. Stable identity aliases protect
all pre-existing history, including DOI upgrades. The recovered November paper
`10.1016/j.diamond.2026.114379` remains present. Only the obsolete Crossref cursor
is reset because its query changed; OpenAlex progress is preserved.

Before a new collection, the recovered state contains 2,117 papers and 103
eligible pending papers at the November 17 UTC cutoff. Ten additional pending
records dated October 16/17 and 104 records with unreliable RSS fallback dates
are removed from the daily work queue, not history. Later verified recent dates
allow previously undated, unprocessed records to re-enter the queue.
The archive, original Git snapshot, actual AI usage and Bark receipts provide
recovery and audit evidence. Notifications already delivered to phones cannot
be recalled. Normal cumulative history does not have rolling 30-day retention.
