# Delayed-indexing recall repair

## Authorized outcome

Recover DOI `10.1016/j.diamond.2026.114379` and prevent the demonstrated gap in
normal collection. The user approved recovery and measures against similar
misses after the read-only investigation identified the cause.

## Evidence

The paper became available online on 2026-11-07. Crossref first registered it
that day and OpenAlex created it on 2026-11-08, but their month-precision
publication metadata resolves to 2026-11-01. The existing publication-date
window had advanced beyond that date. The journal's direct RSS is currently
disabled, leaving database discovery as its only route.

## Design

1. Every new scholarly scan overlaps the previous successful watermark by the
   configured `collection.lookback_days` (30). If there is no watermark, retain
   the existing bootstrap window. Preserve an unfinished scan's original date
   and cursor until all pages complete; never narrow a failed scan's window.
2. Crossref searches use `from-index-date`, so a newly indexed or updated record
   can be discovered even when its publication date precedes the overlap.
   Publication dates in stored records remain publication metadata. Before
   deploying, inspect and reset any legacy Crossref cursor, retaining its
   original start date, because a cursor belongs to its original query.
3. Revalidate Diamond and Related Materials RSS with real GETs and XML parsing;
   restore it only if validation succeeds. Preserve other journals' policies.
4. Recover the requested paper with authoritative metadata and its original
   Elsevier abstract. Merge it into the latest state using ordinary identity
   normalization and queueing; do not manufacture an AI judgment.
5. Use the existing comprehensive digest and NCD workflows to screen and publish
   recovered candidates. Existing accepted papers must not be queued again just
   because an overlapping request returns them. Substantive new abstracts keep
   the existing intentional re-review behavior.

## Verification and limits

Reproduce late-indexed monthly publication metadata in regression tests. Verify
overlapping re-fetches preserve prior decisions and do not duplicate the AI
queue. Test cursor continuation and failures, validate the real journal feed,
run the full suite, then inspect successful deployed collection and the exact
paper's digest/NCD results and delivery receipt. External feeds and indexes can
still omit records; these measures provide independent discovery routes rather
than claiming perfect global coverage.
