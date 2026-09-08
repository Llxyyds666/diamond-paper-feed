from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

from diamond_feed.ai import DeepSeekClient
from diamond_feed.models import PaperRecord
from diamond_feed.normalize import record_key
from diamond_feed.state import FeedState, load_state, save_state
from diamond_feed import summarize
from diamond_feed.summarize import run_summary


NOW = datetime(2026, 9, 6, tzinfo=timezone.utc)


def _decision(key, *, relevant=True, category="other-diamond", summary="中文摘要"):
    return {
        "key": key,
        "relevant": relevant,
        "confidence": 0.8,
        "category": category,
        "matched_topics": ["diamond"],
        "summary_zh": summary,
        "reason": "研究金刚石。",
    }


class RecordingClient:
    def __init__(self, *, relevant=True, digest=None):
        self.relevant = relevant
        self.digest = {"html": "<section><h2>模型概览</h2></section>"} if digest is None else digest
        self.payloads = []
        self.messages = []

    def complete_json(self, messages, max_tokens, budget):
        budget.consume()
        self.messages.append(messages)
        payload = json.loads(messages[-1]["content"])
        self.payloads.append(payload)
        if "papers" not in payload:
            return self.digest
        return [
            _decision(
                paper["key"],
                relevant=self.relevant,
                summary=f"{paper['title']} 的有效中文摘要。",
            )
            for paper in payload["papers"]
        ]


def _save_records(path: Path, records: list[PaperRecord]) -> list[str]:
    keys = [record_key(record) for record in records]
    save_state(path, FeedState(dict(zip(keys, records)), keys, {}))
    return keys


def test_first_run_processes_at_most_40_and_uses_at_most_5_requests(
    tmp_path, configured_state_with_100_pending, fake_deepseek_client, app_config
):
    original_keys = list(load_state(configured_state_with_100_pending).pending_ai)
    stats = run_summary(
        app_config,
        configured_state_with_100_pending,
        fake_deepseek_client,
        datetime(2026, 9, 6, tzinfo=timezone.utc),
        output_dir=tmp_path,
    )
    assert stats.candidates == 40
    assert stats.requests == 5
    assert fake_deepseek_client.requests == 5
    assert stats.processed == 40
    assert stats.selected == 40
    assert stats.remaining == 60
    state = load_state(configured_state_with_100_pending)
    assert state.pending_ai == original_keys[40:]
    assert (tmp_path / "ai_summary_feed.xml").exists()
    assert (tmp_path / "ai_summary.html").exists()
    assert (tmp_path / "ai_usage.json").exists()


def test_same_utc_day_reuses_persisted_candidate_and_request_limits(
    tmp_path, configured_state_with_100_pending, app_config
):
    first_client = RecordingClient()
    first = run_summary(
        app_config,
        configured_state_with_100_pending,
        first_client,
        datetime(2026, 9, 7, 7, tzinfo=timezone.utc),
        output_dir=tmp_path,
    )
    second_client = RecordingClient()
    second = run_summary(
        app_config,
        configured_state_with_100_pending,
        second_client,
        datetime(2026, 9, 7, 23, tzinfo=timezone.utc),
        output_dir=tmp_path,
    )
    usage = json.loads((tmp_path / "ai_usage.json").read_text(encoding="utf-8"))

    assert (first.candidates, first.requests) == (40, 5)
    assert (second.candidates, second.requests, second.processed) == (0, 0, 0)
    assert second_client.payloads == []
    assert usage[-1]["date"] == "2026-09-07"
    assert usage[-1]["candidates"] == 40
    assert usage[-1]["requests"] == 5


def test_failed_attempt_is_persisted_and_reduces_same_day_request_budget(
    tmp_path, configured_state_with_100_pending, failing_deepseek_client, app_config
):
    previous_feed = "<rss>previous feed</rss>"
    previous_html = "<html><body>previous digest</body></html>"
    (tmp_path / "ai_summary_feed.xml").write_text(previous_feed, encoding="utf-8")
    (tmp_path / "ai_summary.html").write_text(previous_html, encoding="utf-8")
    before = configured_state_with_100_pending.read_text(encoding="utf-8")

    first = run_summary(
        app_config,
        configured_state_with_100_pending,
        failing_deepseek_client,
        datetime(2026, 9, 7, tzinfo=timezone.utc),
        output_dir=tmp_path,
    )
    assert (first.failed, first.candidates, first.requests, first.processed) == (
        True,
        40,
        1,
        0,
    )
    assert (tmp_path / "ai_summary_feed.xml").read_text(encoding="utf-8") == previous_feed
    assert (tmp_path / "ai_summary.html").read_text(encoding="utf-8") == previous_html
    assert configured_state_with_100_pending.read_text(encoding="utf-8") == before

    second_client = RecordingClient()
    second = run_summary(
        app_config,
        configured_state_with_100_pending,
        second_client,
        datetime(2026, 9, 7, 12, tzinfo=timezone.utc),
        output_dir=tmp_path,
    )

    usage = json.loads((tmp_path / "ai_usage.json").read_text(encoding="utf-8"))[-1]
    assert (second.candidates, second.requests) == (0, 0)
    assert second_client.payloads == []
    assert (usage["candidates"], usage["requests"]) == (40, 1)


