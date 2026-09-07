from datetime import date, timezone
import json
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus, urlparse

import pytest

from diamond_feed.sources import arxiv, crossref, openalex


FIXTURES = Path(__file__).parent / "fixtures"


def test_source_urls_have_date_window_encoded_query_rows_and_initial_cursor():
    query = "boron-doped diamond & sensors"
    for module in (openalex, crossref):
        url = module.build_url(query, date(2026, 8, 1), 50)
        parsed = parse_qs(urlparse(url).query)
        assert "diamond" in unquote_plus(url)
        assert parsed.get("per-page", parsed.get("rows")) == ["50"]
        assert parsed["cursor"] == ["*"]
        assert "2026-08-01" in unquote_plus(url)
        assert "%26" in url

    arxiv_url = arxiv.build_url(query, date(2026, 8, 1), 50)
    arxiv_query = parse_qs(urlparse(arxiv_url).query)
    assert "diamond" in unquote_plus(arxiv_url)
    assert "20260801" in unquote_plus(arxiv_url)
    assert arxiv_query["max_results"] == ["50"]
    assert "%26" in arxiv_url


def test_json_source_page_sizes_are_capped_at_public_api_limits():
    openalex_query = parse_qs(
        urlparse(openalex.build_url("diamond", date(2026, 8, 1), 2000)).query
    )
    crossref_query = parse_qs(
        urlparse(crossref.build_url("diamond", date(2026, 8, 1), 2000)).query
    )

    assert openalex_query["per-page"] == ["200"]
    assert crossref_query["rows"] == ["1000"]


def test_json_source_urls_accept_opaque_continuation_cursors():
    cursor = "opaque cursor/+?=Unicode-游标"

    for module in (openalex, crossref):
        parsed = parse_qs(
            urlparse(module.build_url("diamond", date(2026, 8, 1), 10, cursor)).query
        )
        assert parsed["cursor"] == [cursor]


def test_json_page_parsers_return_records_and_next_cursor_without_breaking_parse_response():
    openalex_payload = json.loads((FIXTURES / "openalex.json").read_bytes())
    openalex_payload["meta"] = {"count": 400, "next_cursor": "oa-next"}
    openalex_body = json.dumps(openalex_payload).encode()
    crossref_payload = json.loads((FIXTURES / "crossref.json").read_bytes())
    crossref_payload["message"]["next-cursor"] = "cr-next"
    crossref_body = json.dumps(crossref_payload).encode()

    oa_page = openalex.parse_page(openalex_body)
    cr_page = crossref.parse_page(crossref_body)

    assert oa_page.records == openalex.parse_response(openalex_body)
    assert oa_page.next_cursor == "oa-next"
    assert cr_page.records == crossref.parse_response(crossref_body)
    assert cr_page.next_cursor == "cr-next"


def test_openalex_reconstructs_inverted_abstract_stably_and_handles_none():
    assert openalex.reconstruct_abstract({"third": [2], "first": [0], "second": [1]}) == "first second third"
    assert openalex.reconstruct_abstract(None) == ""


@pytest.mark.parametrize(
    "invalid_index",
    [
        {1: [0]},
        {"diamond": "not-a-list"},
        {"diamond": [True]},
        {"diamond": [1.0]},
        {"diamond": [-1]},
    ],
)
def test_openalex_rejects_malformed_inverted_abstract(invalid_index):
    with pytest.raises(ValueError):
        openalex.reconstruct_abstract(invalid_index)


def test_openalex_skips_bad_index_without_losing_neighboring_record():
    payload = json.loads((FIXTURES / "openalex.json").read_bytes())
    malformed = dict(payload["results"][0])
    malformed["id"] = "https://openalex.org/W-malformed"
    malformed["abstract_inverted_index"] = {"diamond": "not-a-list"}
    payload["results"] = [payload["results"][0], malformed]

    records = openalex.parse_response(json.dumps(payload).encode())

    assert [record.source_ids for record in records] == [["https://openalex.org/W123"]]


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


@pytest.mark.parametrize(
    ("date_parts", "expected"),
    [
        ([2026], date(2026, 1, 1)),
        ([2026, 8], date(2026, 8, 1)),
    ],
)
def test_crossref_partial_dates_default_missing_parts_to_one(date_parts, expected):
    payload = json.loads((FIXTURES / "crossref.json").read_bytes())
    item = payload["message"]["items"][0]
    item["published-online"] = {"date-parts": [date_parts]}

    records = crossref.parse_response(json.dumps(payload).encode())

    assert records[0].published_at.date() == expected
    assert records[0].published_at.tzinfo == timezone.utc


def test_crossref_skips_record_without_a_valid_date_and_keeps_neighbor():
    payload = json.loads((FIXTURES / "crossref.json").read_bytes())
    malformed = dict(payload["message"]["items"][0])
    malformed["DOI"] = "10.1000/invalid-date"
    malformed["published-online"] = {"date-parts": [[2026, 13]]}
    malformed.pop("published-print", None)
    payload["message"]["items"] = [payload["message"]["items"][0], malformed]

    records = crossref.parse_response(json.dumps(payload).encode())

    assert [record.doi for record in records] == ["10.1000/diamond.1"]


def test_arxiv_fixture_uses_utc_canonical_url_and_id_and_survives_bad_entry():
    records = arxiv.parse_response((FIXTURES / "arxiv.xml").read_bytes())

    assert len(records) == 2
    assert records[0].source_ids == ["arxiv:2608.01234"]
    assert records[0].url == "https://arxiv.org/abs/2608.01234"
    assert records[0].doi is None
    assert records[0].published_at.tzinfo == timezone.utc
    assert records[0].authors == ["Ada Lovelace", "Grace Hopper"]


@pytest.mark.parametrize(
    ("parser", "body"),
    [
        (openalex.parse_response, b"{}"),
        (openalex.parse_response, b'{"results": {}}'),
        (crossref.parse_response, b"{}"),
        (crossref.parse_response, b'{"message": []}'),
        (crossref.parse_response, b'{"message": {"items": {}}}'),
    ],
)
def test_json_sources_reject_malformed_batch_envelopes(parser, body):
    with pytest.raises(ValueError):
        parser(body)


@pytest.mark.parametrize(
    ("parser", "body"),
    [
        (openalex.parse_response, b'{"results": []}'),
        (crossref.parse_response, b'{"message": {"items": []}}'),
    ],
)
def test_json_sources_accept_valid_empty_batches(parser, body):
    assert parser(body) == []
