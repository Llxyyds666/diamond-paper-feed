import csv
from datetime import datetime, timezone
import io
from pathlib import Path

import pytest

from diamond_feed.http import FetchError, HttpResult
from scripts.import_rss_sources import SourceRow, import_urls, write_sources
from scripts.validate_rss_sources import validate_registry


ATOM = b"""<?xml version='1.0'?>
<feed xmlns='http://www.w3.org/2005/Atom'>
  <title>Diamond\tJournal\nLatest</title>
  <entry><id>one</id><title>Paper</title><updated>2026-01-01T00:00:00Z</updated></entry>
</feed>"""


def _read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


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


def test_import_reads_only_url_lines_and_applies_slug_aliases(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text(
        "Journal title\n"
        " https://ignored.example/rss\n"
        "https://www.mdpi.com/journal/condmat/rss\n"
        "https://www.mdpi.com/rss/journal/microwaves/\n",
        encoding="utf-8",
    )

    assert import_urls([source]) == [
        SourceRow("unknown", "unknown", "https://www.mdpi.com/rss/journal/condensedmatter"),
        SourceRow("unknown", "unknown", "https://www.mdpi.com/rss/journal/microwave"),
    ]


def test_source_writer_has_exact_columns_and_stable_sorting(tmp_path):
    output = tmp_path / "sources.tsv"

    write_sources(
        output,
        [
            SourceRow("Z", "journal", "https://z.test/rss"),
            SourceRow("A", "journal", "https://a.test/rss"),
        ],
    )

    assert output.read_text(encoding="utf-8") == (
        "name\tcategory\turl\n"
        "A\tjournal\thttps://a.test/rss\n"
        "Z\tjournal\thttps://z.test/rss\n"
    )


def test_validation_enriches_title_removes_hard_and_keeps_soft_failures(tmp_path):
    sources = tmp_path / "sources.tsv"
    failures = tmp_path / "failures.tsv"
    write_sources(
        sources,
        [
            SourceRow("unknown", "unknown", "https://good.test/rss"),
            SourceRow("Dead", "journal", "https://dead.test/rss"),
            SourceRow("Slow", "journal", "https://slow.test/rss"),
        ],
    )

    def fetcher(url: str) -> HttpResult:
        if url == "https://good.test/rss":
            return HttpResult(ATOM, 200, url)
        if url == "https://dead.test/rss":
            raise FetchError(status=404, category="http_404", detail="HTTP 404")
        raise FetchError(status=None, category="timeout", detail="request\ttimed out\nretry")

    summary = validate_registry(
        sources,
        failures,
        fetcher=fetcher,
        now=lambda: datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    assert summary.total == 3
    assert summary.active == 2
    assert summary.hard == 1
    assert summary.soft == 1
    assert _read_tsv(sources) == [
        {"name": "Diamond Journal Latest", "category": "unknown", "url": "https://good.test/rss"},
        {"name": "Slow", "category": "journal", "url": "https://slow.test/rss"},
    ]
    assert _read_tsv(failures) == [
        {
            "timestamp": "2026-01-02T00:00:00+00:00",
            "category": "http_404",
            "url": "https://dead.test/rss",
            "detail": "HTTP 404",
        },
        {
            "timestamp": "2026-01-02T00:00:00+00:00",
            "category": "timeout",
            "url": "https://slow.test/rss",
            "detail": "request timed out retry",
        },
    ]


def test_validation_treats_malformed_feed_as_hard(tmp_path):
    sources = tmp_path / "sources.tsv"
    failures = tmp_path / "failures.tsv"
    write_sources(sources, [SourceRow("Bad", "journal", "https://bad.test/rss")])

    summary = validate_registry(
        sources,
        failures,
        fetcher=lambda url: HttpResult(b"not xml", 200, url),
    )

    assert summary.hard == 1
    assert _read_tsv(sources) == []
    assert _read_tsv(failures)[0]["category"] == "parse_error"


def test_validation_treats_recoverable_malformed_feed_as_hard(tmp_path):
    sources = tmp_path / "sources.tsv"
    failures = tmp_path / "failures.tsv"
    write_sources(sources, [SourceRow("Broken", "journal", "https://broken.test/rss")])
    malformed_with_entry = b"""<?xml version='1.0'?>
<rss version='2.0'><channel><title>Broken but readable</title>
<item><title>Recovered paper</title><link>https://broken.test/paper</link></item>
</channel>"""

    summary = validate_registry(
        sources,
        failures,
        fetcher=lambda url: HttpResult(malformed_with_entry, 200, url),
    )

    assert summary.hard == 1
    assert _read_tsv(sources) == []
    assert _read_tsv(failures)[0]["category"] == "parse_error"


def test_validation_requires_hard_failure_to_repeat(tmp_path):
    sources = tmp_path / "sources.tsv"
    failures = tmp_path / "failures.tsv"
    write_sources(sources, [SourceRow("Flaky", "journal", "https://flaky.test/rss")])
    outcomes = iter(
        [
            FetchError(status=404, category="http_404", detail="HTTP 404"),
            FetchError(status=None, category="timeout", detail="timeout"),
        ]
    )

    def fetcher(url):
        raise next(outcomes)

    summary = validate_registry(sources, failures, fetcher=fetcher)

    assert summary.hard == 0
    assert summary.soft == 1
    assert _read_tsv(sources)[0]["url"] == "https://flaky.test/rss"
    assert _read_tsv(failures)[0]["category"] == "timeout"


def test_source_reader_rejects_wrong_schema(tmp_path):
    sources = tmp_path / "sources.tsv"
    failures = tmp_path / "failures.tsv"
    sources.write_text("url\tname\nhttps://feed.test/rss\tFeed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="exact columns"):
        validate_registry(sources, failures, fetcher=lambda url: HttpResult(ATOM, 200, url))
