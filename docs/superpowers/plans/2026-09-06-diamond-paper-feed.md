# Diamond Paper Feed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a GitHub-hosted, high-recall diamond-research literature feed that aggregates RSS, OpenAlex, Crossref, and arXiv, then uses DeepSeek for bounded daily Chinese screening and summaries.

**Architecture:** Source adapters emit one `PaperRecord` model; normalization, rule recall, and DOI/title deduplication run before records enter a JSON-backed state and AI queue. Collection and summarization run as separate GitHub Actions workflows so a large first harvest cannot increase the fixed DeepSeek request budget.

**Tech Stack:** Python 3.11, standard-library `urllib` and `xml.etree`, `feedparser`, `curl-cffi` for MDPI feeds, `pytest`, GitHub Actions, GitHub Pages, DeepSeek OpenAI-compatible Chat Completions API.

## Global Constraints

- Repository and Python package name: `diamond-paper-feed` and `diamond_feed`.
- The public AI base URL is `https://api.deepseek.com`; the model is `deepseek-v4-flash-vision-exp`.
- The only AI credential variable is `DEEPSEEK_API_KEY`; never write its value or fragments to tracked files, generated files, tests, logs, commands, or responses.
- First database bootstrap lookback is exactly 30 days.
- Collection runs every 6 hours; AI summarization runs once daily at 08:00 Asia/Shanghai.
- AI limits are hard limits: 40 candidate papers/day, batch size 10, and 5 total request attempts/run including retries.
- Screening output is capped at 4096 tokens/request; final digest output is capped at 8192 tokens.
- Each abstract sent to AI is truncated to 1200 Unicode characters.
- `filtered_feed.xml` retains at most 2000 records.
- Persist pending AI work before invoking DeepSeek; failures must not remove queued papers or overwrite the last valid digest.
- Treat stable HTTP 404/410, stable malformed XML, and retired endpoints as hard RSS failures. Treat timeout, transient network errors, and empty feeds as soft failures.
- Do not commit `vendor/`, `__pycache__/`, `.env`, credentials, copied upstream output feeds, or user-specific configuration.

---

## File Map

Create the following focused units:

- `pyproject.toml`: package metadata, runtime dependencies, pytest configuration.
- `.gitignore`: generated caches, environments, secrets, and temporary state.
- `paper_feed_config.json`: public collection, AI, and publication settings.
- `config/queries.json`: high-recall diamond phrases and obvious noise contexts.
- `config/rss_sources.tsv`: final verified RSS registry with name, category, and URL.
- `src/diamond_feed/config.py`: validated public configuration loader.
- `src/diamond_feed/models.py`: `PaperRecord`, `SourceFailure`, and AI decision models.
- `src/diamond_feed/normalize.py`: DOI/title normalization and record merging.
- `src/diamond_feed/filtering.py`: deterministic high-recall rule filter.
- `src/diamond_feed/http.py`: bounded HTTP retries and publisher-specific fetching.
- `src/diamond_feed/sources/rss.py`: RSS/Atom parsing.
- `src/diamond_feed/sources/openalex.py`: OpenAlex query and response adapter.
- `src/diamond_feed/sources/crossref.py`: Crossref query and response adapter.
- `src/diamond_feed/sources/arxiv.py`: arXiv query and Atom adapter.
- `src/diamond_feed/state.py`: atomic JSON state persistence and AI queue.
- `src/diamond_feed/render.py`: standards-compliant raw and AI RSS rendering.
- `src/diamond_feed/collect.py`: collection CLI and failure report output.
- `src/diamond_feed/ai.py`: DeepSeek client, request budget, and JSON validation.
- `src/diamond_feed/summarize.py`: daily queue consumer, AI feed, HTML, and usage output.
- `scripts/import_rss_sources.py`: deterministic import/deduplication of local source lists.
- `scripts/validate_rss_sources.py`: full RSS validation and failure classification.
- `.github/workflows/collect.yml`: six-hour collection job.
- `.github/workflows/summarize.yml`: daily bounded DeepSeek job.
- `tests/fixtures/`: fixed RSS and API response samples.
- `tests/`: unit and integration tests corresponding to each module.
- `README.md`: setup, Secret, Pages, feeds, operations, and recovery instructions.

---

### Task 1: Package skeleton and validated public configuration

**Files:**
- Create: `.gitignore`
- Create: `pyproject.toml`
- Create: `paper_feed_config.json`
- Create: `src/diamond_feed/__init__.py`
- Create: `src/diamond_feed/config.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Produces: `load_config(path: Path) -> AppConfig`
- Produces: immutable `AppConfig`, `CollectionConfig`, `AiConfig`, and `PublicationConfig` dataclasses.

- [ ] **Step 1: Write the failing configuration test**

```python
from pathlib import Path

import pytest

from diamond_feed.config import load_config


def test_repository_config_has_locked_safety_limits():
    config = load_config(Path("paper_feed_config.json"))
    assert config.collection.lookback_days == 30
    assert config.collection.raw_feed_max_items == 2000
    assert config.ai.model == "deepseek-v4-flash-vision-exp"
    assert config.ai.daily_candidates == 40
    assert config.ai.batch_size == 10
    assert config.ai.max_requests == 5
    assert config.ai.max_abstract_chars == 1200


def test_invalid_batch_budget_is_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        '{"collection":{"lookback_days":30,"raw_feed_max_items":2000,'
        '"http_timeout_seconds":30,"http_attempts":3},'
        '"ai":{"base_url":"https://api.deepseek.com","model":"deepseek-v4-flash-vision-exp",'
        '"daily_candidates":40,"batch_size":10,"max_requests":4,"max_abstract_chars":1200,'
        '"screening_max_tokens":4096,"digest_max_tokens":8192},'
        '"publication":{"title":"Diamond Paper Feed","base_url":""}}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reserve one request"):
        load_config(path)
```

- [ ] **Step 2: Run the test and confirm the missing package failure**

Run: `python -m pytest tests/test_config.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'diamond_feed'`.

- [ ] **Step 3: Create packaging, ignore rules, and exact public defaults**

`pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "diamond-paper-feed"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "feedparser>=6.0.11,<7",
  "curl-cffi>=0.13,<1",
]

[project.optional-dependencies]
dev = ["pytest>=8,<9"]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

`.gitignore`:

```gitignore
__pycache__/
*.py[cod]
.pytest_cache/
.venv/
.env
*.tmp
*.bak
vendor/
```

