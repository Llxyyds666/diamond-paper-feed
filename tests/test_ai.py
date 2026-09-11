import json
from math import inf, nan
from urllib.error import HTTPError

import pytest

from diamond_feed.ai import DeepSeekClient, RequestBudget, RequestCounter, screen_batch
from diamond_feed.normalize import record_key


def _decision(key, **changes):
    result = {
        "key": key,
        "relevant": True,
        "confidence": 0.95,
        "category": "films-membranes",
        "matched_topics": [],
        "summary_zh": "研究了金刚石膜。",
        "reason": "研究对象是金刚石材料。",
    }
    result.update(changes)
    return result


def _response(content):
    return {"choices": [{"message": {"content": content}}]}


def _single_decision_client(ai_config, decision):
    return DeepSeekClient(
        "test-api-key",
        ai_config,
        transport=lambda *args: _response(
            json.dumps({"decisions": [decision]}, ensure_ascii=False)
        ),
    )


def test_screening_accepts_controlled_multi_focus_labels(ai_config, diamond_records):
    key = record_key(diamond_records[0])
    client = _single_decision_client(
        ai_config,
        _decision(
            key,
            matched_topics=[
                "diamond-power-rf-detectors",
                "device-grade-single-crystal",
            ],
        ),
    )

    decision = screen_batch(
        diamond_records[:1], client, ai_config, RequestBudget(1)
    )[0]

    assert decision.matched_topics == [
        "diamond-power-rf-detectors",
        "device-grade-single-crystal",
    ]


@pytest.mark.parametrize("topics", [["diamond"], ["unknown"], [1]])
def test_screening_rejects_unknown_focus_labels(
    ai_config, diamond_records, topics
):
    key = record_key(diamond_records[0])
    client = _single_decision_client(
        ai_config, _decision(key, matched_topics=topics)
    )

    with pytest.raises(ValueError, match="invalid model response"):
        screen_batch(diamond_records[:1], client, ai_config, RequestBudget(1))


def test_excluded_decision_cannot_claim_focus_area(ai_config, diamond_records):
    key = record_key(diamond_records[0])
    client = _single_decision_client(
        ai_config,
        _decision(
            key,
            relevant=False,
            matched_topics=["diamond-power-rf-detectors"],
            summary_zh="",
            reason="The actual subject is outside the diamond research scope.",
        ),
    )

    with pytest.raises(ValueError, match="invalid model response"):
        screen_batch(diamond_records[:1], client, ai_config, RequestBudget(1))


def test_screening_truncates_abstracts_and_validates_json(ai_config, diamond_records):
    seen = []
    record = diamond_records[0]
    record.abstract = "金刚石" * 500

    def transport(url, headers, payload, timeout):
        seen.append((url, headers, payload, timeout))
        request_content = json.loads(payload["messages"][1]["content"])
        body = [_decision(request_content["papers"][0]["key"])]
        return _response(json.dumps(body, ensure_ascii=False))

    client = DeepSeekClient("test-api-key", ai_config, transport=transport)
    decisions = screen_batch([record], client, ai_config, RequestBudget(5))

    assert decisions[0].relevant is True
    request_content = json.loads(seen[0][2]["messages"][1]["content"])
    assert len(request_content["papers"][0]["abstract"]) == 1200
    assert request_content["papers"][0]["abstract"] == record.abstract[:1200]
    assert 'json object with exactly a "decisions" field' in seen[0][2]["messages"][0]["content"]
    assert '"decisions": [' in seen[0][2]["messages"][0]["content"]


def test_screening_accepts_the_json_object_decisions_wrapper(ai_config, diamond_records):
    key = record_key(diamond_records[0])
    client = DeepSeekClient(
        "test-api-key",
        ai_config,
        transport=lambda *args: _response(
            json.dumps({"decisions": [_decision(key)]}, ensure_ascii=False)
        ),
    )

    decisions = screen_batch(
        diamond_records[:1], client, ai_config, RequestBudget(1)
    )

    assert [decision.key for decision in decisions] == [key]


def test_timeout_retries_three_times_and_reports_incomplete_usage(ai_config, diamond_records):
    calls = 0
    waits = []

    def failing_transport(url, headers, payload, timeout):
        nonlocal calls
        calls += 1
        raise TimeoutError("timeout")

    counter = RequestCounter()
    client = DeepSeekClient(
        "test-api-key", ai_config, transport=failing_transport, wait=waits.append
    )

    with pytest.raises(RuntimeError, match="TimeoutError status=none attempt=3"):
        screen_batch(diamond_records[:1], client, ai_config, counter)

    assert calls == 3
    assert waits == [1.0, 2.0]
    assert counter.used == 3
    assert client.token_usage_complete is False


def test_request_counter_never_exhausts():
    counter = RequestCounter()

    for _ in range(1000):
        counter.consume()

    assert counter.used == 1000


@pytest.mark.parametrize("maximum", [0, -1, True, 1.5, "2"])
def test_request_budget_requires_a_strictly_positive_integer(maximum):
    with pytest.raises(ValueError):
        RequestBudget(maximum)