def test_next_utc_day_restores_candidate_and_request_limits(
    tmp_path, configured_state_with_100_pending, app_config
):
    shanghai = timezone(timedelta(hours=8))
    run_summary(
        app_config,
        configured_state_with_100_pending,
        RecordingClient(),
        datetime(2026, 9, 8, 7, 59, tzinfo=shanghai),
        output_dir=tmp_path,
    )
    client = RecordingClient()
    stats = run_summary(
        app_config,
        configured_state_with_100_pending,
        client,
        datetime(2026, 9, 8, 8, 0, tzinfo=shanghai),
        output_dir=tmp_path,
    )

    assert (stats.candidates, stats.requests, stats.processed) == (40, 5, 40)
    usage = json.loads((tmp_path / "ai_usage.json").read_text(encoding="utf-8"))
    assert [(entry["date"], entry["candidates"], entry["requests"]) for entry in usage] == [
        ("2026-09-07", 40, 5),
        ("2026-09-08", 40, 5),
    ]


def test_same_day_prior_usage_leaves_only_remaining_request_attempts(
    tmp_path, configured_state_with_100_pending, app_config
):
    prior = [{
        "date": "2026-09-07",
        "candidates": 10,
        "processed": 10,
        "selected": 10,
        "requests": 4,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }]
    (tmp_path / "ai_usage.json").write_text(json.dumps(prior), encoding="utf-8")
    client = RecordingClient()

    stats = run_summary(
        app_config,
        configured_state_with_100_pending,
        client,
        datetime(2026, 9, 7, 8, tzinfo=timezone.utc),
        output_dir=tmp_path,
    )
    usage = json.loads((tmp_path / "ai_usage.json").read_text(encoding="utf-8"))[-1]

    assert (stats.candidates, stats.processed, stats.requests, stats.failed) == (30, 10, 1, True)
    assert len(client.payloads) == 1
    assert (usage["candidates"], usage["processed"], usage["requests"]) == (40, 20, 5)


def test_candidate_limit_selects_the_oldest_40_even_if_queue_order_is_stale(
    tmp_path, configured_state_with_100_pending, fake_deepseek_client, app_config
):
    state = load_state(configured_state_with_100_pending)
    original_keys = list(state.pending_ai)
    for index, key in enumerate(original_keys):
        state.papers[key].published_at = NOW - timedelta(days=index)
    save_state(configured_state_with_100_pending, state)

    stats = run_summary(
        app_config,
        configured_state_with_100_pending,
        fake_deepseek_client,
        NOW,
        output_dir=tmp_path,
    )

    assert stats.candidates == 40
    assert load_state(configured_state_with_100_pending).pending_ai == original_keys[:60]


def test_ai_failure_preserves_previous_outputs_and_queue(
    tmp_path, configured_state_with_100_pending, failing_deepseek_client, app_config
):
    previous = {
        "ai_summary_feed.xml": "<rss>previous feed</rss>",
        "ai_summary.html": "<html><body>previous digest</body></html>",
    }
    for name, contents in previous.items():
        (tmp_path / name).write_text(contents, encoding="utf-8")
    (tmp_path / "ai_usage.json").write_text("[]\n", encoding="utf-8")
    before = configured_state_with_100_pending.read_text(encoding="utf-8")
    stats = run_summary(
        app_config,
        configured_state_with_100_pending,
        failing_deepseek_client,
        datetime(2026, 9, 6, tzinfo=timezone.utc),
        output_dir=tmp_path,
    )
    assert stats.failed is True
    for name, contents in previous.items():
        assert (tmp_path / name).read_text(encoding="utf-8") == contents
    usage = json.loads((tmp_path / "ai_usage.json").read_text(encoding="utf-8"))
    assert (usage[0]["candidates"], usage[0]["requests"]) == (40, 1)
    assert configured_state_with_100_pending.read_text(encoding="utf-8") == before
    assert not list(tmp_path.glob("*.tmp"))