`paper_feed_config.json`:

```json
{
  "collection": {
    "lookback_days": 30,
    "raw_feed_max_items": 2000,
    "http_timeout_seconds": 30,
    "http_attempts": 3
  },
  "ai": {
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-v4-flash-vision-exp",
    "daily_candidates": 40,
    "batch_size": 10,
    "max_requests": 5,
    "max_abstract_chars": 1200,
    "screening_max_tokens": 4096,
    "digest_max_tokens": 8192
  },
  "publication": {
    "title": "Diamond Paper Feed",
    "base_url": ""
  }
}
```

- [ ] **Step 4: Implement strict dataclass loading**

`src/diamond_feed/config.py` must parse JSON, reject unknown non-positive limits, require HTTPS for the AI base URL, and enforce `ceil(daily_candidates / batch_size) + 1 <= max_requests`. The public dataclasses are:

```python
from dataclasses import dataclass
from pathlib import Path
import json
import math


@dataclass(frozen=True, slots=True)
class CollectionConfig:
    lookback_days: int
    raw_feed_max_items: int
    http_timeout_seconds: int
    http_attempts: int


@dataclass(frozen=True, slots=True)
class AiConfig:
    base_url: str
    model: str
    daily_candidates: int
    batch_size: int
    max_requests: int
    max_abstract_chars: int
    screening_max_tokens: int
    digest_max_tokens: int


@dataclass(frozen=True, slots=True)
class PublicationConfig:
    title: str
    base_url: str


@dataclass(frozen=True, slots=True)
class AppConfig:
    collection: CollectionConfig
    ai: AiConfig
    publication: PublicationConfig


def _positive(name: str, value: object) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def load_config(path: Path) -> AppConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    collection_raw = raw["collection"]
    ai_raw = raw["ai"]
    publication_raw = raw["publication"]
    collection = CollectionConfig(
        lookback_days=_positive("lookback_days", collection_raw["lookback_days"]),
        raw_feed_max_items=_positive("raw_feed_max_items", collection_raw["raw_feed_max_items"]),
        http_timeout_seconds=_positive("http_timeout_seconds", collection_raw["http_timeout_seconds"]),
        http_attempts=_positive("http_attempts", collection_raw["http_attempts"]),
    )
    ai = AiConfig(
        base_url=str(ai_raw["base_url"]).rstrip("/"),
        model=str(ai_raw["model"]),
        daily_candidates=_positive("daily_candidates", ai_raw["daily_candidates"]),
        batch_size=_positive("batch_size", ai_raw["batch_size"]),
        max_requests=_positive("max_requests", ai_raw["max_requests"]),
        max_abstract_chars=_positive("max_abstract_chars", ai_raw["max_abstract_chars"]),
        screening_max_tokens=_positive("screening_max_tokens", ai_raw["screening_max_tokens"]),
        digest_max_tokens=_positive("digest_max_tokens", ai_raw["digest_max_tokens"]),
    )
    if not ai.base_url.startswith("https://"):
        raise ValueError("AI base_url must use HTTPS")
    if math.ceil(ai.daily_candidates / ai.batch_size) + 1 > ai.max_requests:
        raise ValueError("max_requests must reserve one request for the digest")
    return AppConfig(
        collection=collection,
        ai=ai,
        publication=PublicationConfig(
            title=str(publication_raw["title"]),
            base_url=str(publication_raw["base_url"]).rstrip("/"),
        ),
    )
```

- [ ] **Step 5: Install and verify**

Run: `python -m pip install -e ".[dev]"`

Run: `python -m pytest tests/test_config.py -q`

Expected: `2 passed`.

- [ ] **Step 6: Commit**

```bash
git add .gitignore pyproject.toml paper_feed_config.json src/diamond_feed tests/test_config.py
git commit -m "build: scaffold diamond feed package"
```

---

### Task 2: Canonical paper model, normalization, high-recall filtering, and deduplication

**Files:**
- Create: `config/queries.json`
- Create: `src/diamond_feed/models.py`
- Create: `src/diamond_feed/normalize.py`
- Create: `src/diamond_feed/filtering.py`
- Create: `tests/conftest.py`
- Create: `tests/test_normalize_filter.py`

**Interfaces:**
- Produces: `PaperRecord.to_dict() -> dict[str, object]` and `PaperRecord.from_dict(data) -> PaperRecord`.
- Produces: `SourceFailure(timestamp, category, url, detail)` and `AiDecision(key, relevant, confidence, category, matched_topics, summary_zh, reason)`.
- Produces: `normalize_doi(value: str | None) -> str | None`.
- Produces: `record_key(record: PaperRecord) -> str`.
- Produces: `merge_records(left: PaperRecord, right: PaperRecord) -> PaperRecord`.
- Produces: `load_rules(path: Path) -> QueryRules` and `matches_rules(record: PaperRecord, rules: QueryRules) -> bool`.

- [ ] **Step 1: Add shared record and configuration fixtures**

`tests/conftest.py`:

```python
from datetime import datetime, timezone
from pathlib import Path

import pytest

from diamond_feed.config import load_config
from diamond_feed.filtering import load_rules
from diamond_feed.models import PaperRecord


@pytest.fixture
def app_config():
    return load_config(Path("paper_feed_config.json"))


@pytest.fixture
def ai_config(app_config):
    return app_config.ai


@pytest.fixture
def query_rules():
    return load_rules(Path("config/queries.json"))


@pytest.fixture
def diamond_records():
    return [
        PaperRecord(
            title="Piezoelectric effect in a polycrystalline diamond membrane",
            abstract="A flexible diamond membrane produces a stable voltage.",
            authors=["A. Author"],
            journal="Diamond Journal",
            published_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            doi="10.1000/diamond.1",
            url="https://doi.org/10.1000/diamond.1",
            sources=["rss"],
            source_ids=["rss:1"],
        ),
        PaperRecord(
            title="Boron-doped diamond electrode for electrochemistry",
            abstract="Electrochemical response of a boron-doped diamond electrode.",
            authors=["B. Author"],
            journal="Carbon Journal",
            published_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
            doi="10.1000/diamond.2",
            url="https://doi.org/10.1000/diamond.2",
            sources=["crossref"],
            source_ids=["crossref:2"],
        ),
    ]
```

- [ ] **Step 2: Write failing behavior tests**

