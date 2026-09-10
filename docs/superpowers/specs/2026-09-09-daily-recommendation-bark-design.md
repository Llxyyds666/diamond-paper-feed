# Daily Recommendation and Bark Notification Design

## Goal

Extend the daily DeepSeek summary workflow with one dedicated recommendation request and two Bark notifications:

1. a daily screening summary containing total candidates, completed papers, broad accepted papers, and newly accepted device-focus papers;
2. one recommended device-focus paper with its focus direction, a concise Chinese reason, and a tap-through paper URL.

The recommendation targets a new graduate student following three advisor-defined directions. It should favor topic relevance, novelty, methodological credibility, and learning value rather than model confidence alone.

## Scope

The recommendation pool contains only papers newly accepted during the current production summary run and carrying at least one controlled focus label:

- `diamond-power-rf-detectors`
- `diamond-thermal-management`
- `device-grade-single-crystal`

Historical papers are not reconsidered. Broad-only diamond papers cannot be recommended. A multi-label paper displays every matching direction in Chinese. If the pool is empty, the second Bark notification says `今日无器件方向推荐` and no recommendation model request is made.

Evaluation and offline-promotion workflows never send notifications and never make recommendation requests.

## Original-Abstract Enrichment

Production summarization enriches missing original abstracts before any DeepSeek screening. An abstract is considered missing when it is blank, duplicates the title after normalization, or consists only of recognized publisher boilerplate. Existing substantive abstracts are never replaced.

The enrichment cascade is:

1. When `SEMANTIC_SCHOLAR_API_KEY` is configured, send one authenticated Semantic Scholar batch request for at most 500 DOI-bearing records with missing abstracts. Current pending candidates have priority, followed by the most recently published unresolved records. Request only `title`, `abstract`, and `externalIds`. This daily batch also revisits older records whose publisher metadata may have arrived late.
2. For current daily candidates still missing an abstract, query OpenAIRE V3 by DOI in groups of at most five and read the aggregated `descriptions` field. OpenAIRE may recover an abstract from Crossref, DataCite, arXiv, or an institutional repository even when the collected record did not contain one.
3. If both sources miss or fail, retain the original record unchanged and allow title-based screening rather than blocking the daily feed.

An enrichment is accepted only when the returned DOI exactly matches the normalized requested DOI and the returned title is compatible with the stored title. Title compatibility means that the NFKC/case-folded alphanumeric forms are equal, one contains the other, or their token-set Jaccard similarity is at least 0.8. The normalized abstract must be nonempty, differ from the title, contain something other than recognized no-abstract/publisher boilerplate, and contain no more than 30,000 Unicode characters. Oversized values are rejected rather than truncated. Accepted text is stored in full, its provider is appended to the record's `sources`, and it becomes part of the same atomic state publication as the AI decisions.

If a previously processed record had no substantive abstract and is later enriched, add its canonical key back to `pending_ai` so DeepSeek can reconsider it from the better evidence. This reprocessing consumes the ordinary 40-candidate/six-request daily limits; it does not bypass them. Enrichment HTTP requests are metadata requests and do not count as DeepSeek requests or tokens.

Semantic Scholar and OpenAIRE failures are isolated and nonfatal. Use one bounded attempt per provider call, sanitize logged errors, never log API keys or response bodies, and continue through the cascade. Evaluation, smoke-test, and offline-promotion paths make no enrichment calls.

## DeepSeek Request Budget

Increase the production daily request ceiling from five to six:

1. up to four screening requests of at most ten unique candidates each;
2. up to one comprehensive HTML-overview request;
3. up to one dedicated recommendation request.

Failed attempts continue to consume the shared request budget. If earlier work exhausts the budget, publication continues without a recommendation and the second notification reports that the recommendation could not be generated. The recommendation request is enabled only when `BARK_ENABLED` is true and the current run has at least one device-focus candidate, avoiding cost when no notification can be delivered.

The sixth request receives, for every recommendation candidate:

- stable identity key;
- title;
- journal;
- publication date;
- controlled focus labels;
- the complete original abstract without truncation;
- the existing Chinese summary as fallback context.

If the source has no original abstract, the request explicitly marks it missing and supplies the Chinese summary. The response must contain exactly one candidate key and a nonempty Chinese recommendation reason of at most 60 Chinese characters. Unknown keys, broad-only keys, duplicate fields, blank reasons, or overlong reasons are rejected before notification.

`ai_usage.json` records the sixth request and all returned token usage through the existing accounting path. Configuration adds a bounded recommendation output limit while retaining the fixed model `deepseek-v4-flash-vision-exp`.

## Bark Integration

Use the official Bark JSON endpoint:

```text
POST https://api.day.app/push
```

The JSON body contains `device_key`, `title`, `body`, `group`, and optionally `url`. The device key is read only from the environment variable `BARK_TOKEN`, injected by the post-publication notification step from the GitHub Actions secret of the same name. It is never placed in a URL, repository file, exception message, command output, or log.

Both messages use the group `diamond-paper-feed`:

### Notification 1: Daily statistics

Title: `金刚石文献日报`

Body:

```text
今日候选：{candidates} 篇
完成筛选：{processed} 篇
综合入选：{selected} 篇
器件方向：{focus_selected} 篇
```