def test_timeout_failure_is_logged_safely_and_marks_token_usage_incomplete(
    tmp_path, configured_state_with_100_pending, app_config, capsys
):
    secret = "do-not-print-this-key"
    calls = 0

    def timeout_transport(url, headers, payload, timeout):
        nonlocal calls
        calls += 1
        raise TimeoutError(f"provider may still be billing {secret}")

    client = DeepSeekClient(secret, app_config.ai, transport=timeout_transport)
    stats = run_summary(
        app_config,
        configured_state_with_100_pending,
        client,
        datetime(2026, 9, 8, tzinfo=timezone.utc),
        output_dir=tmp_path,
    )
    usage = json.loads((tmp_path / "ai_usage.json").read_text(encoding="utf-8"))[0]
    captured = capsys.readouterr()

    assert (calls, stats.requests, stats.processed) == (1, 1, 0)
    assert stats.token_usage_complete is False
    assert usage["token_usage_complete"] is False
    assert "TimeoutError status=none attempt=1" in captured.err
    assert secret not in captured.err


def test_later_batch_failure_publishes_successful_batch_and_leaves_rest_queued(
    tmp_path, configured_state_with_100_pending, app_config
):
    class FailSecondBatch(RecordingClient):
        def complete_json(self, messages, max_tokens, budget):
            payload = json.loads(messages[-1]["content"])
            if "papers" in payload and len(self.payloads) == 1:
                budget.consume()
                self.payloads.append(payload)
                raise RuntimeError("second batch failed")
            return super().complete_json(messages, max_tokens, budget)

    before_keys = list(load_state(configured_state_with_100_pending).pending_ai)
    client = FailSecondBatch()
    stats = run_summary(
        app_config, configured_state_with_100_pending, client, NOW, output_dir=tmp_path
    )

    assert stats.candidates == 40
    assert stats.processed == 10
    assert stats.selected == 10
    assert stats.requests == 2
    assert stats.remaining == 90
    assert stats.failed is True
    state = load_state(configured_state_with_100_pending)
    assert state.pending_ai == before_keys[10:]
    assert all(state.papers[key].ai_relevant is True for key in before_keys[:10])
    assert all(state.papers[key].ai_relevant is None for key in before_keys[10:])
    assert [set(payload) for payload in client.payloads] == [{"papers", "categories", "required_fields"}] * 2
    assert "今日概览" in (tmp_path / "ai_summary.html").read_text(encoding="utf-8")


def test_screening_retry_that_consumes_reserve_uses_fallback_and_keeps_unfinished_batches(
    tmp_path, configured_state_with_100_pending, app_config
):
    class RetryConsumesReserve(RecordingClient):
        def complete_json(self, messages, max_tokens, budget):
            payload = json.loads(messages[-1]["content"])
            if "papers" in payload and len(self.payloads) == 2:
                budget.consume()
                budget.consume()
                self.payloads.append(payload)
                raise RuntimeError("retry failed")
            return super().complete_json(messages, max_tokens, budget)

    client = RetryConsumesReserve()
    stats = run_summary(
        app_config, configured_state_with_100_pending, client, NOW, output_dir=tmp_path
    )

    assert (stats.processed, stats.requests, stats.remaining, stats.failed) == (20, 4, 80, True)
    assert len(client.payloads) == 3
    assert all("papers" in payload for payload in client.payloads)
    assert "今日概览" in (tmp_path / "ai_summary.html").read_text(encoding="utf-8")


def test_invalid_whole_batch_applies_no_partial_decisions(
    tmp_path, configured_state_with_100_pending, app_config
):
    class InvalidBatch:
        def complete_json(self, messages, max_tokens, budget):
            budget.consume()
            papers = json.loads(messages[-1]["content"])["papers"]
            decisions = [_decision(paper["key"]) for paper in papers]
            decisions[-1]["category"] = "invalid-category"
            return decisions

    before = configured_state_with_100_pending.read_text(encoding="utf-8")
    stats = run_summary(
        app_config, configured_state_with_100_pending, InvalidBatch(), NOW, output_dir=tmp_path
    )

    assert (stats.processed, stats.requests, stats.failed) == (0, 1, True)
    assert configured_state_with_100_pending.read_text(encoding="utf-8") == before
    assert not (tmp_path / "ai_summary_feed.xml").exists()


def test_unexpected_client_bug_is_not_misreported_as_an_ai_failure(
    tmp_path, configured_state_with_100_pending, app_config
):
    class BuggyClient:
        def complete_json(self, messages, max_tokens, budget):
            budget.consume()
            raise KeyError("implementation bug")

    before = configured_state_with_100_pending.read_text(encoding="utf-8")
    with pytest.raises(KeyError, match="implementation bug"):
        run_summary(
            app_config,
            configured_state_with_100_pending,
            BuggyClient(),
            NOW,
            output_dir=tmp_path,
        )

    assert configured_state_with_100_pending.read_text(encoding="utf-8") == before
    assert not (tmp_path / "ai_summary_feed.xml").exists()