```python
from datetime import datetime, timezone
from pathlib import Path

from diamond_feed.filtering import load_rules, matches_rules
from diamond_feed.models import PaperRecord
from diamond_feed.normalize import merge_records, normalize_doi, record_key


def paper(title: str, abstract: str = "", doi: str | None = None) -> PaperRecord:
    return PaperRecord(
        title=title,
        abstract=abstract,
        authors=["A. Author"],
        journal="Test Journal",
        published_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        doi=doi,
        url="https://example.test/paper",
        sources=["test"],
        source_ids=["id-1"],
    )


def test_doi_and_title_keys_are_canonical():
    assert normalize_doi("https://doi.org/10.1000/ABC.1") == "10.1000/abc.1"
    assert record_key(paper("A title", doi="doi:10.1000/ABC.1")) == "doi:10.1000/abc.1"


def test_merge_prefers_complete_metadata_and_keeps_provenance():
    merged = merge_records(
        paper("CVD diamond", doi="10.1/x"),
        PaperRecord(
            title="CVD diamond",
            abstract="A complete abstract",
            authors=["A. Author", "B. Author"],
            journal="Diamond Journal",
            published_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            doi="10.1/x",
            url="https://publisher.test/x",
            sources=["crossref"],
            source_ids=["cr-x"],
        ),
    )
    assert merged.abstract == "A complete abstract"
    assert merged.sources == ["test", "crossref"]


def test_rules_keep_material_research_and_drop_obvious_semantic_noise():
    rules = load_rules(Path("config/queries.json"))
    assert matches_rules(paper("Piezoelectric effect in polycrystalline diamond membranes"), rules)
    assert matches_rules(paper("Boron-doped diamond electrodes for electrochemistry"), rules)
    assert not matches_rules(paper("A diamond graph algorithm for network routing"), rules)
    assert not matches_rules(paper("Baseball diamond geometry for player tracking"), rules)
```

- [ ] **Step 3: Run the focused test**

Run: `python -m pytest tests/test_normalize_filter.py -q`

Expected: import failure for the new modules.

- [ ] **Step 4: Define the canonical record and AI fields**

`PaperRecord` must contain the fields shown in the test plus `categories: list[str]`, `summary_zh: str | None`, `ai_relevant: bool | None`, and `ai_confidence: float | None`. Serialize `published_at` as an ISO-8601 UTC string and reconstruct it with `datetime.fromisoformat`.

```python
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class PaperRecord:
    title: str
    abstract: str
    authors: list[str]
    journal: str
    published_at: datetime
    doi: str | None
    url: str
    sources: list[str]
    source_ids: list[str]
    categories: list[str] = field(default_factory=list)
    summary_zh: str | None = None
    ai_relevant: bool | None = None
    ai_confidence: float | None = None


@dataclass(frozen=True, slots=True)
class SourceFailure:
    timestamp: datetime
    category: str
    url: str
    detail: str


@dataclass(frozen=True, slots=True)
class AiDecision:
    key: str
    relevant: bool
    confidence: float
    category: str
    matched_topics: list[str]
    summary_zh: str
    reason: str
```

Implement `PaperRecord.to_dict` and `PaperRecord.from_dict` in this step using only JSON-native values; timestamps use `datetime.isoformat()` and `datetime.fromisoformat()`.

- [ ] **Step 5: Implement canonical keys and deterministic merging**

Use `unicodedata.normalize("NFKC", value)`, lowercase DOI values, remove `https://doi.org/`, `http://dx.doi.org/`, and `doi:` prefixes, collapse non-alphanumeric title runs to one space, and use `title:{normalized}:{year}` when DOI is absent. `merge_records` must preserve stable source order, prefer longer abstracts and author lists, prefer URLs containing `doi.org` or a publisher host over generic source URLs, and never mutate its inputs.

- [ ] **Step 6: Add exact high-recall and noise configuration**

`config/queries.json`:

```json
{
  "include_any": [
    "diamond", "diamonds", "nanodiamond", "nanodiamonds",
    "nano-diamond", "nanocrystalline diamond", "ultrananocrystalline diamond",
    "polycrystalline diamond", "single-crystal diamond", "single crystal diamond",
    "cvd diamond", "chemical vapor deposition diamond", "chemical vapour deposition diamond",
    "boron-doped diamond", "boron doped diamond", "diamond membrane", "diamond film",
    "diamond semiconductor", "diamond transistor", "diamond photonics", "diamond electrode",
    "nitrogen-vacancy center", "nitrogen vacancy centre", "nv center in diamond",
    "silicon-vacancy center", "germanium-vacancy center", "tin-vacancy center",
    "diamond color center", "diamond colour centre", "diamond-like carbon", "diamond like carbon"
  ],
  "material_context": [
    "carbon", "crystal", "film", "membrane", "substrate", "surface", "grain",
    "defect", "vacancy", "quantum", "semiconductor", "electrode", "coating",
    "growth", "cvd", "hpht", "thermal", "phonon", "optical", "sensor",
    "geology", "mineral", "inclusion", "mantle", "anvil"
  ],
  "obvious_noise": [
    "diamond graph", "diamond norm", "diamond operator", "diamond lemma",
    "baseball diamond", "softball diamond", "diamond league", "diamond jewelry",
    "diamond jewellery", "diamond ring market", "diamond film festival"
  ]
}
```

Implement `matches_rules` as: normalize concatenated title and abstract; require one `include_any`; reject `obvious_noise` only when no `material_context` occurs. This deliberately keeps ambiguous records for AI review.

- [ ] **Step 7: Run tests and commit**

Run: `python -m pytest tests/test_normalize_filter.py -q`

Expected: `4 passed`.

```bash
git add config/queries.json src/diamond_feed/models.py src/diamond_feed/normalize.py src/diamond_feed/filtering.py tests/conftest.py tests/test_normalize_filter.py
git commit -m "feat: add canonical paper filtering and dedupe"
```

---

### Task 3: Resilient RSS ingestion and failure taxonomy

**Files:**
- Create: `src/diamond_feed/http.py`
- Create: `src/diamond_feed/sources/__init__.py`
- Create: `src/diamond_feed/sources/rss.py`
- Create: `tests/fixtures/sample_feed.xml`
- Create: `tests/test_rss_source.py`

