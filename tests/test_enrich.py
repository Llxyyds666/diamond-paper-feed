from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from urllib.parse import parse_qs, urlparse

import pytest

from diamond_feed.enrich import AbstractEnricher, MetadataResponse
from diamond_feed.models import PaperRecord
from diamond_feed.normalize import record_key
from diamond_feed.state import FeedState
from diamond_feed import enrich


def paper(index: int, *, abstract: str = "", processed: bool = False) -> PaperRecord:
    return PaperRecord(
        title=f"Diamond detector device {index}",
        abstract=abstract,
        authors=["A. Author"],
        journal="Diamond Devices",
        published_at=datetime(2026, 9, index % 28 + 1, tzinfo=timezone.utc),
        doi=f"10.1000/device.{index}",
        url=f"https://doi.org/10.1000/device.{index}",
        sources=["crossref"],
        source_ids=[f"crossref:{index}"],
        categories=["electronics-optoelectronics"] if processed else [],
        summary_zh="旧摘要" if processed else None,
        ai_relevant=True if processed else None,
        ai_confidence=0.8 if processed else None,
    )


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def semantic_response(records):
    return MetadataResponse(
        200,
        json.dumps(records).encode("utf-8"),
    )


def semantic_record(item: PaperRecord, abstract: str, *, title: str | None = None):
    return {
        "paperId": "s2",
        "title": title or item.title,
        "abstract": abstract,
        "externalIds": {"DOI": item.doi},
    }


def openaire_response(records):
    return MetadataResponse(
        200,
        json.dumps({"header": {}, "results": records}).encode("utf-8"),
    )


def openaire_record(item: PaperRecord, descriptions):
    return {
        "title": item.title,
        "pids": [{"scheme": "doi", "value": item.doi}],
        "descriptions": descriptions,
    }


def test_semantic_scholar_batches_at_500_prioritizes_pending_and_requeues_processed():
    records = [paper(index, processed=index == 501) for index in range(502)]
    state = FeedState(
        papers={record_key(item): item for item in records},
        pending_ai=[record_key(records[501])],
    )
    chosen = records[501]
    transport = RecordingTransport([
        semantic_response([
            {
                "paperId": "s2",
                "title": chosen.title,
                "abstract": "A complete original abstract for a diamond detector device.",
                "externalIds": {"DOI": chosen.doi},
            }
        ]),
        MetadataResponse(200, b'{"header":{},"results":[]}'),
    ])

    stats = AbstractEnricher("secret-value", transport=transport).enrich(
        state, [record_key(chosen)]
    )

    posted = json.loads(transport.calls[0][3])
    assert len(posted["ids"]) == 500
    assert posted["ids"][0] == "DOI:10.1000/device.501"
    assert transport.calls[0][2]["x-api-key"] == "secret-value"
    assert state.papers[record_key(chosen)].abstract.startswith("A complete")
    assert state.pending_ai.count(record_key(chosen)) == 1
    assert stats.enriched == 1


def test_openaire_queries_only_semantic_misses_in_groups_of_five():
    records = [paper(index) for index in range(6)]
    state = FeedState(
        papers={record_key(item): item for item in records},
        pending_ai=[record_key(item) for item in records],
    )
    semantic = semantic_response([])
    openaire_one = MetadataResponse(200, b'{"header":{},"results":[]}')
    openaire_two = MetadataResponse(200, b'{"header":{},"results":[]}')
    transport = RecordingTransport([semantic, openaire_one, openaire_two])

    stats = AbstractEnricher("key", transport=transport).enrich(
        state, list(state.pending_ai)
    )

    assert [call[0] for call in transport.calls] == ["POST", "GET", "GET"]
    assert all("api.openaire.eu/graph/v3/research-products" in call[1]
               for call in transport.calls[1:])
    queries = [parse_qs(urlparse(call[1]).query) for call in transport.calls[1:]]
    assert [query["pageSize"] for query in queries] == [["100"], ["100"]]
    assert queries[0]["pid"] == [
        '"10.1000/device.0" OR "10.1000/device.1" OR "10.1000/device.2" '
        'OR "10.1000/device.3" OR "10.1000/device.4"'
    ]
    assert queries[1]["pid"] == ['"10.1000/device.5"']
    assert stats.semantic_scholar_queried == 6
    assert stats.openaire_queried == 6