def test_request_budget_tracks_remaining_and_consumes_before_network(ai_config):
    budget = RequestBudget(2)
    observed = []

    def transport(url, headers, payload, timeout):
        observed.append((budget.used, budget.remaining))
        return _response("[]")

    client = DeepSeekClient("test-api-key", ai_config, transport=transport)
    assert budget.remaining == 2
    client.complete_json([], 7, budget)
    assert observed == [(1, 1)]
    assert budget.used == 1
    assert budget.remaining == 1


def test_exhausted_budget_does_not_call_transport(ai_config):
    calls = 0

    def transport(url, headers, payload, timeout):
        nonlocal calls
        calls += 1
        return _response("[]")

    budget = RequestBudget(1)
    client = DeepSeekClient("test-api-key", ai_config, transport=transport)
    client.complete_json([], 1, budget)
    with pytest.raises(RuntimeError, match="request budget exhausted"):
        client.complete_json([], 1, budget)
    assert calls == 1


@pytest.mark.parametrize("status", [408, 425, 429, 500, 503])
def test_retries_only_explicit_retryable_http_statuses(ai_config, status):
    calls = 0

    def transport(url, headers, payload, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HTTPError(url, status, "hidden response", None, None)
        return _response("[]")

    counter = RequestCounter()
    client = DeepSeekClient(
        "test-api-key", ai_config, transport=transport, wait=lambda _: None
    )
    assert client.complete_json([], 1, counter) == []
    assert (calls, counter.used) == (2, 2)


def test_timeout_retries_and_can_succeed(ai_config):
    calls = 0

    def transport(url, headers, payload, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("do not expose this")
        return _response("[]")

    counter = RequestCounter()
    waits = []
    client = DeepSeekClient(
        "test-api-key", ai_config, transport=transport, wait=waits.append
    )
    assert client.complete_json([], 1, counter) == []

    assert (calls, counter.used) == (2, 2)
    assert waits == [1.0]
    assert client.token_usage_complete is False


def test_invalid_model_json_retries_then_succeeds(ai_config):
    responses = iter([_response("not-json"), _response("still-not-json"), _response("[]")])
    waits = []
    counter = RequestCounter()
    client = DeepSeekClient(
        "test-api-key",
        ai_config,
        transport=lambda *args: next(responses),
        wait=waits.append,
    )

    assert client.complete_json([], 512, counter) == []
    assert counter.used == 3
    assert waits == [1.0, 2.0]


@pytest.mark.parametrize("status", [401, 403, 402])
def test_auth_and_insufficient_balance_errors_are_not_retried_or_leaked(ai_config, status):
    calls = 0
    secret = "test-api-key"

    def transport(url, headers, payload, timeout):
        nonlocal calls
        calls += 1
        raise HTTPError(url, status, f"response containing {secret}", None, None)

    client = DeepSeekClient(secret, ai_config, transport=transport)
    with pytest.raises(RuntimeError) as raised:
        client.complete_json([], 1, RequestBudget(3))

    assert calls == 1
    assert f"status={status}" in str(raised.value)
    assert "HTTPError" in str(raised.value)
    assert "attempt=1" in str(raised.value)
    assert secret not in str(raised.value)
    assert "response" not in str(raised.value)


def test_urllib_http_error_is_handled_from_status_without_reading_body(ai_config):
    class ForbiddenBody:
        def read(self):
            pytest.fail("HTTP error body must not be read")

        def close(self):
            return None

    def transport(url, headers, payload, timeout):
        raise HTTPError(url, 401, "forbidden", None, ForbiddenBody())

    with pytest.raises(RuntimeError, match="HTTPError status=401 attempt=1"):
        DeepSeekClient("test-api-key", ai_config, transport=transport).complete_json([], 1, RequestBudget(1))


def test_default_transport_uses_required_endpoint_payload_and_headers(ai_config, monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"[]"}}]}'

        def getcode(self):
            return 200

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        seen["payload"] = json.loads(request.data)
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr("diamond_feed.ai.urlopen", fake_urlopen)
    client = DeepSeekClient("test-api-key", ai_config)

    assert client.complete_json([{"role": "user", "content": "hello"}], 77, RequestBudget(1)) == []
    assert seen["url"] == "https://api.deepseek.com/chat/completions"
    assert seen["payload"] == {
        "model": ai_config.model,
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 77,
    }
    assert seen["headers"]["Content-type"] == "application/json"
    assert seen["headers"]["Authorization"] == "Bearer test-api-key"
    assert seen["timeout"] >= 600