**Interfaces:**
- Consumes: `PaperRecord` from Task 2.
- Produces: `HttpResult(body: bytes, status: int, final_url: str)`.
- Produces: `SourceFailure(timestamp, category, url, detail)`.
- Produces: `fetch_bytes(url, timeout, attempts) -> HttpResult`.
- Produces: `parse_feed(body: bytes, source_url: str) -> list[PaperRecord]`.
- Produces: `collect_rss(url, fetcher) -> tuple[list[PaperRecord], SourceFailure | None]`.

- [ ] **Step 1: Add RSS fixtures and failing parser tests**

The fixture must contain two Atom entries: one with DOI `10.1000/diamond.1`, authors, summary, and an RFC-3339 timestamp; one without DOI using its entry ID. Tests must assert two records, UTC timestamps, preserved summary, source name `rss`, and a DOI extracted from either `prism_doi`, `dc_identifier`, or a DOI URL.

```python
from pathlib import Path

from diamond_feed.sources.rss import classify_failure, parse_feed


def test_parse_atom_feed_to_canonical_records():
    records = parse_feed(Path("tests/fixtures/sample_feed.xml").read_bytes(), "https://feed.test/rss")
    assert len(records) == 2
    assert records[0].doi == "10.1000/diamond.1"
    assert records[0].sources == ["rss"]
    assert records[0].published_at.tzinfo is not None


def test_failure_taxonomy_distinguishes_hard_and_soft_failures():
    assert classify_failure(404, None, False) == "http_404"
    assert classify_failure(410, None, False) == "http_410"
    assert classify_failure(200, None, True) == "empty_feed"
    assert classify_failure(None, TimeoutError(), False) == "timeout"
    assert classify_failure(200, ValueError("bad xml"), False) == "parse_error"
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/test_rss_source.py -q`

Expected: module or symbol import failure.

- [ ] **Step 3: Implement bounded HTTP behavior**

`fetch_bytes` must set a descriptive `User-Agent`, use `urllib.request` for normal URLs, and call `curl_cffi.requests.get(url, timeout=timeout, impersonate="chrome", allow_redirects=True)` for `www.mdpi.com`. Retry only timeouts, transient URL errors, HTTP 429, and HTTP 5xx. Sleep durations are `1`, `2`, then `4` seconds and never exceed the configured attempt count. Raise a typed `FetchError` containing status and category without including request headers.

- [ ] **Step 4: Implement feed parsing and classification**

Use `feedparser.parse(body)`. A bozo feed with zero entries is `parse_error`; a valid feed with zero entries is `empty_feed`. Extract the DOI from DOI-specific fields, identifiers, and DOI links, then call `normalize_doi`. Use the channel title as journal when an entry lacks a journal field. Missing publication dates use the current UTC time and add `missing_date` to the record categories.

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/test_rss_source.py -q`

Expected: `2 passed`.

```bash
git add src/diamond_feed/http.py src/diamond_feed/sources tests/fixtures/sample_feed.xml tests/test_rss_source.py
git commit -m "feat: ingest resilient journal feeds"
```

---

### Task 4: OpenAlex, Crossref, and arXiv adapters

**Files:**
- Create: `src/diamond_feed/sources/openalex.py`
- Create: `src/diamond_feed/sources/crossref.py`
- Create: `src/diamond_feed/sources/arxiv.py`
- Create: `tests/fixtures/openalex.json`
- Create: `tests/fixtures/crossref.json`
- Create: `tests/fixtures/arxiv.xml`
- Create: `tests/test_academic_sources.py`

**Interfaces:**
- Consumes: `PaperRecord`, `normalize_doi`, `fetch_bytes`.
- Produces in every module: `build_url(query: str, from_date: date, rows: int) -> str`.
- Produces in every module: `parse_response(body: bytes) -> list[PaperRecord]`.
- OpenAlex abstract reconstruction: `reconstruct_abstract(index: dict[str, list[int]] | None) -> str`.

- [ ] **Step 1: Write fixed response fixtures and failing adapter tests**

Each fixture contains two records, with one DOI duplicated across OpenAlex and Crossref. The arXiv fixture contains one diamond record with an arXiv ID but no DOI.

```python
from datetime import date
from pathlib import Path

from diamond_feed.sources import arxiv, crossref, openalex


def test_source_urls_have_date_window_and_encoded_query():
    for module in (openalex, crossref):
        url = module.build_url("boron-doped diamond", date(2026, 8, 1), 50)
        assert "diamond" in url
        assert "2026" in url
    arxiv_url = arxiv.build_url("boron-doped diamond", date(2026, 8, 1), 50)
    assert "diamond" in arxiv_url
    assert "20260801" in arxiv_url
    assert "max_results=50" in arxiv_url


def test_all_source_fixtures_become_records():
    oa = openalex.parse_response(Path("tests/fixtures/openalex.json").read_bytes())
    cr = crossref.parse_response(Path("tests/fixtures/crossref.json").read_bytes())
    ax = arxiv.parse_response(Path("tests/fixtures/arxiv.xml").read_bytes())
    assert oa[0].sources == ["openalex"]
    assert cr[0].sources == ["crossref"]
    assert ax[0].source_ids[0].startswith("arxiv:")
    assert oa[0].doi == cr[0].doi
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/test_academic_sources.py -q`

Expected: import failure for the adapter modules.

- [ ] **Step 3: Implement exact API requests**

- OpenAlex: `https://api.openalex.org/works?search={query}&filter=from_publication_date:{date}&per-page={rows}&select=id,doi,title,display_name,publication_date,authorships,primary_location,abstract_inverted_index`.
- Crossref: `https://api.crossref.org/works?query.bibliographic={query}&filter=from-pub-date:{date}&rows={rows}&select=DOI,title,abstract,author,container-title,published-online,published-print,URL`.
- arXiv: `https://export.arxiv.org/api/query` with an encoded search expression equivalent to `(all:{quoted_query}) AND submittedDate:[{from_date at 00:00} TO 300001010000]`, plus `start=0`, `max_results={rows}`, `sortBy=submittedDate`, and `sortOrder=descending`.

All URL construction uses `urllib.parse.urlencode` or `quote_plus`, never string interpolation of raw query text.

- [ ] **Step 4: Implement canonical response parsers**