@pytest.mark.parametrize(
    ("doi", "title", "abstract"),
    [
        ("10.1000/wrong", "Diamond detector device 1", "A real-looking abstract."),
        ("10.1000/device.1", "Unrelated silicon battery", "A real-looking abstract."),
        ("10.1000/device.1", "Diamond detector device 1", "Diamond detector device 1"),
        ("10.1000/device.1", "Diamond detector device 1", "No abstract available."),
        ("10.1000/device.1", "Diamond detector device 1", "x" * 30001),
    ],
)
def test_invalid_enrichment_is_rejected(doi, title, abstract):
    item = paper(1)
    state = FeedState(papers={record_key(item): item}, pending_ai=[record_key(item)])
    transport = RecordingTransport([
        semantic_response([{
            "paperId": "s2",
            "title": title,
            "abstract": abstract,
            "externalIds": {"DOI": doi},
        }]),
        MetadataResponse(200, b'{"header":{},"results":[]}'),
    ])

    AbstractEnricher("key", transport=transport).enrich(state, [record_key(item)])

    assert state.papers[record_key(item)].abstract == ""


def test_provider_failure_is_sanitized_and_falls_through(capsys):
    item = paper(2)
    state = FeedState(papers={record_key(item): item}, pending_ai=[record_key(item)])
    transport = RecordingTransport([
        RuntimeError("secret-value and response body"),
        MetadataResponse(503, b'secret-value and response body'),
    ])

    stats = AbstractEnricher("secret-value", transport=transport).enrich(
        state, [record_key(item)]
    )

    captured = capsys.readouterr()
    assert stats.enriched == 0
    assert "secret-value" not in captured.err
    assert "response body" not in captured.err
    assert "semantic-scholar RuntimeError none" in captured.err
    assert "openaire HTTPError 503" in captured.err
    assert state.papers[record_key(item)].abstract == ""


