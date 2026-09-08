# Cumulative AI Feed Implementation Plan

> **For agentic workers:** Use subagent-driven-development to implement the code task, then review and release. Steps use checkbox syntax for tracking.

**Goal:** Publish the already evaluated 15 papers at the stable feed URL, hold the legacy 10 outside publication, and accumulate subsequent accepted papers without resummarizing history.

**Architecture:** state.json remains the source of scholarly metadata and decisions. A small publication policy holds stable identity aliases for user-withheld papers. Shared publication helpers derive a deduplicated accepted collection; an offline promotion command validates and imports an existing evaluation, then atomically writes RSS, HTML and state without touching usage.

**Tech Stack:** Python 3.11+, pytest, existing atomic staging, RSS ElementTree renderer, GitHub Pages.

## Global Constraints

- Formal feed starts with exactly the new 15 papers; legacy 10 records, summaries and judgments stay in state.json but are withheld from publication.
- No real model/API requests during implementation, tests, promotion or publication. Do not read or print credentials.
- Daily model limits remain 40 unique candidates and 5 attempts. Historical accepted papers are not resent for a daily overview.
- Cumulative AI RSS and HTML retain all accepted non-withheld papers. Raw RSS retains its 2000-item cap.
- No state schema migration, no duplicate archive database. Preserve canonical keys, source metadata, continuations, watermarks and unrelated queue entries.
- Every promotion write is atomic across state, RSS and HTML. ai_usage.json and evaluation artifacts are untouched.
- Existing partial-failure behavior, secret hygiene and rollback tests remain valid.

## Task 1: Cumulative publishing and offline promotion

**Files:** create src/diamond_feed/publication.py and src/diamond_feed/promote.py; modify src/diamond_feed/summarize.py and src/diamond_feed/render.py; create tests/test_cumulative_publication.py and tests/test_promote.py. Do not edit real root outputs, config/ai_publication.json, README or evaluation artifacts: the controller owns those.

**Interfaces:**

```python
def load_withheld_aliases(path: Path) -> set[str]: ...
def cumulative_records(state: FeedState, withheld_aliases: set[str]) -> list[PaperRecord]: ...
def promote_evaluation(config, state_path: Path, report_path: Path, now: datetime,
                       *, output_dir: Path, policy_path: Path | None = None) -> dict: ...
```

Policy schema: exactly {"version": 1, "withheld_identity_aliases": [string, ...]}; absent policy means an empty set for isolated tests/evaluations. Default policy is state_path.parent / "config/ai_publication.json". Malformed policy fails closed before model calls or publication.

- [x] Write and run failing tests before implementation: two different days each with a new accepted paper retain both in RSS/HTML, while day-two model input contains only the new paper; an all-negative day retains history; a later rejected alias removes an earlier accepted duplicate; withheld identities stay out without mutating state decisions; 2001 accepted papers remain in AI RSS, raw renderer still caps at 2000.

Test pattern for cumulative outputs (use real state/output IO and an existing injected fake client):

```python
first = run_summary(config, state_path, client, day_one, output_dir=tmp_path)
# Add one distinct pending PaperRecord to persisted state, then:
second = run_summary(config, state_path, client, day_two, output_dir=tmp_path)
items = ElementTree.parse(tmp_path / "ai_summary_feed.xml").findall("./channel/item")
assert len(items) == 2
assert first.selected == second.selected == 1
```

- [x] Implement strict policy loading and cumulative collection. Group all state records by existing group_records identity logic. Published collection requires an accepted decision and nonempty summary, no conflicting explicit rejection, and no member identity in the withheld set. Use merged metadata. Do not mutate caller data.
- [x] Daily screening groups all state records but schedules only groups containing pending keys; propagate each new decision to every alias in its group, including previously processed aliases, so rejections cannot leave stale positive copies. Preserve 40/5 limits and daily stats as THIS RUN counts.
- [x] After successful/partial screening, render all cumulative non-withheld papers, but send only this run's accepted non-withheld papers to the AI overview. Clearly distinguish overview/new-run count from cumulative total. Empty/no-budget no-op preserves prior outputs. Keep model-generated HTML sanitization.
- [x] Add a backward-compatible keyword-only option to render_rss to disable the 2000-item cap for AI publication only; keep default raw behavior and negative-limit validation.
- [x] Write and run promotion failure tests: failed/incomplete report; duplicate keys or contradictory aliases; missing state record; invalid boolean/confidence/category; changed title/abstract input; publication path collision; mid-publication failure. Each must preserve state/RSS/HTML/usage bytes.
- [x] Implement promotion without constructing any network client: report must be complete with candidates == processed == unique_records and valid per-paper decisions; verify original keys and title/truncated-abstract input against current state. Resolve all existing aliases. Apply report's existing accepted and rejected decisions, remove covered pending keys, preserve other records and queue order. Conflicting alias outcomes are rejected. Repeated import is idempotent; do not call the model or change ai_usage.json. Return imported/selected/published/remaining counts. Provide CLI --report, --state, --config, --output-dir and optional --policy; no key/environment requirement.
- [x] Use existing stage_text/stage_state/commit_staged and cleanup protocol for RSS, HTML, state. Report cannot alias an output path. Preserve all original metadata; empty original abstract may get a transparent title-only note in rendered text but do not alter saved model results.
- [x] Focused tests then full `python -m pytest -q`; git diff --check. Commit only task code/tests and write the TDD report to .superpowers/sdd/cumulative-task-report.md.

## Task 2: Seed held-back policy, promote, verify and publish (controller)

- [x] Build config/ai_publication.json from identity_aliases of the current 10 accepted legacy state records; record count and keys without printing secrets. Preserve legacy HTML/RSS through Git history.
- [x] Run Task 1 review; fix Critical/Important findings and repeat covering tests.
- [x] Run offline promotion of evaluations/34175877785/report.json. Assert 15 unique RSS GUIDs/15 HTML articles, old 10 original records unchanged, ai_usage.json byte-identical, raw feed unchanged, pending 1274 -> 1234 and no processed evaluation key pending.
- [x] Re-run promotion and compare file hashes to verify idempotence. Update README with cumulative policy/holdback/import semantics and zero additional AI cost. Record completion of the ledger.
- [x] Full test suite and final whole-branch review; verify no credentials in staged files. Publish only explicit code/config/docs/state/summary paths, never the usage ledger or altered evaluation results.
- [ ] Verify GitHub Pages build and live root RSS/HTML HTTP 200 and 15 entries. Report the unchanged stable URL, new cumulative behavior, withheld legacy entries, and zero new model requests.