Reconstruct OpenAlex abstracts by sorting the `(position, word)` pairs. Strip XML/HTML tags from Crossref abstracts. For arXiv, join author names, preserve the canonical entry URL, and use `arxiv:{id}` as the source ID. All publication times are timezone-aware UTC values.

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/test_academic_sources.py tests/test_normalize_filter.py -q`

Expected: all tests pass.

```bash
git add src/diamond_feed/sources tests/fixtures/openalex.json tests/fixtures/crossref.json tests/fixtures/arxiv.xml tests/test_academic_sources.py
git commit -m "feat: collect scholarly database records"
```

---

### Task 5: Atomic state, collection orchestration, and raw RSS publication

**Files:**
- Create: `src/diamond_feed/state.py`
- Create: `src/diamond_feed/render.py`
- Create: `src/diamond_feed/collect.py`
- Create: `tests/test_state_collect_render.py`

**Interfaces:**
- Consumes: all record/filter/source interfaces from Tasks 2–4.
- Produces: `FeedState(papers, pending_ai, source_watermarks)`.
- Produces: `load_state(path: Path) -> FeedState` and `save_state(path: Path, state: FeedState) -> None`.
- Produces: `merge_into_state(state, incoming, rules) -> CollectionStats`.
- Produces: `render_rss(records, title, link, limit) -> str`.
- CLI: `python -m diamond_feed.collect --config paper_feed_config.json --state state.json`.

- [ ] **Step 1: Write failing state and collection tests**

```python
from pathlib import Path

from diamond_feed.collect import merge_into_state
from diamond_feed.render import render_rss
from diamond_feed.state import FeedState, load_state, save_state


def test_atomic_state_round_trip_and_queue_dedup(tmp_path, diamond_records, query_rules):
    state = FeedState.empty()
    stats = merge_into_state(state, diamond_records + diamond_records, query_rules)
    assert stats.added == len(diamond_records)
    assert len(state.pending_ai) == len(diamond_records)
    path = tmp_path / "state.json"
    save_state(path, state)
    loaded = load_state(path)
    assert list(loaded.papers) == list(state.papers)
    assert not list(tmp_path.glob("*.tmp"))


def test_raw_rss_is_valid_and_bounded(diamond_records):
    xml = render_rss(diamond_records * 1001, "Diamond Paper Feed", "https://example.test", 2000)
    assert xml.count("<item>") == 2000
    assert "10.1000/diamond.1" in xml
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/test_state_collect_render.py -q`

Expected: import failure for state, collect, or render.

- [ ] **Step 3: Implement atomic state storage**

State JSON schema version is `1`:

```json
{
  "version": 1,
  "papers": {},
  "pending_ai": [],
  "source_watermarks": {}
}
```

Write to a sibling file ending in `.tmp`, call `flush()` and `os.fsync()`, then replace the destination with `os.replace()`. Reject unsupported versions instead of silently resetting.

- [ ] **Step 4: Implement merge and queue invariants**

For each incoming record, call `matches_rules`; calculate `record_key`; merge duplicates; append only new keys to `pending_ai`; and keep queue order oldest-first. A previously AI-processed record reappearing from another source updates metadata without returning to the queue unless its abstract changes from empty to non-empty.

- [ ] **Step 5: Render RSS 2.0 with Dublin Core metadata**

Use `xml.etree.ElementTree`; emit `title`, `link`, `description`, `guid`, `pubDate`, `dc:creator`, `dc:source`, and `dc:identifier` for DOI. Escape content through ElementTree rather than manual string replacement. Sort newest-first and apply the limit after sorting.

- [ ] **Step 6: Implement collection CLI**

The CLI reads `config/rss_sources.tsv` and `config/queries.json`, computes the date window from `source_watermarks` or the 30-day bootstrap default, invokes each adapter independently, merges successful records, writes `filtered_feed.xml`, writes `fetch_failures.tsv`, and saves state. If every source fails, return exit code `2` and do not replace `filtered_feed.xml` or `state.json`.

- [ ] **Step 7: Run tests and commit**

Run: `python -m pytest tests/test_state_collect_render.py -q`

Expected: `2 passed`.

```bash
git add src/diamond_feed/state.py src/diamond_feed/render.py src/diamond_feed/collect.py tests/test_state_collect_render.py
git commit -m "feat: persist collection state and raw feed"
```

---

### Task 6: DeepSeek client, strict decisions, and hard request budget

**Files:**
- Create: `src/diamond_feed/ai.py`
- Create: `tests/test_ai.py`

**Interfaces:**
- Consumes: `AiConfig`, `PaperRecord`, and `AiDecision`.
- Produces: `RequestBudget.consume() -> None`, `remaining: int`, and `used: int`.
- Produces: `DeepSeekClient.complete_json(messages, max_tokens, budget) -> object`.
- Produces: `screen_batch(records, client, config, budget) -> list[AiDecision]`.
- Produces: validated `AiDecision` instances from model JSON.

- [ ] **Step 1: Write failing tests for validation, truncation, and retries**

```python
import json

import pytest

from diamond_feed.ai import DeepSeekClient, RequestBudget, screen_batch


def test_screening_truncates_abstracts_and_validates_json(ai_config, diamond_records):
    seen = []

    def transport(url, headers, payload, timeout):
        seen.append((url, headers, payload))
        request_content = json.loads(payload["messages"][1]["content"])
        body = [{
            "key": request_content["papers"][0]["key"],
            "relevant": True,
            "confidence": 0.95,
            "category": "films-membranes",
            "matched_topics": ["diamond membrane"],
            "summary_zh": "研究了金刚石膜。",
            "reason": "研究对象是金刚石材料。"
        }]
        return {"choices": [{"message": {"content": json.dumps(body)}}], "usage": {"total_tokens": 120}}

    client = DeepSeekClient("secret-for-test-only", ai_config, transport=transport)
    decisions = screen_batch(diamond_records[:1], client, ai_config, RequestBudget(5))
    assert decisions[0].relevant is True
    request_content = json.loads(seen[0][2]["messages"][1]["content"])
    assert len(request_content["papers"][0]["abstract"]) <= 1200