The notification opens `ai_summary.html`.

### Notification 2: Daily recommendation

Title: `今日论文推荐`

For a valid recommendation:

```text
{paper title}
方向：{one or more Chinese focus names}
推荐理由：{reason}
```

Tapping it opens the paper URL.

For an empty pool:

```text
今日无器件方向推荐
```

For a nonempty pool whose recommendation request fails validation or transport:

```text
今日推荐生成失败，器件方向 RSS 已正常更新
```

This distinction prevents a technical failure from being presented as a scientific result.

## Data Flow and Transaction Boundary

1. The production summary command enriches missing abstracts through Semantic Scholar and OpenAIRE.
2. The existing screening phase classifies and summarizes daily candidates.
3. The existing overview request runs when its current conditions are met.
4. Newly accepted focus-labelled records form the recommendation pool.
5. When the workflow indicates that Bark is configured and the pool is nonempty, the sixth request selects one paper.
6. RSS, HTML, usage, focus RSS, and state are staged and committed atomically as before. The summary command also writes an ephemeral notification plan beneath `RUNNER_TEMP`; that plan is never committed.
7. GitHub Actions commits and pushes the published outputs.
8. Only after the push succeeds does a separate notification command read the ephemeral plan and attempt the two Bark notifications with `BARK_TOKEN`.

The summary step receives only a boolean `BARK_ENABLED` value derived from whether the secret exists, not the token itself. `BARK_TOKEN` is exposed only to the final notification step. Failure to create the ephemeral plan is nonfatal and skips notification. No notification is sent when the run processes zero new papers, preventing duplicates on same-day no-op reruns. A summary, local publication, commit, rebase, or push failure sends nothing. Notification state is not written into `state.json`.

## Failure Handling

Bark is secondary to publication:

- send the two notifications independently so one failure does not suppress the other;
- use one POST attempt per notification to avoid duplicate delivery after an ambiguous timeout;
- enforce a bounded HTTP timeout;
- validate Bark's HTTP and JSON success response;
- log only the notification number and sanitized error class/status;
- never fail or roll back the successfully published feed because Bark failed.

DeepSeek recommendation failure is also nonfatal. It is accounted for, publication proceeds, and Bark sends the explicit recommendation-failure message when possible.

## Components

- `src/diamond_feed/recommend.py`: focus-name mapping, recommendation prompt, strict response validation, and recommendation result model.
- `src/diamond_feed/bark.py`: official Bark client, payload construction, sanitized error handling, and two-message orchestration.
- `src/diamond_feed/enrich.py`: Semantic Scholar batch lookup, OpenAIRE fallback, identity/content validation, and nonfatal enrichment orchestration.
- `src/diamond_feed/notify.py`: consume the ephemeral notification plan and invoke Bark after the repository push.
- `src/diamond_feed/summarize.py`: production-only enrichment, recommendation pool, sixth request, extended stats/result, and ephemeral notification-plan generation.
- `src/diamond_feed/config.py` and `paper_feed_config.json`: six-request ceiling and recommendation output limit.
- `.github/workflows/summarize.yml`: inject `SEMANTIC_SCHOLAR_API_KEY` only into production summarization, pass only `BARK_ENABLED` to that step, and inject `BARK_TOKEN` only into the post-push notification step.
- `README.md`: secret setup, abstract-source cascade, notification meanings, request/token behavior, and failure semantics.

## Testing and Acceptance

Tests must prove:

- Semantic Scholar uses one authenticated batch request for at most 500 prioritized DOI records and never exposes its key;
- OpenAIRE receives only current-candidate misses in groups of at most five;
- enrichment rejects DOI/title mismatches, title-equivalent text, boilerplate, and oversized values without truncating valid abstracts;
- accepted enrichment preserves the full original abstract, records its source, and requeues a previously title-only decision;
- enrichment failure leaves records unchanged and does not block screening, publication, evaluation, smoke tests, or offline promotion;
- the recommendation prompt includes complete original abstracts and identifies missing abstracts;
- only a newly accepted controlled-focus candidate can be returned;
- empty, unknown, broad-only, duplicate-key, extra-field, and overlong responses are rejected;
- a multi-label recommendation renders all directions in Chinese;
- zero focus candidates use no sixth request and produce the explicit no-recommendation message;
- a valid focus pool uses exactly one additional request, for a maximum of six;
- absent `BARK_TOKEN` uses no sixth request and sends nothing;
- Bark receives two JSON POSTs at `https://api.day.app/push` only after a successful repository push, with the token in the body and never the URL or logs;
- Bark errors do not change published files or the summary exit status;
- no notification is sent after a summary, commit, rebase, or push failure;
- no-op reruns, evaluation, promotion, and collection send no Bark messages;
- workflow secret boundaries and credential scans cover both `BARK_TOKEN` and `SEMANTIC_SCHOLAR_API_KEY`;
- the full suite, compilation, output validation, and live workflow remain green.

The user will add `BARK_TOKEN` and `SEMANTIC_SCHOLAR_API_KEY` directly in GitHub Actions Secrets after deployment; neither token must be pasted into chat or committed.
