# Three-Direction Diamond Device RSS Design

## Goal

Add a separate cumulative RSS at `device_focus_feed.xml` for three approved directions while leaving `filtered_feed.xml`, `ai_summary_feed.xml`, and `ai_summary.html` unchanged in purpose and coverage. The focused feed favors precision over volume and never tries to fill a quota.

## Approved Scope

A generally relevant diamond paper enters the focused feed when DeepSeek assigns at least one of these controlled labels:

1. `diamond-power-rf-detectors`: diamond power-semiconductor devices, RF/high-frequency devices, radiation/particle/X-ray/UV detectors, or device-realized NV/SiV quantum sensing. Include contacts, dielectrics, interfaces, structures, fabrication, reliability, and characterization when explicitly tied to one of these devices. Exclude pure color-center physics and unimplemented theoretical quantum protocols.
2. `diamond-thermal-management`: thermal management of diamond devices, or diamond used as a heat spreader, substrate, coating, composite, thermal interface, or heterogeneous integration layer for devices such as GaN and SiC. Include interface thermal resistance, integration damage, packaging, and reliability. Exclude thermal-property studies without a device heat-management connection.
3. `device-grade-single-crystal`: single-crystal diamond growth, doping, defects, surfaces, contacts, and processing only when the title or abstract explicitly connects the work to electronic-grade material, device fabrication, integration, or device performance. Exclude natural-diamond geology, gemology, and generic single-crystal characterization without device relevance.

Labels are multi-select. A paper can appear in the focused RSS only once even when it belongs to multiple directions. A relevant paper with no focus label remains in the comprehensive AI feed but does not enter the focused feed.

## Classification and Cost

The existing daily DeepSeek screening request will return the controlled focus labels in its existing `matched_topics` list. No second model pass is added, so daily limits remain 40 unique candidates and 5 request attempts. Adding a few output label tokens can slightly change token usage, but there is no additional request solely for this RSS.

Validation accepts only the three exact labels and requires excluded papers to return no focus labels. The primary broad category remains the first value persisted in `PaperRecord.categories`; approved focus labels follow it. This reuses the current state schema and leaves comprehensive HTML grouping unchanged.

## Initial Conservative Backfill

Existing saved decisions do not contain focus labels. An exact, auditable override file will backfill only four of the current 15 papers whose titles and saved summaries clearly satisfy the approved device scope:

- `doi:10.1016/j.diamond.2026.114067` — diamond-channel MOSFET.
- `doi:10.1049/ell2.70574` — diamond ohmic contacts.
- `doi:10.1080/08957959.2026.2651875` — implemented wide-field diamond quantum sensor.
- `doi:10.1080/08957959.2026.2661257` — diamond quantum-sensing measurement.

The override schema is exactly:

```json
{
  "version": 1,
  "papers": {
    "doi:<normalized-doi>": ["diamond-power-rf-detectors"]
  }
}
```

Unknown fields, unknown labels, malformed identities, or conflicting duplicate entries fail closed. Overrides affect focused publication only; they do not rewrite saved AI judgments. The other 11 papers are conservatively excluded. No DeepSeek request is made for the backfill.

## Publication Flow

After a successful or partially successful daily screening batch:

1. Apply the general relevance decision, primary category, summary, and validated focus labels to every identity alias of the paper.
2. Rebuild the comprehensive cumulative RSS and HTML exactly as today.
3. Derive a second deduplicated collection from accepted, non-withheld papers carrying a focus label in state or the exact override file.
4. Render all focused records, newest first, to `device_focus_feed.xml` with their existing Chinese summaries.
5. Atomically commit comprehensive RSS, HTML, focused RSS, usage ledger, and state. Any staging or publication failure preserves the previous complete set.

No-candidate and exhausted-budget runs remain no-ops and preserve existing outputs. Offline evaluation promotion also rebuilds the focused RSS in its atomic transaction, enabling the initial backfill without a model call. The collect workflow does not write this AI-derived feed.

## User-Facing Output

- URL: `https://llxyyds666.github.io/diamond-paper-feed/device_focus_feed.xml`
- Title: `Diamond Device Focus Feed · 中文摘要`
- Format: RSS 2.0 using the current DOI/arXiv/Figshare identity deduplication and Chinese summaries.
- Retention: cumulative and uncapped by the raw feed's 2,000-item limit.

README will document the three boundaries, the independent subscription URL, the four-paper conservative seed, and the fact that it shares the existing request budget.

## Verification

Tests must first fail for the missing behavior, then cover:

- exact focus-label validation, multi-label decisions, and rejection of unknown labels;
- comprehensive accepted papers remaining in the original feed even without a focus label;
- focused papers appearing once in the new RSS and accumulating across days;
- pure color-center theory, generic thermal properties, and non-device single-crystal work staying out;
- the four explicit historical overrides yielding four initial feed items without changing their state records;
- all identity aliases sharing later focus decisions;
- no-candidate/no-budget preservation and atomic rollback across the added output;
- unchanged 40-candidate/5-attempt limits and no extra model call;
- workflow publication path allowlist, XML validity, secret hygiene, full regression suite, and live GitHub Pages HTTP/item-count verification.