def test_failed_attempts_count_against_hard_budget(ai_config, diamond_records):
    calls = 0

    def failing_transport(url, headers, payload, timeout):
        nonlocal calls
        calls += 1
        raise TimeoutError("timeout")

    budget = RequestBudget(5)
    client = DeepSeekClient("secret-for-test-only", ai_config, transport=failing_transport)
    with pytest.raises(RuntimeError, match="request budget exhausted"):
        for _ in range(6):
            screen_batch(diamond_records[:1], client, ai_config, budget)
    assert calls == 5
    assert budget.used == 5
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/test_ai.py -q`

Expected: module import failure.

- [ ] **Step 3: Implement a secret-safe DeepSeek transport**

Post to `{base_url}/chat/completions` with `Authorization: Bearer {key}`, JSON content type, configured model, `stream: false`, `temperature: 0.1`, and the requested `max_tokens`. The default transport uses `urllib.request`. Raised errors include only exception type, HTTP status, and attempt count; do not include headers, request bodies, response bodies, or the key.

- [ ] **Step 4: Implement request accounting before network I/O**

`RequestBudget.consume()` increments before the transport call. Once `used == maximum`, the next call raises `RuntimeError("request budget exhausted")`. Each retry calls `consume()` again. Screening may retry timeout, HTTP 429, and HTTP 5xx while budget remains; it does not retry authentication or insufficient-balance responses.

- [ ] **Step 5: Implement strict decision validation**

Accept exactly the category identifiers from the design. Require every requested record key exactly once; `relevant` must be Boolean; `confidence` must be within `[0, 1]`; topic and summary fields must have the declared types. Reject the whole batch on a missing key, duplicate key, unknown category, or non-JSON content so no partial batch is marked complete.

- [ ] **Step 6: Run tests and commit**

Run: `python -m pytest tests/test_ai.py -q`

Expected: `2 passed`.

```bash
git add src/diamond_feed/ai.py tests/test_ai.py
git commit -m "feat: add bounded DeepSeek screening"
```

---

### Task 7: Daily AI queue processing, digest RSS, HTML, and usage accounting

**Files:**
- Create: `src/diamond_feed/summarize.py`
- Modify: `tests/conftest.py`
- Create: `tests/test_summarize.py`

**Interfaces:**
- Consumes: state, rendering, configuration, DeepSeek interfaces.
- Produces: `run_summary(config, state_path, client, now) -> SummaryStats`.
- Produces files: `ai_summary_feed.xml`, `ai_summary.html`, `ai_usage.json`.
- CLI: `python -m diamond_feed.summarize --config paper_feed_config.json --state state.json`.

- [ ] **Step 1: Add deterministic queue and DeepSeek fixtures**

Append these fixtures to `tests/conftest.py`:

```python
import json

from diamond_feed.normalize import record_key
from diamond_feed.state import FeedState, save_state


class FakeDeepSeekClient:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.requests = 0

    def complete_json(self, messages, max_tokens, budget):
        budget.consume()
        self.requests += 1
        if self.fail:
            raise RuntimeError("simulated DeepSeek failure")
        request = json.loads(messages[-1]["content"])
        papers = request.get("papers")
        if papers is None:
            return {"html": "<section><h2>金刚石论文摘要</h2></section>"}
        return [
            {
                "key": item["key"],
                "relevant": True,
                "confidence": 0.95,
                "category": "other-diamond",
                "matched_topics": ["diamond"],
                "summary_zh": f"{item['title']} 的中文摘要。",
                "reason": "论文研究对象是金刚石。",
            }
            for item in papers
        ]


@pytest.fixture
def configured_state_with_100_pending(tmp_path):
    papers = {}
    pending = []
    for index in range(100):
        record = PaperRecord(
            title=f"Diamond research paper {index}",
            abstract="Diamond material research.",
            authors=["A. Author"],
            journal="Diamond Journal",
            published_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            doi=f"10.1000/bootstrap.{index}",
            url=f"https://doi.org/10.1000/bootstrap.{index}",
            sources=["openalex"],
            source_ids=[f"openalex:{index}"],
        )
        key = record_key(record)
        papers[key] = record
        pending.append(key)
    path = tmp_path / "state.json"
    save_state(path, FeedState(papers=papers, pending_ai=pending, source_watermarks={}))
    return path


@pytest.fixture
def fake_deepseek_client():
    return FakeDeepSeekClient()


@pytest.fixture
def failing_deepseek_client():
    return FakeDeepSeekClient(fail=True)
```

- [ ] **Step 2: Write failing end-to-end quota and recovery tests**

```python
from datetime import datetime, timezone

from diamond_feed.summarize import run_summary


def test_first_run_processes_at_most_40_and_uses_at_most_5_requests(
    tmp_path, configured_state_with_100_pending, fake_deepseek_client, app_config
):
    stats = run_summary(
        app_config,
        configured_state_with_100_pending,
        fake_deepseek_client,
        datetime(2026, 9, 6, tzinfo=timezone.utc),
        output_dir=tmp_path,
    )
    assert stats.candidates == 40
    assert stats.requests <= 5
    assert stats.remaining == 60
    assert (tmp_path / "ai_summary_feed.xml").exists()
    assert (tmp_path / "ai_summary.html").exists()
    assert (tmp_path / "ai_usage.json").exists()


def test_ai_failure_preserves_previous_outputs_and_queue(
    tmp_path, configured_state_with_100_pending, failing_deepseek_client, app_config
):
    old = "<html><body>previous digest</body></html>"
    (tmp_path / "ai_summary.html").write_text(old, encoding="utf-8")
    before = configured_state_with_100_pending.read_text(encoding="utf-8")
    stats = run_summary(
        app_config,
        configured_state_with_100_pending,
        failing_deepseek_client,
        datetime(2026, 9, 6, tzinfo=timezone.utc),
        output_dir=tmp_path,
    )
    assert stats.failed is True
    assert (tmp_path / "ai_summary.html").read_text(encoding="utf-8") == old
    assert configured_state_with_100_pending.read_text(encoding="utf-8") == before
