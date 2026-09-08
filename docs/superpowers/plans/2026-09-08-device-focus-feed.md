# Three-Direction Diamond Device RSS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a cumulative `device_focus_feed.xml` containing only DeepSeek-approved diamond power/RF/detector, device thermal-management, and device-grade single-crystal papers without narrowing the comprehensive AI feed or adding a second model request.

**Architecture:** The existing screening response's `matched_topics` field becomes a strict three-label focus classification and is persisted after the primary broad category in `PaperRecord.categories`, avoiding a state-schema migration. A focused-publication module combines those stored labels with an exact four-paper historical override, then the daily summary and offline-promotion transactions render the additional RSS atomically beside existing outputs.

**Tech Stack:** Python 3.11+, dataclasses, JSON, ElementTree RSS renderer, pytest, GitHub Actions, GitHub Pages.

## Global Constraints

- Preserve the purpose and coverage of `filtered_feed.xml`, `ai_summary_feed.xml`, and `ai_summary.html`.
- Use exactly `diamond-power-rf-detectors`, `diamond-thermal-management`, and `device-grade-single-crystal` as focus labels; labels are multi-select.
- Detector scope includes device-realized NV/SiV sensing but excludes pure color-center physics and unimplemented theoretical protocols.
- Single-crystal scope requires an explicit electronic-grade, fabrication, integration, or device-performance connection.
- Reuse the existing screening calls; retain limits of 40 unique candidates and 5 request attempts per day. Do not add a second AI pass.
- Existing 15-paper publication receives four exact DOI overrides and no model request.
- Publish focused records cumulatively and once per DOI/arXiv/Figshare identity; respect the existing legacy-withhold policy.
- Atomically publish the new RSS with the existing summary transaction. Preserve old outputs on validation, staging, or commit failure.
- Do not expose, read, print, or commit the DeepSeek credential.

---

## File Structure

- Create `src/diamond_feed/focus.py`: focus-label constants, exact override parsing, and focused cumulative selection.
- Create `config/device_focus_overrides.json`: transparent four-paper historical seed.
- Create `tests/test_device_focus.py`: contract, selection, accumulation, rollback, and backfill behavior.
- Modify `src/diamond_feed/ai.py`: strict focus-label validation and prompt definitions using `matched_topics`.
- Modify `src/diamond_feed/summarize.py`: persist focus labels and render/stage the focused RSS.
- Modify `src/diamond_feed/promote.py`: rebuild the focused RSS during zero-request offline promotion.
- Modify `.github/workflows/summarize.yml`: allowlist `device_focus_feed.xml` for publication.
- Modify `tests/conftest.py`, `tests/test_ai.py`, `tests/test_summarize.py`, `tests/test_cumulative_publication.py`, `tests/test_acceptance.py`, `tests/test_workflows.py`, and `tests/test_promote.py`: update fixtures and verify unchanged budgets/coverage plus the added atomic output.
- Modify `README.md`: document scope, cost semantics, seed count, and subscription URL.
- Generate `device_focus_feed.xml`: initial four-item cumulative RSS.

---

### Task 1: Strict focus classification and cumulative selection

**Files:**
- Create: `src/diamond_feed/focus.py`
- Create: `tests/test_device_focus.py`
- Modify: `src/diamond_feed/ai.py`
- Modify: `src/diamond_feed/summarize.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_ai.py`
- Modify: `tests/test_summarize.py`
- Modify: `tests/test_cumulative_publication.py`

**Interfaces:**
- Produces: `FOCUS_LABELS: frozenset[str]`
- Produces: `load_focus_overrides(path: Path) -> dict[str, frozenset[str]]`
- Produces: `focused_records(state: FeedState, withheld_aliases: set[str], overrides: dict[str, frozenset[str]]) -> list[PaperRecord]`
- Changes: `AiDecision.matched_topics` remains `list[str]`, but only exact values from `FOCUS_LABELS` are accepted.
- Changes: `_apply_decision` persists `[decision.category, *decision.matched_topics]` in stable order.

- [x] **Step 1: Write failing AI-contract tests**

Add focused tests that request one paper and assert an approved multi-label response survives validation, while unknown labels and labels on a `relevant=false` result fail before state mutation:

```python
def test_screening_accepts_only_controlled_focus_labels(ai_config, diamond_records):
    key = record_key(diamond_records[0])
    client = DeepSeekClient(
        "test-api-key",
        ai_config,
        transport=lambda *args: _response(json.dumps({"decisions": [_decision(
            key,
            matched_topics=["diamond-power-rf-detectors", "device-grade-single-crystal"],
        )]})),
    )
    decision = screen_batch(diamond_records[:1], client, ai_config, RequestBudget(1))[0]
    assert decision.matched_topics == [
        "diamond-power-rf-detectors", "device-grade-single-crystal"
    ]


@pytest.mark.parametrize("topics", [["diamond"], ["unknown"], [1]])
def test_screening_rejects_unknown_focus_labels(ai_config, diamond_records, topics):
    key = record_key(diamond_records[0])
    client = DeepSeekClient(
        "test-api-key",
        ai_config,
        transport=lambda *args: _response(json.dumps({"decisions": [
            _decision(key, matched_topics=topics)
        ]})),
    )
    with pytest.raises(ValueError, match="invalid model response"):
        screen_batch(diamond_records[:1], client, ai_config, RequestBudget(1))


def test_excluded_decision_cannot_claim_focus_area(ai_config, diamond_records):
    key = record_key(diamond_records[0])
    client = DeepSeekClient(
        "test-api-key",
        ai_config,
        transport=lambda *args: _response(json.dumps({"decisions": [_decision(
            key,
            relevant=False,
            matched_topics=["diamond-power-rf-detectors"],
            summary_zh="",
            reason="不属于金刚石材料研究。",
        )]})),
    )
    with pytest.raises(ValueError, match="invalid model response"):
        screen_batch(diamond_records[:1], client, ai_config, RequestBudget(1))
```

- [x] **Step 2: Run the AI-contract tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_ai.py -k "focus_labels or claim_focus"
```

Expected: FAIL because arbitrary strings in `matched_topics` are currently accepted.

- [x] **Step 3: Implement the strict one-pass label contract**

In `src/diamond_feed/focus.py`, define:

```python
FOCUS_LABELS = frozenset({
    "diamond-power-rf-detectors",
    "diamond-thermal-management",
    "device-grade-single-crystal",
})
```

In `src/diamond_feed/ai.py`, import `FOCUS_LABELS`; reject any non-string, duplicate, or unknown `matched_topics` entry, and reject nonempty topics when `relevant` is false. Extend the system prompt with the approved inclusion/exclusion definitions and explicitly require zero or more exact focus labels without changing `DECISION_FIELDS` or making another request.

Update every fake response currently returning `matched_topics=["diamond"]` or descriptive free text to return `[]` unless that test is specifically exercising a controlled label.

- [x] **Step 4: Run the AI tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests/test_ai.py tests/test_relevance_regressions.py
```

Expected: all selected tests PASS; request-budget assertions remain unchanged.

- [x] **Step 5: Write failing override and focused-selection tests**

Cover absent overrides, the exact schema, duplicate JSON keys, unknown labels, invalid identities, stored labels, override labels, multi-label deduplication, rejected aliases, and withheld aliases. The central behavior should be explicit:

```python
def test_focused_records_include_stored_and_overridden_labels_once():
    state = FeedState(papers={
        record_key(stored): stored,
        record_key(overridden): overridden,
        record_key(general_only): general_only,
    })
    records = focused_records(
        state,
        withheld_aliases=set(),
        overrides={record_key(overridden): frozenset({"diamond-power-rf-detectors"})},
    )
    assert {record_key(record) for record in records} == {
        record_key(stored), record_key(overridden)
    }
```