def test_irrelevant_decisions_leave_queue_and_all_ai_fields_are_applied(
    tmp_path, diamond_records, app_config
):
    state_path = tmp_path / "state.json"
    keys = _save_records(state_path, diamond_records)

    class MixedClient(RecordingClient):
        def complete_json(self, messages, max_tokens, budget):
            budget.consume()
            payload = json.loads(messages[-1]["content"])
            self.payloads.append(payload)
            if "papers" not in payload:
                return self.digest
            return [
                _decision(keys[0], relevant=False, category="films-membranes", summary="不相关摘要"),
                _decision(keys[1], relevant=True, category="electrochemistry-catalysis", summary="电极摘要"),
            ]

    stats = run_summary(app_config, state_path, MixedClient(), NOW, output_dir=tmp_path)
    state = load_state(state_path)

    assert (stats.processed, stats.selected, stats.remaining) == (2, 1, 0)
    assert state.pending_ai == []
    assert state.papers[keys[0]].ai_relevant is False
    assert state.papers[keys[0]].ai_confidence == 0.8
    assert state.papers[keys[0]].categories == ["films-membranes"]
    assert state.papers[keys[0]].summary_zh == "不相关摘要"
    root = ElementTree.fromstring((tmp_path / "ai_summary_feed.xml").read_text(encoding="utf-8"))
    items = root.findall("./channel/item")
    assert [item.findtext("title") for item in items] == [diamond_records[1].title]
    assert items[0].findtext("description") == "电极摘要"