```

- [ ] **Step 3: Verify failure**

Run: `python -m pytest tests/test_summarize.py -q`

Expected: import failure for summarize.

- [ ] **Step 4: Implement queue selection and request reservation**

Select the oldest 40 pending keys. Divide them into four batches of ten. Keep one of the five request attempts reserved for final digest generation. If a screening retry consumes the reserve, stop screening and generate deterministic HTML/RSS from successful decisions without another AI request; unprocessed batch keys stay queued.

- [ ] **Step 5: Apply decisions transactionally**

For a successfully validated batch, set `ai_relevant`, `ai_confidence`, category, and Chinese summary on each record and remove its key from `pending_ai`, including successfully judged irrelevant records. Do not save state until all output files have been written to temporary sibling files. Replace the outputs first and state last; on any exception delete temporary files and leave prior state and outputs untouched.

- [ ] **Step 6: Generate digest content and usage**

The optional fifth DeepSeek request receives only successfully selected titles, metadata, categories, and Chinese summaries; it returns an HTML body fragment. Wrap it in a fixed UTF-8 page with escaped metadata and grouped category headings. If the fifth call is unavailable or invalid, deterministic rendering groups the same records and summaries. `ai_usage.json` is an array of daily entries with `date`, `candidates`, `processed`, `selected`, `requests`, `prompt_tokens`, `completion_tokens`, and `total_tokens` only.

- [ ] **Step 7: Run tests and commit**

Run: `python -m pytest tests/test_summarize.py tests/test_ai.py -q`

Expected: all tests pass.

```bash
git add src/diamond_feed/summarize.py tests/conftest.py tests/test_summarize.py
git commit -m "feat: publish daily Chinese diamond digest"
```

---

### Task 8: Import, expand, and validate the RSS source registry

**Files:**
- Create: `scripts/import_rss_sources.py`
- Create: `scripts/validate_rss_sources.py`
- Create: `config/rss_sources.tsv`
- Create: `tests/test_source_tools.py`

**Interfaces:**
- Consumes local lists named in the approved design.
- Produces TSV columns: `name`, `category`, `url`.
- Produces validation TSV columns: `timestamp`, `category`, `url`, `detail`.

- [ ] **Step 1: Write failing import/deduplication tests**

```python
from pathlib import Path

from scripts.import_rss_sources import import_urls


def test_import_deduplicates_and_normalizes_known_publishers(tmp_path):
    source = tmp_path / "journals.dat"
    source.write_text(
        "https://www.mdpi.com/journal/materials/rss\n"
        "https://www.mdpi.com/rss/journal/materials\n"
        "https://journals.aps.org/prmaterials/rss\n",
        encoding="utf-8",
    )
    rows = import_urls([source])
    assert [row.url for row in rows] == [
        "https://www.mdpi.com/rss/journal/materials",
        "https://feeds.aps.org/rss/recent/prmaterials.xml",
    ]
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/test_source_tools.py -q`

Expected: import failure for the source import tool.

- [ ] **Step 3: Implement deterministic source import**

Read only lines beginning with `http://` or `https://`; normalize MDPI `/journal/{slug}/rss` to `/rss/journal/{slug}`; apply the known aliases `condmat -> condensedmatter` and `microwaves -> microwave`; replace APS `journals.aps.org/{code}/rss` with `feeds.aps.org/rss/recent/{code}.xml`; deduplicate by normalized URL while preserving first occurrence. The script accepts repeated `--input` paths and writes sorted TSV with `unknown` name/category until validation supplies the channel title.

- [ ] **Step 4: Import the local verified and semiconductor pools**

Run:

```powershell
python scripts/import_rss_sources.py `
  --input 'E:\Desktop\mcp\paper-feed\paper-feed\journals.dat' `
  --input 'E:\Desktop\mcp\paper-feed\paper\paper-search-semiconductor\期刊.txt' `
  --output config/rss_sources.tsv
```

Expected: a deduplicated registry containing the local active materials pool and additional semiconductor candidates.

- [ ] **Step 5: Add and verify diamond-focused coverage**

Ensure the registry covers these journal families, using official RSS when it survives a real GET and database queries when it does not:

- Diamond and Related Materials; Carbon; Carbon Trends; Journal of Carbon Research.
- Applied Surface Science; Surface and Coatings Technology; Thin Solid Films.
- Journal of Crystal Growth; Crystal Growth & Design; Plasma Processes and Polymers.
- Physical Review B, Physical Review Applied, Physical Review Materials, and Physical Review Research.
- Applied Physics Letters, Journal of Applied Physics, Journal of Physics D, and Semiconductor Science and Technology.
- Advanced Materials, Advanced Functional Materials, Advanced Science, Small, Nano Letters, ACS Nano, ACS Applied Materials & Interfaces.
- Nature, Nature Materials, Nature Physics, Nature Photonics, Nature Electronics, Nature Communications, Communications Materials, Science, and Science Advances.
- Sensors, Biosensors, Nanomaterials, Materials, Coatings, Crystals, Micromachines, Photonics, and Quantum Science and Technology equivalents.
- High Pressure Research, Physics of the Earth and Planetary Interiors, American Mineralogist, and Earth and Planetary Science Letters for natural-diamond coverage.

Run a real GET for every new URL. Do not add guessed publisher endpoints. If no official RSS works, record the journal in `README.md` under database-only coverage instead of adding a dead URL.

- [ ] **Step 6: Validate the full registry and classify failures**

Run:

```powershell
python scripts/validate_rss_sources.py --sources config/rss_sources.tsv --failures fetch_failures.tsv
```

Expected: zero stable 404/410 or stable parse errors in the committed active registry. Soft failures remain listed in `fetch_failures.tsv` and do not cause removal.

- [ ] **Step 7: Run tests and commit**

Run: `python -m pytest tests/test_source_tools.py tests/test_rss_source.py -q`

Expected: all tests pass.

```bash
git add scripts config/rss_sources.tsv fetch_failures.tsv tests/test_source_tools.py
git commit -m "data: curate diamond literature sources"
```

---

### Task 9: GitHub Actions collection and summarization workflows

**Files:**
- Create: `.github/workflows/collect.yml`
- Create: `.github/workflows/summarize.yml`
- Create: `tests/test_workflows.py`

**Interfaces:**
- Collection command: `python -m diamond_feed.collect --config paper_feed_config.json --state state.json`.
- Summary command: `python -m diamond_feed.summarize --config paper_feed_config.json --state state.json`.
- Secret consumed only by summary workflow: `DEEPSEEK_API_KEY`.

- [ ] **Step 1: Write failing workflow contract tests**

```python
from pathlib import Path


def test_only_summary_workflow_receives_deepseek_secret():
    collect = Path(".github/workflows/collect.yml").read_text(encoding="utf-8")
    summarize = Path(".github/workflows/summarize.yml").read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY" not in collect
    assert "DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}" in summarize
    assert "0 */6 * * *" in collect
    assert "0 0 * * *" in summarize
    assert "contents: write" in collect
    assert "contents: write" in summarize
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/test_workflows.py -q`

Expected: `FileNotFoundError` for workflow files.

- [ ] **Step 3: Create the collection workflow**

`collect.yml` must use `actions/checkout@v6` with full history, `actions/setup-python@v6` with Python 3.11, install `.[dev]`, run tests before collection, execute the collection command, stage only `filtered_feed.xml`, `state.json`, and `fetch_failures.tsv`, then commit only when the index changed. Before pushing, run `git pull --rebase origin "${GITHUB_REF_NAME}"`. Set `permissions.contents: write` and concurrency group `diamond-paper-feed-${{ github.ref }}` with `cancel-in-progress: false`.

- [ ] **Step 4: Create the summary workflow**

`summarize.yml` uses the same checkout, Python, test, commit, rebase, permission, and concurrency behavior. Its schedule is `0 0 * * *`, corresponding to 08:00 Asia/Shanghai. Its run step contains exactly:

```yaml
      - name: Generate bounded DeepSeek digest
        env:
          DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
        run: python -m diamond_feed.summarize --config paper_feed_config.json --state state.json
