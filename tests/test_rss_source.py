from pathlib import Path
import socket
from urllib.error import HTTPError, URLError

import pytest
from curl_cffi import requests as curl_requests

from diamond_feed.http import FetchError, HttpResult, fetch_bytes
from diamond_feed.sources.rss import classify_failure, collect_rss, parse_feed


def test_parse_atom_feed_to_canonical_records():
    records = parse_feed(Path("tests/fixtures/sample_feed.xml").read_bytes(), "https://feed.test/rss")

    assert len(records) == 2
    assert records[0].doi == "10.1000/diamond.1"
    assert records[0].abstract == "Preserved study summary."
    assert records[0].sources == ["rss"]
    assert records[0].published_at.tzinfo is not None
    assert records[0].published_at.utcoffset().total_seconds() == 0
    assert records[1].doi == "10.1000/diamond.2"
    assert records[1].source_ids == ["tag:feed.test,2026:diamond-2"]


def test_failure_taxonomy_distinguishes_hard_and_soft_failures():
    assert classify_failure(404, None, False) == "http_404"
    assert classify_failure(410, None, False) == "http_410"
    assert classify_failure(200, None, True) == "empty_feed"
    assert classify_failure(None, TimeoutError(), False) == "timeout"
    assert classify_failure(200, ValueError("bad xml"), False) == "parse_error"


@pytest.mark.parametrize(
    ("error", "expected_attempts"),
    [
        (TimeoutError("slow"), 3),
        (URLError(socket.gaierror("temporary DNS failure")), 3),
    ],
)
def test_fetch_bytes_retries_only_transient_transport_errors(monkeypatch, error, expected_attempts):
    calls = []
    sleeps = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        raise error

    monkeypatch.setattr("diamond_feed.http.urlopen", fake_urlopen)
    monkeypatch.setattr("diamond_feed.http.time.sleep", sleeps.append)

    with pytest.raises(FetchError) as raised:
        fetch_bytes("https://feed.test/rss", timeout=3, attempts=3)

    assert len(calls) == expected_attempts
    assert sleeps == [1, 2]
    assert raised.value.category in {"timeout", "url_error"}


@pytest.mark.parametrize("status", [429, 500, 503])
def test_fetch_bytes_retries_retryable_http_statuses(monkeypatch, status):
    calls = []
    sleeps = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        raise HTTPError("https://feed.test/rss", status, "test response", None, None)

    monkeypatch.setattr("diamond_feed.http.urlopen", fake_urlopen)
    monkeypatch.setattr("diamond_feed.http.time.sleep", sleeps.append)

    with pytest.raises(FetchError) as raised:
        fetch_bytes("https://feed.test/rss", timeout=3, attempts=3)

    assert len(calls) == 3
    assert sleeps == [1, 2]
    assert raised.value.status == status


@pytest.mark.parametrize("status", [404, 410])
def test_fetch_bytes_does_not_retry_retired_or_missing_endpoints(monkeypatch, status):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        raise HTTPError("https://feed.test/rss", status, "test response", None, None)

    monkeypatch.setattr("diamond_feed.http.urlopen", fake_urlopen)
    monkeypatch.setattr("diamond_feed.http.time.sleep", lambda _: pytest.fail("must not sleep"))

    with pytest.raises(FetchError) as raised:
        fetch_bytes("https://feed.test/rss", timeout=3, attempts=3)

    assert len(calls) == 1
    assert raised.value.status == status