- [x] **Step 6: Run selection tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_device_focus.py -k "override or focused_records"
```

Expected: collection fails because `diamond_feed.focus` does not yet exist.

- [x] **Step 7: Implement exact override parsing and selection**

Implement `load_focus_overrides` with `json.loads(..., object_pairs_hook=...)` so duplicate object keys raise `ValueError`. Accept only root keys `version` and `papers`, require integer version `1`, require every paper key to start with `doi:`, `arxiv:`, `figshare:`, or `title:`, and require each nonempty label list to contain unique members of `FOCUS_LABELS`. Missing files return `{}` for isolated tests.

Implement `focused_records` by deriving the same accepted, non-withheld grouped records as `cumulative_records`, then including only merged records whose `categories` contain a focus label or whose stable identity aliases match an override. Return each merged identity once and never mutate state records.

Update `_apply_decision` to persist the broad category first and validated focus labels afterward:

```python
record.categories = [decision.category, *decision.matched_topics]
```

- [x] **Step 8: Run Task 1 tests and commit**

Run:

```powershell
python -m pytest -q tests/test_ai.py tests/test_summarize.py tests/test_cumulative_publication.py tests/test_device_focus.py tests/test_relevance_regressions.py
git diff --check
```

Expected: all selected tests PASS and `git diff --check` exits 0.

Commit only Task 1 paths:

```powershell
git add src/diamond_feed/focus.py src/diamond_feed/ai.py src/diamond_feed/summarize.py tests/conftest.py tests/test_ai.py tests/test_summarize.py tests/test_cumulative_publication.py tests/test_device_focus.py tests/test_relevance_regressions.py
git commit -m "feat: classify three diamond device focus areas"
```

---

### Task 2: Atomic focused RSS publication

**Files:**
- Modify: `src/diamond_feed/focus.py`
- Modify: `src/diamond_feed/summarize.py`
- Modify: `src/diamond_feed/promote.py`
- Modify: `.github/workflows/summarize.yml`
- Modify: `tests/test_device_focus.py`
- Modify: `tests/test_promote.py`
- Modify: `tests/test_workflows.py`
- Modify: `tests/test_acceptance.py`

**Interfaces:**
- Produces: `FOCUS_RSS_NAME = "device_focus_feed.xml"`
- Produces: `render_focused_rss(state: FeedState, config: AppConfig, withheld_aliases: set[str], overrides: dict[str, frozenset[str]]) -> str`
- Changes: `run_summary(..., focus_overrides_path: Path | None = None)` loads default `state_path.parent / "config/device_focus_overrides.json"`.
- Changes: `promote_evaluation(..., focus_overrides_path: Path | None = None)` renders the same focused RSS without constructing a model client.

- [x] **Step 1: Write failing two-day accumulation and no-extra-request tests**

Use a recording fake client whose screening response assigns a focus label. Run two daily batches with one distinct accepted paper each; assert the original AI feed and focused feed both contain two papers, while day two sends only the new paper and uses the same screening-plus-overview request count as before:

```python
assert len(_feed_titles(tmp_path / "ai_summary_feed.xml")) == 2
assert len(_feed_titles(tmp_path / "device_focus_feed.xml")) == 2
assert second.requests == 2
assert [paper["title"] for paper in day_two_screening["papers"]] == [second_record.title]
```

Also run an accepted general-only paper with `matched_topics=[]` and assert it appears in `ai_summary_feed.xml` but not `device_focus_feed.xml`.

- [x] **Step 2: Run publication tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_device_focus.py -k "two_days or general_only"
```

Expected: FAIL because `device_focus_feed.xml` is not produced.

- [x] **Step 3: Implement focused RSS rendering and daily staging**

In `focus.py`, build feed records from `focused_records`, replace each RSS abstract with `summary_zh`, and call the existing renderer with no 2,000 cap:

```python
return render_rss(
    feed_records,
    "Diamond Device Focus Feed · 中文摘要",
    config.publication.base_url,
    len(feed_records),
    cap_at_2000=False,
)
```

At the start of `run_summary`, load focus overrides before any model call. Add the focused path to `validate_output_layout`, render it from the updated state, stage it between comprehensive outputs and state, and include it in the same `commit_staged` call. Keep candidate-limit, request-limit, early-return, and usage behavior unchanged.

- [x] **Step 4: Write and verify RED for failure preservation**

Add tests that precreate all outputs, simulate `os.replace` failing specifically for `device_focus_feed.xml.tmp`, and assert byte-for-byte restoration of comprehensive RSS, HTML, focused RSS, usage, and state with no temporary or backup debris. Add malformed override tests proving rejection occurs before the fake client's first call.

Run:

```powershell
python -m pytest -q tests/test_device_focus.py -k "rollback or malformed"
```

Expected: at least the focused-output rollback test FAILS before integration is complete.

- [x] **Step 5: Integrate offline promotion and path guards**

Load default or explicit focus overrides in `promote_evaluation`, include override and focused-output paths in collision validation, render the focused RSS from imported state, and stage focused RSS in the same transaction as comprehensive RSS, HTML, and state. Add CLI option:

```python
parser.add_argument("--focus-overrides", type=Path)
```

Promotion must continue returning `model_requests=0`, must leave `ai_usage.json` and the report byte-identical, and must be idempotent across all four outputs.

- [x] **Step 6: Update the workflow allowlist and tests**

Add only `"device_focus_feed.xml"` to the summary workflow's `allowed_outputs`. Update workflow tests so the expected summary outputs are:

```python
(
    "ai_summary_feed.xml",
    "ai_summary.html",
    "device_focus_feed.xml",
    "ai_usage.json",
    "state.json",
)
```

Update acceptance tests to require valid RSS 2.0 XML with the focused title and to ensure the collection workflow does not receive the new output or the AI secret.

- [x] **Step 7: Run Task 2 tests and commit**

Run:

```powershell
python -m pytest -q tests/test_device_focus.py tests/test_promote.py tests/test_workflows.py tests/test_acceptance.py tests/test_summarize.py
git diff --check
```