```

Stage only `ai_summary_feed.xml`, `ai_summary.html`, `ai_usage.json`, and `state.json`.

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/test_workflows.py -q`

Expected: `1 passed`.

```bash
git add .github/workflows tests/test_workflows.py
git commit -m "ci: automate collection and DeepSeek digest"
```

---

### Task 10: Documentation, offline acceptance suite, and network smoke test

**Files:**
- Create: `README.md`
- Create: `tests/test_acceptance.py`
- Modify: `paper_feed_config.json`
- Modify: `config/rss_sources.tsv`

**Interfaces:**
- User-facing URLs: `filtered_feed.xml`, `ai_summary_feed.xml`, and `ai_summary.html` under the repository's GitHub Pages base URL.
- User action: create repository `Llxyyds666/diamond-paper-feed`, add `DEEPSEEK_API_KEY` as an Actions Secret, and enable Pages from `main` root.

- [ ] **Step 1: Write the offline acceptance test**

```python
from pathlib import Path
from xml.etree import ElementTree


def test_repository_outputs_and_security_contract():
    for name in ("filtered_feed.xml", "ai_summary_feed.xml"):
        path = Path(name)
        if path.exists():
            ElementTree.parse(path)
    scan_paths = [Path("src"), Path("config"), Path(".github")]
    scan_paths.extend([Path("README.md"), Path("paper_feed_config.json")])
    tracked_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for root in scan_paths
        for path in ([root] if root.is_file() else root.rglob("*"))
        if path.is_file() and path.stat().st_size < 2_000_000
    )
    assert "DEEPSEEK_API_KEY" + "=" not in tracked_text
    assert "Authorization: Bearer " + "sk-" not in tracked_text
    assert "deepseek-v4-flash-vision-exp" in Path("paper_feed_config.json").read_text(encoding="utf-8")
```

- [ ] **Step 2: Document exact deployment and recovery actions**

README sections must be: overview; covered diamond categories; source architecture; local setup; configuration; DeepSeek Secret setup; manual workflow runs; GitHub Pages and Zotero URLs; AI hard limits; failure report meanings; adding sources; state recovery; security. State explicitly that a large first harvest is queued and only 40 candidates/day are submitted using at most 5 request attempts.

- [ ] **Step 3: Run the complete offline suite**

Run: `python -m pytest -q`

Expected: every test passes with no network access and no API key.

- [ ] **Step 4: Run collection smoke test with no AI credential**

Run: `python -m diamond_feed.collect --config paper_feed_config.json --state state.json`

Expected: at least one RSS or database adapter succeeds; `filtered_feed.xml`, `state.json`, and `fetch_failures.tsv` are valid; no AI request occurs.

- [ ] **Step 5: Run security and repository hygiene checks**

Run: `git ls-files | rg '(^|/)(vendor|__pycache__)/|\.env$'`

Expected: no output.

Run: `rg -n 'sk-[A-Za-z0-9_-]{10,}|Authorization: Bearer sk-' src config .github README.md paper_feed_config.json`

Expected: no output.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 6: Commit documentation and verified initial outputs**

```bash
git add README.md tests/test_acceptance.py paper_feed_config.json config/rss_sources.tsv filtered_feed.xml state.json fetch_failures.tsv
git commit -m "docs: complete diamond feed deployment guide"
```

---

### Task 11: Publish the new GitHub repository and perform a bounded live AI run

**Files:**
- Modify after live run: `ai_summary_feed.xml`
- Modify after live run: `ai_summary.html`
- Modify after live run: `ai_usage.json`
- Modify after live run: `state.json`

**Interfaces:**
- GitHub repository: `Llxyyds666/diamond-paper-feed`.
- GitHub Actions Secret: `DEEPSEEK_API_KEY`.
- GitHub Pages base: `https://llxyyds666.github.io/diamond-paper-feed`.

- [ ] **Step 1: Have the user create the empty public repository**

The GitHub connector available in this environment can update repositories but cannot create repositories or manage Actions Secrets. Ask the user to create `Llxyyds666/diamond-paper-feed` with no generated README, license, or `.gitignore`.

- [ ] **Step 2: Publish the exact verified local Git state**

After the repository exists, publish the local `main` tree through the authenticated GitHub connector or an authenticated `git push`. Confirm the remote head SHA equals local `git rev-parse HEAD` before enabling scheduled workflows.

- [ ] **Step 3: Have the user add the Secret and enable Pages**

The user adds `DEEPSEEK_API_KEY` at `Settings -> Secrets and variables -> Actions`, then enables Pages with `Deploy from a branch`, branch `main`, directory `/(root)`. Do not ask the user to paste the key into chat or a terminal command.

- [ ] **Step 4: Trigger one collection workflow and inspect artifacts**

Expected: workflow succeeds, request logs contain no credential, raw feed contains no more than 2000 items, and any RSS failures are classified in `fetch_failures.tsv`.

- [ ] **Step 5: Trigger one summary workflow and verify hard limits**

Expected: `ai_usage.json` reports `candidates <= 40` and `requests <= 5`; XML files parse; HTML renders; remaining queue length is preserved in `state.json`.

- [ ] **Step 6: Verify public endpoints**

Open:

- `https://llxyyds666.github.io/diamond-paper-feed/filtered_feed.xml`
- `https://llxyyds666.github.io/diamond-paper-feed/ai_summary_feed.xml`
- `https://llxyyds666.github.io/diamond-paper-feed/ai_summary.html`

Expected: HTTP 200 after Pages deployment completes, with the two XML documents parseable by Zotero.

- [ ] **Step 7: Commit live-generated state if the workflow did not already commit it**

```bash
git add ai_summary_feed.xml ai_summary.html ai_usage.json state.json
git commit -m "chore: publish initial diamond digest"
```
