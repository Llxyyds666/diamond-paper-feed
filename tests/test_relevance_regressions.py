from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from diamond_feed.ai import DeepSeekClient, RequestBudget, screen_batch
from diamond_feed.collect import merge_into_state
from diamond_feed.evaluate import run_evaluation
from diamond_feed.normalize import record_key
from diamond_feed.state import FeedState, load_state, save_state
from diamond_feed.summarize import run_summary


def copies(record):
    first = replace(record, doi="10.48550/arxiv.2608.08152", url="https://doi.org/10.48550/arXiv.2608.08152")
    second = replace(record, doi=None, url="https://arxiv.org/abs/2608.08152v2", sources=["arxiv"], source_ids=["arxiv:2608.08152v2"])
    return first, second


def test_cross_source_duplicates_are_screened_and_published_once(tmp_path, diamond_records, app_config, fake_deepseek_client):
    records = copies(diamond_records[0])
    state_path = tmp_path / "state.json"
    save_state(state_path, FeedState(papers={record_key(p): p for p in records}, pending_ai=[record_key(p) for p in records]))
    stats = run_summary(app_config, state_path, fake_deepseek_client, datetime.now(timezone.utc), output_dir=tmp_path)
    assert (stats.candidates, stats.processed, stats.selected) == (1, 1, 1)
    result = load_state(state_path)
    assert not result.pending_ai
    assert all(p.ai_relevant is True for p in result.papers.values())
    assert (tmp_path / "ai_summary_feed.xml").read_text(encoding="utf-8").count("<item>") == 1


def test_collector_merges_arxiv_alias_and_keeps_provenance(diamond_records, query_rules):
    first, second = copies(diamond_records[0])
    state = FeedState(papers={record_key(second): second}, pending_ai=[record_key(second)])
    merge_into_state(state, [first], query_rules)
    assert len(state.papers) == len(state.pending_ai) == 1
    result = next(iter(state.papers.values()))
    assert set(result.sources) == {"rss", "arxiv"}
    assert state.pending_ai == [record_key(result)]


def test_same_title_distinct_dois_are_not_silently_collapsed(tmp_path, diamond_records, app_config, fake_deepseek_client):
    first, second = diamond_records
    second = replace(second, title=first.title)
    path = tmp_path / "state.json"
    save_state(path, FeedState(papers={record_key(p): p for p in [first, second]}, pending_ai=[record_key(p) for p in [first, second]]))
    stats = run_summary(app_config, path, fake_deepseek_client, datetime.now(timezone.utc), output_dir=tmp_path)
    assert stats.selected == 2


def test_positive_decision_cannot_explicitly_say_it_is_unrelated(ai_config, diamond_records):
    decision = dict(key=record_key(diamond_records[0]), relevant=True, confidence=.95,
                    category="other-diamond", matched_topics=["diamond"],
                    summary_zh="这是一篇数学论文，与金刚石材料无关。", reason="只涉及抽象几何。")
    client = DeepSeekClient("test-api-key", ai_config, transport=lambda *args: {"choices": [{"message": {"content": json.dumps({"decisions": [decision]})}}]})
    with pytest.raises(ValueError, match="invalid model response"):
        screen_batch(diamond_records[:1], client, ai_config, RequestBudget(1))


def test_baseline_replay_uses_saved_inputs_not_current_queue(tmp_path, configured_state_with_100_pending, app_config, fake_deepseek_client, diamond_records):
    baseline = tmp_path / "baseline.json"
    papers = [dict(key=record_key(p), title=p.title, doi=p.doi, url=p.url,
                   published_at=p.published_at.isoformat(), journal=p.journal,
                   input_abstract="Original abstract.", ai_relevant=False) for p in copies(diamond_records[0])]
    baseline.write_text(json.dumps({"papers": papers}), encoding="utf-8")
    original = configured_state_with_100_pending.read_bytes()
    report = run_evaluation(app_config, configured_state_with_100_pending, fake_deepseek_client,
                            datetime.now(timezone.utc), output_dir=tmp_path / "replay", baseline_path=baseline)
    assert [p["key"] for p in report["papers"]] == [p["key"] for p in papers]
    assert all(p["input_abstract"] == "Original abstract." for p in report["papers"])
    assert report["stats"]["processed"] == 1
    assert report["input_records"] == 2
    assert report["duplicates_removed"] == 1
    assert configured_state_with_100_pending.read_bytes() == original


def test_arxiv_versions_without_doi_keep_a_persistable_representative(tmp_path, diamond_records, app_config, fake_deepseek_client):
    first = replace(diamond_records[0], doi=None, url="https://arxiv.org/abs/2608.08152v1", title="Diamond growth", published_at=datetime(2025, 12, 1, tzinfo=timezone.utc))
    second = replace(first, url="https://arxiv.org/pdf/2608.08152v2.pdf", title="Diamond growth at high temperature", published_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    path = tmp_path / "state.json"
    save_state(path, FeedState(papers={record_key(p): p for p in [first, second]}, pending_ai=[record_key(p) for p in [first, second]]))
    stats = run_summary(app_config, path, fake_deepseek_client, datetime.now(timezone.utc), output_dir=tmp_path)
    assert stats.processed == 1
    assert not load_state(path).pending_ai


def test_known_processed_alias_does_not_spend_budget_again_on_collection(diamond_records, query_rules):
    first, second = copies(diamond_records[0])
    first.ai_relevant = True
    first.summary_zh = "已有审核结论。"
    state = FeedState(papers={record_key(first): first})
    merge_into_state(state, [second], query_rules)
    assert not state.pending_ai
    assert next(iter(state.papers.values())).ai_relevant is True


def test_alias_enrichment_requeues_when_previously_missing_abstract(diamond_records, query_rules):
    first, second = copies(diamond_records[0])
    first.abstract = ""
    first.ai_relevant = False
    state = FeedState(papers={record_key(first): first})
    merge_into_state(state, [second], query_rules)
    assert len(state.papers) == len(state.pending_ai) == 1


def test_figshare_version_aliases_share_decision_but_different_ids_do_not(tmp_path, diamond_records, app_config, fake_deepseek_client):
    first = replace(diamond_records[0], doi="10.6084/m9.figshare.12345")
    records = [first, replace(first, doi=first.doi + ".v1"), replace(first, doi="10.6084/m9.figshare.12346")]
    path = tmp_path / "state.json"
    save_state(path, FeedState(papers={record_key(p): p for p in records}, pending_ai=[record_key(p) for p in records]))
    stats = run_summary(app_config, path, fake_deepseek_client, datetime.now(timezone.utc), output_dir=tmp_path)
    assert stats.processed == stats.selected == 2
    assert not load_state(path).pending_ai


def test_failure_keeps_all_aliases_pending(tmp_path, diamond_records, app_config, failing_deepseek_client):
    records = copies(diamond_records[0])
    path = tmp_path / "state.json"
    keys = [record_key(p) for p in records]
    save_state(path, FeedState(papers=dict(zip(keys, records)), pending_ai=keys))
    original = path.read_bytes()
    stats = run_summary(app_config, path, failing_deepseek_client, datetime.now(timezone.utc), output_dir=tmp_path)
    assert stats.failed and stats.requests == 1
    assert path.read_bytes() == original