def test_default_semantic_transport_rejects_redirect_without_leaking_api_key(
    monkeypatch, capsys
):
    source_requests = []
    target_requests = []

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            source_requests.append((self.path, dict(self.headers), self.rfile.read(length)))
            self.send_response(302)
            self.send_header("Location", target_url)
            self.end_headers()
            self.wfile.write(b"redirect response body")

        def log_message(self, format, *args):
            pass

    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            target_requests.append((self.path, dict(self.headers), b""))
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            length = int(self.headers["Content-Length"])
            target_requests.append((self.path, dict(self.headers), self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    class OpenAIREHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"header":{},"results":[]}')

        def log_message(self, format, *args):
            pass

    target_server = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    target_thread = Thread(target=target_server.serve_forever, daemon=True)
    target_thread.start()
    target_host, target_port = target_server.server_address
    target_url = f"http://{target_host}:{target_port}/redirect-target"

    source_server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    source_thread = Thread(target=source_server.serve_forever, daemon=True)
    source_thread.start()
    openaire_server = ThreadingHTTPServer(("127.0.0.1", 0), OpenAIREHandler)
    openaire_thread = Thread(target=openaire_server.serve_forever, daemon=True)
    openaire_thread.start()
    try:
        source_host, source_port = source_server.server_address
        openaire_host, openaire_port = openaire_server.server_address
        monkeypatch.setattr(enrich, "SEMANTIC_BATCH_URL", f"http://{source_host}:{source_port}/semantic")
        monkeypatch.setattr(enrich, "OPENAIRE_URL", f"http://{openaire_host}:{openaire_port}/openaire")
        item = paper(3)
        key = record_key(item)
        state = FeedState(papers={key: item}, pending_ai=[key])

        stats = AbstractEnricher("semantic-secret", timeout_seconds=2.5).enrich(state, [key])
    finally:
        for server, thread in (
            (source_server, source_thread),
            (openaire_server, openaire_thread),
            (target_server, target_thread),
        ):
            server.shutdown()
            server.server_close()
            thread.join()

    captured = capsys.readouterr()
    assert len(source_requests) == 1
    assert source_requests[0][1]["X-Api-Key"] == "semantic-secret"
    assert target_requests == []
    assert stats.enriched == 0
    assert item.abstract == ""
    assert "semantic-secret" not in captured.err
    assert "redirect-target" not in captured.err
    assert "redirect response body" not in captured.err


def test_substantive_existing_abstract_is_never_requested_or_replaced():
    original = (
        "Diamond detector device 3 enables measurements through a newly fabricated "
        "electrode geometry."
    )
    item = paper(3, abstract=original)
    state = FeedState(papers={record_key(item): item})
    transport = RecordingTransport([])

    stats = AbstractEnricher("key", transport=transport).enrich(
        state, [record_key(item), record_key(item)]
    )

    assert transport.calls == []
    assert item.abstract == original
    assert item.sources == ["crossref"]
    assert stats.enriched == 0


def test_valid_openaire_description_is_cleaned_and_stored_in_full():
    item = paper(4)
    key = record_key(item)
    complete = "First sentence. " + "Detailed device results. " * 40
    transport = RecordingTransport([
        semantic_response([]),
        openaire_response([
            openaire_record(
                item,
                [None, "No abstract available.", f"<p>  {complete}\n</p>", "unused"],
            ),
        ]),
    ])
    state = FeedState(papers={key: item}, pending_ai=[key])

    stats = AbstractEnricher("key", transport=transport).enrich(state, [key])

    assert item.abstract == complete.strip()
    assert item.sources == ["crossref", "openaire"]
    assert stats.enriched == 1


@pytest.mark.parametrize("provider", ["semantic", "openaire"])
def test_duplicate_provider_doi_results_fail_closed(provider):
    item = paper(5)
    key = record_key(item)
    first = "First candidate abstract with substantive research details."
    second = "Second candidate abstract with conflicting research details."
    semantic_records = []
    openaire_records = []
    if provider == "semantic":
        semantic_records = [semantic_record(item, first), semantic_record(item, second)]
    else:
        openaire_records = [
            openaire_record(item, [first]),
            openaire_record(item, [second]),
        ]
    transport = RecordingTransport([
        semantic_response(semantic_records),
        openaire_response(openaire_records),
    ])
    state = FeedState(papers={key: item}, pending_ai=[key])

    AbstractEnricher("key", transport=transport).enrich(state, [key])

    assert item.abstract == ""
    assert item.sources == ["crossref"]


@pytest.mark.parametrize(
    "responses",
    [
        [MetadataResponse(200, b"not-json"), MetadataResponse(200, b"not-json")],
        [MetadataResponse(200, b"{}"), MetadataResponse(200, b"{}")],
        [MetadataResponse(200, b"null"), MetadataResponse(200, b"null")],
    ],
)
def test_missing_or_invalid_json_is_nonfatal_and_does_not_mutate(responses, capsys):
    item = paper(6)
    key = record_key(item)
    state = FeedState(papers={key: item}, pending_ai=[key])

    stats = AbstractEnricher("key", transport=RecordingTransport(responses)).enrich(
        state, [key]
    )

    assert stats.enriched == 0
    assert item.abstract == ""
    assert item.sources == ["crossref"]
    assert "not-json" not in capsys.readouterr().err


def test_compatible_case_and_punctuation_title_variant_is_accepted():
    item = paper(7)
    key = record_key(item)
    transport = RecordingTransport([
        semantic_response([
            semantic_record(
                item,
                "We report an original abstract containing complete experimental details.",
                title="DIAMOND detector—DEVICE: 7!",
            )
        ])
    ])
    state = FeedState(papers={key: item}, pending_ai=[key])

    stats = AbstractEnricher("key", transport=transport).enrich(state, [key])

    assert stats.enriched == 1
    assert item.abstract.startswith("We report")
    assert item.sources == ["crossref", "semantic-scholar"]


def test_title_containment_does_not_match_inside_a_different_token():
    item = paper(9)
    key = record_key(item)
    transport = RecordingTransport([
        semantic_response([
            semantic_record(
                item,
                "A plausible but incorrectly matched abstract.",
                title="Diamond detector device 90",
            )
        ]),
        openaire_response([]),
    ])
    state = FeedState(papers={key: item}, pending_ai=[key])

    AbstractEnricher("key", transport=transport).enrich(state, [key])

    assert item.abstract == ""
    assert item.sources == ["crossref"]


def test_newly_enriched_processed_record_is_requeued_without_clearing_decision():
    item = paper(8, processed=True)
    key = record_key(item)
    state = FeedState(papers={key: item}, pending_ai=[])
    transport = RecordingTransport([
        semantic_response([
            semantic_record(item, "Full original findings for this diamond detector study.")
        ])
    ])

    AbstractEnricher("key", transport=transport).enrich(state, [key])

    assert state.pending_ai == [key]
    assert item.summary_zh == "旧摘要"
    assert item.categories == ["electronics-optoelectronics"]
    assert item.ai_relevant is True
    assert item.ai_confidence == 0.8
