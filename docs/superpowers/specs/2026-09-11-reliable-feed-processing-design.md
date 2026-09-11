# Reliable Feed Processing Design

## Goal

Make the daily diamond-paper pipeline process a larger backlog predictably and recover from transient HTTP failures without changing the purpose or identity of either published feed.

## Confirmed product behavior

- A normal daily summary run processes at most 100 pending candidate papers per UTC day instead of 40.
- There is no global daily cap on DeepSeek request attempts. The pipeline processes the selected candidate allowance even when more than six model calls are required.
- Every application-level external HTTP integration uses the same bounded transient-failure policy: at most three attempts, with one-second and two-second waits before attempts two and three.
- Permanent client failures are not retried. These include authentication, insufficient balance, missing resources, and invalid request parameters.
- Retryable failures include connection failures, timeouts, HTTP 408, 425, 429, and 5xx responses. A malformed DeepSeek or Bark response may be retried because a later model/service response can be valid.
- Retries are bounded per operation. Removing the daily request cap does not permit an infinite loop or a workflow that waits forever.
- DeepSeek token usage and actual attempt counts remain recorded in `ai_usage.json`.

## Throughput and cost behavior

`daily_candidates` becomes 100. The current historical backlog is roughly 1,082 pending candidates, so the first several daily runs can use about ten screening batches plus the optional recommendation and overview. Once the initial 30-day backlog is drained, recent collection history indicates roughly 5–20 new broad candidates per day, normally one or two screening batches.

The candidate limit remains a same-day processing limit: rerunning the summary workflow on the same UTC day processes only the unused part of that day's 100-paper allowance. Request attempts are counted for reporting but never used to stop the run.

## Retry architecture

A small shared retry-policy module owns retryable status classification, the three-attempt loop, and injectable waiting for deterministic tests. Integrations retain their protocol-specific validation and secret handling.

- Collection RSS, Crossref, OpenAlex, and arXiv requests continue through `diamond_feed.http`, migrated to the shared policy without changing hard/soft failure classification.
- Semantic Scholar and OpenAIRE abstract enrichment retry each provider request independently. A provider that still fails after three attempts falls through to the next provider without aborting publication.
- DeepSeek retries transient transport failures and invalid model JSON up to three attempts. Screening batches remain atomic: an invalid final response leaves that batch pending for a future run.
- The daily recommendation is generated before the optional overview. This prioritizes the user-facing recommendation if a run approaches its workflow timeout, while the overview retains its deterministic fallback.
- Each Bark message retries independently up to three attempts. Failure remains nonfatal and never rolls back already-published feeds.
- Evaluation-only HTTP downloads use the same retry policy where applicable.

No secret, response body, or credential-bearing URL is written to logs. Errors expose only integration name, sanitized exception class, HTTP status when known, and attempt count.

## Zotero diagnosis

The published `device_focus_feed.xml` was healthy: GitHub Pages served HTTP 200 with `application/xml`, and the feed had grown from 18 to 23 entries. The local Zotero feed database still contained 18 entries and showed no refresh check after the previous day, despite a 60-minute configured interval. Restarting Zotero immediately refreshed the existing subscription to 23 entries.

Therefore the incident was a stalled Zotero client refresh scheduler, not a feed identity or XML problem. The existing feed URL and GUID values remain unchanged. No server-side GUID migration is needed.

## Data flow

1. Collection fetches all configured sources with bounded retries and appends new deduplicated candidates.
2. The daily summary selects up to the remaining portion of the 100-paper daily allowance.
3. Missing abstracts are enriched with provider-specific retries.
4. DeepSeek screens all selected batches without a global request quota.
5. If device-focus papers were selected, DeepSeek chooses the daily recommendation before the optional overview.
6. Feed/state/usage outputs are committed atomically and pushed.
7. Bark sends the statistics and recommendation notifications with bounded retries.
8. Zotero continues using the existing device-feed identity; a stalled local refresh is recovered by restarting Zotero and refreshing the subscription.

## Failure handling

- Exhausting one operation's three attempts records a sanitized failure and continues only where the existing pipeline supports a safe fallback.
- A failed collection source preserves its watermark or continuation so a later run can resume it.
- A failed DeepSeek screening batch remains pending and prevents a misleading partial daily notification.
- Failed abstract enrichment leaves the original metadata intact and allows title-only screening.
- Failed recommendation produces the existing failure notification only after all three attempts fail.
- Failed Bark delivery does not change publication state.

## Verification

- Unit tests prove retryable errors retry exactly three times and permanent errors run once for each integration.
- Tests use injected wait functions and perform no real sleeping.
- Summary tests prove 100 candidates can be processed, same-day reruns respect the remaining candidate allowance, request attempts are not capped, recommendation precedes overview, and retry counts are reported.
- RSS tests continue proving that all published XML feeds parse and preserve their existing stable GUID behavior.
- Acceptance tests validate the configured 100-paper allowance and remove assertions tied to the former six-request ceiling.
- The complete test suite, compile check, XML parse check, and `git diff --check` must pass before deployment.
- After deployment, GitHub Pages must serve the device feed as `application/xml`; a GitHub Actions run must process the expected candidate count without exposing secrets.

## Out of scope

- The 40-paper isolated evaluation workflow remains unchanged.
- The source list and three device-focus definitions do not change.
- No new paid metadata provider is introduced.
- Zotero client scheduling is not modified by this repository; the confirmed local-client stall was resolved by restarting Zotero.