def test_default_transport_errors_do_not_expose_key(ai_config, monkeypatch):
    def fake_urlopen(request, timeout):
        raise TimeoutError("test-api-key must not appear")

    monkeypatch.setattr("diamond_feed.ai.urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="TimeoutError status=none attempt=1") as raised:
        DeepSeekClient("test-api-key", ai_config).complete_json([], 1, RequestBudget(1))
    assert "test-api-key" not in str(raised.value)


def test_screening_returns_empty_without_spending_a_request(ai_config):
    def transport(*args):
        pytest.fail("empty batches must not call transport")

    budget = RequestBudget(1)
    assert screen_batch([], DeepSeekClient("test-api-key", ai_config, transport=transport), ai_config, budget) == []
    assert budget.used == 0


def test_screening_rejects_batch_larger_than_configuration_before_network(ai_config, diamond_records):
    records = [diamond_records[0]] * (ai_config.batch_size + 1)

    def transport(*args):
        pytest.fail("oversized batches must not call transport")

    with pytest.raises(ValueError, match="batch size"):
        screen_batch(records, DeepSeekClient("test-api-key", ai_config, transport=transport), ai_config, RequestBudget(1))


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "```json\\n[]\\n```",
        "{}",
        '[{"key":"x","key":"x","relevant":true,"confidence":0.5,"category":"films-membranes","matched_topics":[],"summary_zh":"s","reason":"r"}]',
    ],
)
def test_screening_rejects_non_array_fenced_and_duplicate_json(ai_config, diamond_records, content):
    client = DeepSeekClient("test-api-key", ai_config, transport=lambda *args: _response(content))
    with pytest.raises(ValueError, match="invalid model response"):
        screen_batch(diamond_records[:1], client, ai_config, RequestBudget(1))


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": []}}]},
    ],
)
def test_screening_rejects_invalid_choices_message_content_shape(ai_config, diamond_records, response):
    client = DeepSeekClient("test-api-key", ai_config, transport=lambda *args: response)
    with pytest.raises(ValueError, match="invalid model response"):
        screen_batch(diamond_records[:1], client, ai_config, RequestBudget(1))


@pytest.mark.parametrize(
    "changes",
    [
        {"relevant": 1},
        {"confidence": True},
        {"confidence": nan},
        {"confidence": inf},
        {"confidence": -0.01},
        {"confidence": 1.01},
        {"category": "not-a-category"},
        {"matched_topics": "not a list"},
        {"matched_topics": ["ok", 3]},
        {"summary_zh": 3},
        {"reason": None},
    ],
)
def test_screening_rejects_wrong_decision_field_types_and_values(ai_config, diamond_records, changes):
    content = json.dumps([_decision(record_key(diamond_records[0]), **changes)])
    client = DeepSeekClient("test-api-key", ai_config, transport=lambda *args: _response(content))
    with pytest.raises(ValueError, match="invalid model response"):
        screen_batch(diamond_records[:1], client, ai_config, RequestBudget(1))


def test_screening_requires_exact_decision_fields_and_requested_keys_once(ai_config, diamond_records):
    key = record_key(diamond_records[0])
    cases = [
        [],
        [{field: value for field, value in _decision(key).items() if field != "reason"}],
        [{**_decision(key), "extra": "no"}],
        [_decision("unknown-key")],
        [_decision(key), _decision(key)],
    ]
    for body in cases:
        client = DeepSeekClient("test-api-key", ai_config, transport=lambda *args, body=body: _response(json.dumps(body)))
        with pytest.raises(ValueError, match="invalid model response"):
            screen_batch(diamond_records[:1], client, ai_config, RequestBudget(1))


def test_screening_rejects_whole_batch_when_any_decision_is_invalid(ai_config, diamond_records):
    body = [
        _decision(record_key(diamond_records[0])),
        _decision(record_key(diamond_records[1]), category="not-a-category"),
    ]
    client = DeepSeekClient("test-api-key", ai_config, transport=lambda *args: _response(json.dumps(body)))

    with pytest.raises(ValueError, match="invalid model response"):
        screen_batch(diamond_records, client, ai_config, RequestBudget(1))


def test_complete_json_accepts_a_strict_digest_object_without_weakening_screening(ai_config, diamond_records):
    client = DeepSeekClient(
        "test-api-key",
        ai_config,
        transport=lambda *args: _response('{"html":"<section><h2>概览</h2></section>"}'),
    )

    assert client.complete_json([], 10, RequestBudget(1)) == {
        "html": "<section><h2>概览</h2></section>"
    }

    client = DeepSeekClient(
        "test-api-key",
        ai_config,
        transport=lambda *args: _response('{"html":"<section></section>"}'),
    )
    with pytest.raises(ValueError, match="invalid model response"):
        screen_batch(diamond_records[:1], client, ai_config, RequestBudget(1))


def test_complete_json_accumulates_available_model_token_usage(ai_config):
    responses = iter(
        [
            {
                "choices": [{"message": {"content": "[]"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
            },
            {
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
            },
        ]
    )
    client = DeepSeekClient("test-api-key", ai_config, transport=lambda *args: next(responses))
    budget = RequestBudget(2)

    client.complete_json([], 10, budget)
    client.complete_json([], 10, budget)

    assert (client.prompt_tokens, client.completion_tokens, client.total_tokens) == (18, 6, 24)


def test_complete_json_counts_usage_from_an_invalid_json_completion(ai_config):
    response = {
        "choices": [{"message": {"content": "not JSON"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
    }
    client = DeepSeekClient("test-api-key", ai_config, transport=lambda *args: response)

    with pytest.raises(ValueError, match="invalid model response"):
        client.complete_json([], 10, RequestBudget(1))

    assert (client.prompt_tokens, client.completion_tokens, client.total_tokens) == (5, 1, 6)
