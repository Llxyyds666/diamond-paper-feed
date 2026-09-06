from datetime import date, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus, urlparse

from diamond_feed.sources import arxiv, crossref, openalex


FIXTURES = Path("tests/fixtures")


def test_source_urls_have_date_window_encoded_query_and_rows():
    query = "boron-doped diamond & sensors"
    for module in (openalex, crossref):
        url = module.build_url(query, date(2026, 8, 1), 50)
        parsed = parse_qs(urlparse(url).query)
        assert "diamond" in unquote_plus(url)
        assert parsed.get("per-page", parsed.get("rows")) == ["50"]
        assert "2026-08-01" in unquote_plus(url)
        assert "%26" in url

    arxiv_url = arxiv.build_url(query, date(2026, 8, 1), 50)
    arxiv_query = parse_qs(urlparse(arxiv_url).query)
    assert "diamond" in unquote_plus(arxiv_url)
    assert "20260801" in unquote_plus(arxiv_url)
    assert arxiv_query["max_results"] == ["50"]
    assert "%26" in arxiv_url


def test_openalex_reconstructs_inverted_abstract_stably_and_handles_none():
    assert openalex.reconstruct_abstract({"third": [2], "first": [0], "second": [1]}) == "first second third"
    assert openalex.reconstruct_abstract(None) == ""


def test_openalex_fixture_becomes_records_and_skips_malformed_item():
    records = openalex.parse_response((FIXTURES / "openalex.json").read_bytes())

    assert len(records) == 2
    assert records[0].sources == ["openalex"]
    assert records[0].doi == "10.1000/diamond.1"
    assert records[0].abstract == "Boron-doped diamond work electrodes"
    assert records[1].journal == ""
    assert records[1].published_at.tzinfo == timezone.utc


def test_crossref_fixture_strips_tags_uses_online_date_and_print_fallback():
    records = crossref.parse_response((FIXTURES / "crossref.json").read_bytes())

    assert len(records) == 2
    assert records[0].sources == ["crossref"]
    assert records[0].abstract == "Diamond electrode performance."
    assert records[0].published_at.date() == date(2026, 8, 16)
    assert records[1].published_at.date() == date(2026, 8, 10)
    assert records[0].doi == "10.1000/diamond.1"


def test_arxiv_fixture_uses_utc_canonical_url_and_id_and_survives_bad_entry():
    records = arxiv.parse_response((FIXTURES / "arxiv.xml").read_bytes())

    assert len(records) == 2
    assert records[0].source_ids == ["arxiv:2608.01234"]
    assert records[0].url == "https://arxiv.org/abs/2608.01234"
    assert records[0].doi is None
    assert records[0].published_at.tzinfo == timezone.utc
    assert records[0].authors == ["Ada Lovelace", "Grace Hopper"]