def test_fetch_bytes_uses_mdpi_curl_parameters(monkeypatch):
    seen = {}

    class Response:
        content = b"feed"
        status_code = 200
        url = "https://www.mdpi.com/final"

    def fake_get(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr("diamond_feed.http.curl_requests.get", fake_get)

    result = fetch_bytes("https://www.mdpi.com/rss", timeout=7, attempts=1)

    assert result.body == b"feed"
    assert result.final_url == "https://www.mdpi.com/final"
    assert seen == {
        "url": "https://www.mdpi.com/rss",
        "timeout": 7,
        "impersonate": "chrome",
        "allow_redirects": True,
    }


def test_collect_rss_maps_hard_failure_without_header_leakage():
    def fetcher(url):
        raise FetchError(status=404, category="http_404", detail="HTTP 404 headers={'Authorization': 'hidden'}")

    records, failure = collect_rss("https://feed.test/rss", fetcher)

    assert records == []
    assert failure is not None
    assert failure.category == "http_404"
    assert "Authorization" not in failure.detail


def test_collect_rss_maps_soft_empty_feed_failure():
    def fetcher(url):
        return type("Result", (), {"body": b"<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'/>", "status": 200})()

    records, failure = collect_rss("https://feed.test/rss", fetcher)

    assert records == []
    assert failure is not None
    assert failure.category == "empty_feed"


def test_collect_rss_treats_bozo_zero_entries_as_parse_error():
    def fetcher(url):
        return type("Result", (), {"body": b"not xml", "status": 200})()

    records, failure = collect_rss("https://feed.test/rss", fetcher)

    assert records == []
    assert failure is not None
    assert failure.category == "parse_error"


def test_parse_feed_marks_missing_dates(monkeypatch):
    body = b"""<feed xmlns='http://www.w3.org/2005/Atom'><title>Diamond Journal</title><entry><id>entry-1</id><title>Untimed diamond paper</title><summary>Summary</summary></entry></feed>"""

    records = parse_feed(body, "https://feed.test/rss")

    assert records[0].journal == "Diamond Journal"
    assert "missing_date" in records[0].categories
    assert records[0].published_at.tzinfo is not None


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (curl_requests.exceptions.Timeout("slow"), "timeout"),
        (curl_requests.exceptions.ConnectionError("disconnected"), "network_error"),
    ],
)
def test_fetch_bytes_retries_mdpi_transient_transport_errors(monkeypatch, error, category):
    calls = []
    sleeps = []

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        raise error

    monkeypatch.setattr("diamond_feed.http.curl_requests.get", fake_get)
    monkeypatch.setattr("diamond_feed.http.time.sleep", sleeps.append)

    with pytest.raises(FetchError) as raised:
        fetch_bytes("https://www.mdpi.com/rss", timeout=3, attempts=3)

    assert len(calls) == 3
    assert sleeps == [1, 2]
    assert raised.value.category == category


def test_fetch_bytes_retries_only_transient_url_errors(monkeypatch):
    calls = []
    sleeps = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        raise URLError(socket.gaierror("temporary DNS failure"))

    monkeypatch.setattr("diamond_feed.http.urlopen", fake_urlopen)
    monkeypatch.setattr("diamond_feed.http.time.sleep", sleeps.append)

    with pytest.raises(FetchError) as raised:
        fetch_bytes("https://feed.test/rss", timeout=3, attempts=3)

    assert len(calls) == 3
    assert sleeps == [1, 2]
    assert raised.value.category == "url_error"


def test_fetch_bytes_does_not_retry_permanent_url_error(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        raise URLError("unknown url type: bad")

    monkeypatch.setattr("diamond_feed.http.urlopen", fake_urlopen)
    monkeypatch.setattr("diamond_feed.http.time.sleep", lambda _: pytest.fail("must not sleep"))

    with pytest.raises(FetchError) as raised:
        fetch_bytes("bad://feed.test/rss", timeout=3, attempts=3)

    assert len(calls) == 1
    assert raised.value.category == "url_error"


@pytest.mark.parametrize(
    ("attempts", "expected_sleeps"),
    [(1, []), (4, [1, 2, 4])],
)
def test_fetch_bytes_never_sleeps_beyond_attempt_budget(monkeypatch, attempts, expected_sleeps):
    sleeps = []

    def fake_urlopen(request, timeout):
        raise TimeoutError("slow")

    monkeypatch.setattr("diamond_feed.http.urlopen", fake_urlopen)
    monkeypatch.setattr("diamond_feed.http.time.sleep", sleeps.append)

    with pytest.raises(FetchError):
        fetch_bytes("https://feed.test/rss", timeout=3, attempts=attempts)

    assert sleeps == expected_sleeps


@pytest.mark.parametrize("status", [404, 410, 502])
def test_collect_rss_classifies_fetch_result_status_before_parsing(status):
    def fetcher(url):
        return HttpResult(body=b"not xml", status=status, final_url="https://feed.test/final")

    records, failure = collect_rss("https://feed.test/rss", fetcher)

    assert records == []
    assert failure is not None
    assert failure.category == f"http_{status}"
    assert failure.url == "https://feed.test/final"
    assert failure.detail == f"HTTP {status}"