Expected: all selected tests PASS and no fixed-output allowlist regression.

Commit only Task 2 paths:

```powershell
git add src/diamond_feed/focus.py src/diamond_feed/summarize.py src/diamond_feed/promote.py .github/workflows/summarize.yml tests/test_device_focus.py tests/test_promote.py tests/test_workflows.py tests/test_acceptance.py tests/test_summarize.py
git commit -m "feat: publish cumulative diamond device focus RSS"
```

---

### Task 3: Seed, document, verify, and release

**Files:**
- Create: `config/device_focus_overrides.json`
- Create: `device_focus_feed.xml`
- Modify: `README.md`
- Test: `tests/test_device_focus.py`
- Preserve unchanged: `ai_usage.json`, `filtered_feed.xml`, evaluation reports, and DeepSeek secret configuration.

**Interfaces:**
- Consumes: `load_focus_overrides`, `render_focused_rss`, and the updated offline-promotion CLI.
- Produces: public `https://llxyyds666.github.io/diamond-paper-feed/device_focus_feed.xml`.

- [x] **Step 1: Add the exact four-paper seed with a failing fixture test**

Write a repository-level test asserting the override file is exact and contains these four DOI identities, each mapped only to `diamond-power-rf-detectors`:

```json
{
  "version": 1,
  "papers": {
    "doi:10.1016/j.diamond.2026.114067": ["diamond-power-rf-detectors"],
    "doi:10.1049/ell2.70574": ["diamond-power-rf-detectors"],
    "doi:10.1080/08957959.2026.2651875": ["diamond-power-rf-detectors"],
    "doi:10.1080/08957959.2026.2661257": ["diamond-power-rf-detectors"]
  }
}
```

Run the test before creating the file and verify it fails because the file is absent. Then create the file with `apply_patch` and rerun to PASS.

- [x] **Step 2: Synchronize against current remote data without losing scheduled collection**

Read the latest remote `main` SHA and changed paths. Bring the latest `state.json`, `filtered_feed.xml`, and `fetch_failures.tsv` into the release base through a normal fetch/rebase when available. If Git HTTPS remains unavailable, retain the verified remote parent SHA and publish the final full tree through the GitHub Git Data API with a non-force compare-and-swap update; never replace scheduled collection outputs with older local copies.

Before seeding, assert the remote state still contains all 15 accepted non-withheld identities and all 10 withheld legacy identities with their decisions intact.

- [x] **Step 3: Generate the four-item focused feed with zero AI requests**

Snapshot SHA-256 hashes of `ai_usage.json`, `filtered_feed.xml`, `ai_summary_feed.xml`, `ai_summary.html`, and the evaluation report. Run:

```powershell
python -m diamond_feed.promote --report evaluations/34175877785/report.json --state state.json --config paper_feed_config.json --output-dir . --focus-overrides config/device_focus_overrides.json
```

Assert `model_requests=0`; parse `device_focus_feed.xml`; require four unique GUIDs matching the override identities; and require all snapshotted unrelated files to remain byte-identical except comprehensive outputs that are expected to render identically. Run promotion a second time and require byte-for-byte identical state and all three publication files.

- [x] **Step 4: Document the focused feed**

Update README output, manual-workflow, URL, scope, and cost sections. State plainly that the general AI feed remains broad, the new feed is an AI-approved subset, current seed count is four, no second request is made, and adding label tokens may cause a negligible token-count difference.

- [x] **Step 5: Run final verification**

Run:

```powershell
python -m pytest -q
python -m compileall -q src tests
git diff --check
```

Parse all three RSS files with ElementTree. Verify the comprehensive feed still has at least its pre-change identities, the focused feed has exactly four initial identities, the old 10 remain withheld, request limits remain 40/5, and `ai_usage.json` plus evaluation artifacts are unchanged. Scan staged files for API-key patterns and ensure only declared paths are staged.

- [x] **Step 6: Commit and deploy**

Commit configuration, generated focused RSS, README, tests, and any release-state changes with:

```powershell
git add config/device_focus_overrides.json device_focus_feed.xml README.md tests/test_device_focus.py
git commit -m "data: seed diamond device focus feed"
```

Publish to `main` only after confirming its SHA still equals the reviewed release parent. Wait for the Pages build to complete.

- [x] **Step 7: Verify the live subscription**

Fetch both `device_focus_feed.xml` and `ai_summary_feed.xml` with cache-busting query parameters. Require HTTP 200, valid RSS 2.0, four focused items, comprehensive items retained, and remote hashes matching the release tree. Open the focused URL for the user and report the request count and boundaries in Simplified Chinese.