def test_usage_merges_same_day_preserves_history_and_counts_client_tokens(
    tmp_path, diamond_records, app_config
):
    state_path = tmp_path / "state.json"
    _save_records(state_path, diamond_records[:1])
    prior = [
        {"date": "2026-09-05", "candidates": 4, "processed": 4, "selected": 2, "requests": 2, "prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        {"date": "2026-09-06", "candidates": 1, "processed": 1, "selected": 1, "requests": 1, "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    ]
    (tmp_path / "ai_usage.json").write_text(json.dumps(prior), encoding="utf-8")

    class CountingClient(RecordingClient):
        def __init__(self):
            super().__init__()
            self.prompt_tokens = 0
            self.completion_tokens = 0

        def complete_json(self, messages, max_tokens, budget):
            result = super().complete_json(messages, max_tokens, budget)
            self.prompt_tokens += 10
            self.completion_tokens += 3
            return result

    stats = run_summary(app_config, state_path, CountingClient(), NOW, output_dir=tmp_path)
    usage = json.loads((tmp_path / "ai_usage.json").read_text(encoding="utf-8"))

    assert (stats.prompt_tokens, stats.completion_tokens, stats.total_tokens) == (20, 6, 26)
    assert usage[0] == {**prior[0], "token_usage_complete": True}
    assert usage[1] == {
        "date": "2026-09-06",
        "candidates": 2,
        "processed": 2,
        "selected": 2,
        "requests": 3,
        "prompt_tokens": 27,
        "completion_tokens": 9,
        "total_tokens": 36,
        "token_usage_complete": True,
    }
    assert set(usage[1]) == summarize.USAGE_FIELDS


def test_digest_request_contains_only_selected_metadata_and_chinese_summaries(
    tmp_path, diamond_records, app_config
):
    state_path = tmp_path / "state.json"
    _save_records(state_path, diamond_records[:1])
    client = RecordingClient()

    run_summary(app_config, state_path, client, NOW, output_dir=tmp_path)

    assert len(client.payloads) == 2
    assert "Example json output:" in client.messages[1][0]["content"]
    assert '{"html":' in client.messages[1][0]["content"]
    assert set(client.payloads[1]) == {"selected"}
    assert set(client.payloads[1]["selected"][0]) == {
        "title",
        "authors",
        "journal",
        "published_at",
        "doi",
        "url",
        "category",
        "summary_zh",
    }
    assert "abstract" not in json.dumps(client.payloads[1], ensure_ascii=False)


def test_invalid_digest_html_falls_back_without_losing_valid_summary_or_allowing_injection(
    tmp_path, diamond_records, app_config
):
    record = diamond_records[0]
    record.title = '<script>alert("metadata")</script>'
    state_path = tmp_path / "state.json"
    _save_records(state_path, [record])
    client = RecordingClient(
        digest={"html": '<section onclick="steal()"><script>alert(1)</script></section>'}
    )

    stats = run_summary(app_config, state_path, client, NOW, output_dir=tmp_path)
    html = (tmp_path / "ai_summary.html").read_text(encoding="utf-8")

    assert stats.failed is False
    assert "今日概览" in html
    assert "有效中文摘要" in html
    assert "<script" not in html
    assert "onclick" not in html
    assert "&lt;script&gt;" in html


def test_publication_stages_outputs_then_state_and_cleans_temporaries_on_commit_error(
    tmp_path, diamond_records, app_config, monkeypatch
):
    state_path = tmp_path / "state.json"
    _save_records(state_path, diamond_records[:1])
    old_state = state_path.read_text(encoding="utf-8")
    previous = {
        "ai_summary_feed.xml": "old feed",
        "ai_summary.html": "old html",
        "ai_usage.json": "[]\n",
    }
    for name, contents in previous.items():
        (tmp_path / name).write_text(contents, encoding="utf-8")
    order = []

    def fail_commit(staged):
        order.extend(item.destination.name for item in staged)
        raise OSError("commit failed")

    monkeypatch.setattr(summarize, "commit_staged", fail_commit)
    with pytest.raises(OSError, match="commit failed"):
        run_summary(app_config, state_path, RecordingClient(), NOW, output_dir=tmp_path)

    assert order == ["ai_summary_feed.xml", "ai_summary.html", "ai_usage.json", "state.json"]
    assert state_path.read_text(encoding="utf-8") == old_state
    for name, contents in previous.items():
        assert (tmp_path / name).read_text(encoding="utf-8") == contents
    assert not list(tmp_path.glob("*.tmp"))


def test_staging_error_preserves_all_destinations_and_cleans_earlier_temporaries(
    tmp_path, diamond_records, app_config, monkeypatch
):
    state_path = tmp_path / "state.json"
    _save_records(state_path, diamond_records[:1])
    old_state = state_path.read_text(encoding="utf-8")
    previous = {
        "ai_summary_feed.xml": "old feed",
        "ai_summary.html": "old html",
        "ai_usage.json": "[]\n",
    }
    for name, contents in previous.items():
        (tmp_path / name).write_text(contents, encoding="utf-8")
    monkeypatch.setattr(
        summarize,
        "stage_state",
        lambda *args: (_ for _ in ()).throw(OSError("state stage failed")),
    )

    with pytest.raises(OSError, match="state stage failed"):
        run_summary(app_config, state_path, RecordingClient(), NOW, output_dir=tmp_path)

    assert state_path.read_text(encoding="utf-8") == old_state
    for name, contents in previous.items():
        assert (tmp_path / name).read_text(encoding="utf-8") == contents
    assert not list(tmp_path.glob("*.tmp"))


def test_mid_commit_failure_rolls_back_outputs_and_state_as_one_group(
    tmp_path, diamond_records, app_config, monkeypatch
):
    from diamond_feed import atomic

    state_path = tmp_path / "state.json"
    _save_records(state_path, diamond_records[:1])
    destinations = {
        state_path: state_path.read_text(encoding="utf-8"),
        tmp_path / "ai_summary_feed.xml": "old feed",
        tmp_path / "ai_summary.html": "old html",
        tmp_path / "ai_usage.json": "[]\n",
    }
    for path, contents in destinations.items():
        path.write_text(contents, encoding="utf-8")
    real_replace = atomic.os.replace
    html_path = (tmp_path / "ai_summary.html").resolve()

    def fail_html_publication(source, destination):
        if Path(source).name == "ai_summary.html.tmp" and Path(destination) == html_path:
            raise OSError("HTML publication failed")
        return real_replace(source, destination)

    monkeypatch.setattr(atomic.os, "replace", fail_html_publication)
    with pytest.raises(OSError, match="HTML publication failed"):
        run_summary(app_config, state_path, RecordingClient(), NOW, output_dir=tmp_path)

    for path, contents in destinations.items():
        assert path.read_text(encoding="utf-8") == contents
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob("*.bak"))


def test_output_topology_collision_is_rejected_before_any_ai_call(
    tmp_path, diamond_records, app_config
):
    state_path = tmp_path / "ai_summary.html"
    _save_records(state_path, diamond_records[:1])
    client = RecordingClient()

    with pytest.raises(ValueError, match="destinations must be unique"):
        run_summary(app_config, state_path, client, NOW, output_dir=tmp_path)

    assert client.payloads == []


def test_cli_without_api_key_fails_safely(capsys, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    assert summarize.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "DEEPSEEK_API_KEY is required\n"
