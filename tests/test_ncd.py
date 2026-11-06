from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import importlib.util
import json

import pytest

from diamond_feed.ai import DeepSeekClient, RequestCounter
from diamond_feed.enrich import EnrichmentStats
from diamond_feed.normalize import record_key
from diamond_feed.state import FeedState, save_state


NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)


def ncd_module():
    return importlib.import_module("diamond_feed.ncd")


def test_ncd_tracking_module_exists():
    assert importlib.util.find_spec("diamond_feed.ncd") is not None


def paper(diamond_records, index=1, **changes):
    return replace(
        diamond_records[0],
        **{
            "title": f"Nanocrystalline diamond film growth {index}",
            "abstract": "We deposit continuous NCD films and measure their properties.",
            "doi": f"10.1000/ncd.{index}",
            "url": f"https://doi.org/10.1000/ncd.{index}",
            **changes,
        },
    )


def store(path, records):
    save_state(path, FeedState(papers={record_key(p): p for p in records}))


class ModelTransport:
    def __init__(self, *, rejected=(), outcomes=()):
        self.calls = []
        self.rejected = set(rejected)
        self.outcomes = list(outcomes)

    def __call__(self, url, headers, payload, timeout):
        self.calls.append(payload)
        if self.outcomes:
            raw = self.outcomes.pop(0)
            if isinstance(raw, Exception):
                raise raw
        else:
            papers = json.loads(payload["messages"][-1]["content"])["papers"]
            raw = {
                "decisions": [
                    {
                        "key": p["key"],
                        "is_ncd": p["key"] not in self.rejected,
                        "summary_zh": "实验研究 NCD 薄膜的生长。" if p["abstract"] else "仅据标题，缺少摘要。",
                        "reason": "研究对象为纳米晶金刚石连续膜。" if p["key"] not in self.rejected else "仅研究分散的纳米金刚石颗粒。",
                    }
                    for p in papers
                ]
            }
        return {
            "choices": [{"message": {"content": json.dumps(raw)}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        }


def client(ai_config, transport):
    return DeepSeekClient("test-secret", ai_config, transport=transport, wait=lambda _: None)


@pytest.mark.parametrize("title,abstract", [
    ("Growth of nano-crystalline diamond", ""),
    ("Ultra-nano-crystalline diamond films", ""),
    ("纳米晶金刚石薄膜低温生长", ""),
    ("超纳米晶金刚石的电极应用", ""),
    ("NCD films as heat spreaders", ""),
    ("UNCD based microelectrodes", ""),
    ("Diamond films", "20 nm crystallites form a continuous film."),
    ("Nanodiamond seeded coatings", "Surface preparation before diamond growth."),
])
def test_prefilter_recalls_ncd_variants(diamond_records, title, abstract):
    assert ncd_module().is_ncd_candidate(paper(diamond_records, title=title, abstract=abstract))


def test_prefilter_does_not_match_substrings_in_unrelated_words(diamond_records):
    assert not ncd_module().is_ncd_candidate(paper(
        diamond_records, title="UNCDisease classification", abstract="Clinical diagnosis."
    ))


def test_full_original_abstract_and_untrusted_data_rule_are_sent(ai_config, diamond_records):
    abstract = "A" * 5000 + " END_OF_ORIGINAL_ABSTRACT"
    record = paper(diamond_records, abstract=abstract)
    transport = ModelTransport()
    decisions = ncd_module().screen_ncd_batch([record], client(ai_config, transport), ai_config, RequestCounter())
    prompt = transport.calls[0]["messages"]
    assert json.loads(prompt[-1]["content"])["papers"][0]["abstract"] == abstract
    assert "untrusted" in prompt[0]["content"]
    assert "particles" in prompt[0]["content"]
    assert decisions[0]["is_ncd"] is True


@pytest.mark.parametrize("raw", [
    {"decisions": []},
    {"decisions": [{"key": "forged", "is_ncd": True, "summary_zh": "摘要", "reason": "理由"}]},
    {"decisions": [{"key": "doi:10.1000/ncd.1", "is_ncd": "true", "summary_zh": "摘要", "reason": "理由"}]},
    {"decisions": [{"key": "doi:10.1000/ncd.1", "is_ncd": True, "summary_zh": "", "reason": "理由"}]},
])
def test_malformed_decisions_retry_without_accepting_partial_batch(ai_config, diamond_records, raw):
    transport = ModelTransport(outcomes=[raw] * 3)
    counter = RequestCounter()
    with pytest.raises(ValueError, match="invalid NCD model response"):
        ncd_module().screen_ncd_batch([paper(diamond_records)], client(ai_config, transport), ai_config, counter)
    assert counter.used == 3


def test_initialize_baselines_history_without_model_or_push(tmp_path, diamond_records):
    state_path, watch_path = tmp_path / "state.json", tmp_path / "watch.json"
    record = paper(diamond_records)
    store(state_path, [record])
    ncd_module().initialize_watch(state_path, watch_path, NOW)
    watch = ncd_module().load_watch(watch_path)
    assert record_key(record) in watch["baseline_aliases"]
    assert watch["outbox"] == watch["reviews"] == {}
    assert watch["usage"] == []
    with pytest.raises(ValueError, match="already initialized"):
        ncd_module().initialize_watch(state_path, watch_path, NOW)


def initialized(tmp_path, diamond_records):
    state_path, watch_path = tmp_path / "state.json", tmp_path / "watch.json"
    old = paper(diamond_records, 0)
    store(state_path, [old])
    ncd_module().initialize_watch(state_path, watch_path, NOW)
    return state_path, watch_path, old


def test_all_directions_selected_and_particle_only_rejected(tmp_path, diamond_records, app_config):
    state_path, watch_path, old = initialized(tmp_path, diamond_records)
    records = [paper(diamond_records, i, title=title) for i, title in enumerate([
        "NCD nucleation", "UNCD electrochemistry", "NCD thermal conductivity",
        "NCD quantum sensors", "Nanodiamond particles for drug delivery"
    ], 1)]
    store(state_path, [old, *records])
    transport = ModelTransport(rejected=[record_key(records[-1])])
    stats = ncd_module().run_screening(app_config, state_path, watch_path, client(app_config.ai, transport), NOW)
    watch = ncd_module().load_watch(watch_path)
    assert (stats.processed, stats.selected, stats.failed) == (5, 4, False)
    assert len(watch["outbox"]) == 4
    assert len(watch["reviews"]) == 5
    assert watch["usage"][0]["requests"] == 1
    assert watch["usage"][0]["total_tokens"] == 150
    assert "test-secret" not in watch_path.read_text(encoding="utf-8")


def test_ncd_is_not_subject_to_general_100_candidate_limit(tmp_path, diamond_records, app_config):
    state_path, watch_path, old = initialized(tmp_path, diamond_records)
    records = [paper(diamond_records, i) for i in range(1, 106)]
    store(state_path, [old, *records])
    transport = ModelTransport()
    model = client(app_config.ai, transport)
    stats = ncd_module().run_screening(app_config, state_path, watch_path, model, NOW)
    assert stats.processed == stats.selected == 105
    assert stats.requests == 11
    assert len(ncd_module().load_watch(watch_path)["outbox"]) == 105
    again = ncd_module().run_screening(app_config, state_path, watch_path, model, NOW)
    assert again.processed == again.requests == 0
    assert len(transport.calls) == 11


def test_partial_model_failure_keeps_unscreened_for_next_run(tmp_path, diamond_records, app_config, capsys):
    state_path, watch_path, old = initialized(tmp_path, diamond_records)
    store(state_path, [old, *[paper(diamond_records, i) for i in range(1, 12)]])
    transport = ModelTransport(outcomes=[RuntimeError("test-secret")])
    stats = ncd_module().run_screening(app_config, state_path, watch_path, client(app_config.ai, transport), NOW)
    assert stats.failed is True
    watch = ncd_module().load_watch(watch_path)
    assert len(watch["reviews"]) == len(watch["outbox"]) == 1
    transport = ModelTransport()
    retry = ncd_module().run_screening(app_config, state_path, watch_path, client(app_config.ai, transport), NOW + timedelta(hours=6))
    assert retry.processed == 10
    assert len(ncd_module().load_watch(watch_path)["outbox"]) == 11
    assert "test-secret" not in capsys.readouterr().err


def test_rejected_paper_with_new_abstract_is_reconsidered(tmp_path, diamond_records, app_config):
    state_path, watch_path, old = initialized(tmp_path, diamond_records)
    record = paper(diamond_records, abstract="")
    store(state_path, [old, record])
    first = ModelTransport(rejected=[record_key(record)])
    ncd_module().run_screening(app_config, state_path, watch_path, client(app_config.ai, first), NOW)
    record.abstract = "Continuous nanocrystalline diamond film growth was measured."
    store(state_path, [old, record])
    second = ModelTransport()
    stats = ncd_module().run_screening(app_config, state_path, watch_path, client(app_config.ai, second), NOW)
    assert stats.selected == 1


def test_aliases_and_doi_upgrade_do_not_duplicate_notification(tmp_path, diamond_records, app_config):
    state_path, watch_path, old = initialized(tmp_path, diamond_records)
    record = paper(diamond_records, doi=None)
    store(state_path, [old, record])
    model = client(app_config.ai, ModelTransport())
    ncd_module().run_screening(app_config, state_path, watch_path, model, NOW)
    upgraded = replace(record, doi="10.1000/new-doi", url="https://doi.org/10.1000/new-doi")
    store(state_path, [old, upgraded])
    stats = ncd_module().run_screening(app_config, state_path, watch_path, model, NOW)
    assert stats.processed == 0
    assert len(ncd_module().load_watch(watch_path)["outbox"]) == 1


def test_original_abstract_enrichment_is_scoped_to_ncd_candidates(tmp_path, diamond_records, app_config):
    state_path, watch_path, old = initialized(tmp_path, diamond_records)
    record = paper(diamond_records, abstract="")
    store(state_path, [old, record, paper(diamond_records, 3, title="Diamond quantum bulk crystal", abstract="")])

    class Enricher:
        def enrich(self, state, keys):
            assert set(state.papers) == set(keys) == {record_key(record)}
            state.papers[record_key(record)].abstract = "ENRICHED original NCD abstract"
            return EnrichmentStats(enriched=1)

    transport = ModelTransport()
    ncd_module().run_screening(app_config, state_path, watch_path, client(app_config.ai, transport), NOW, enricher=Enricher())
    request = json.loads(transport.calls[0]["messages"][-1]["content"])
    assert request["papers"][0]["abstract"] == "ENRICHED original NCD abstract"


def test_repository_assets_never_enter_ncd_screening(tmp_path, diamond_records, app_config):
    state_path, watch_path, old = initialized(tmp_path, diamond_records)
    store(state_path, [old, paper(diamond_records, doi="10.71947/arim.ncd-dataset")])
    transport = ModelTransport()
    stats = ncd_module().run_screening(app_config, state_path, watch_path, client(app_config.ai, transport), NOW)
    assert stats.processed == stats.requests == 0


def test_pending_bark_messages_are_independent_durable_and_only_retry_failures(tmp_path, diamond_records, app_config):
    state_path, watch_path, old = initialized(tmp_path, diamond_records)
    records = [paper(diamond_records, i) for i in (1, 2)]
    store(state_path, [old, *records])
    ncd_module().run_screening(app_config, state_path, watch_path, client(app_config.ai, ModelTransport()), NOW)
    calls = []

    def sender(token, plan):
        assert token == "bark-test-secret"
        assert len(plan.messages) == 1
        calls.append(plan.messages[0])
        return (len(calls) != 1,)

    first = ncd_module().deliver_pending(watch_path, "bark-test-secret", NOW, sender=sender)
    assert first.accepted == first.failed == 1
    watch = ncd_module().load_watch(watch_path)
    assert sum(item["sent_at"] is not None for item in watch["outbox"].values()) == 1
    assert all("DOI：" in message.body and "摘要：" in message.body for message in calls)
    assert {message.url for message in calls} == {p.url for p in records}
    second = ncd_module().deliver_pending(watch_path, "bark-test-secret", NOW, sender=sender)
    assert (second.accepted, second.failed) == (1, 0)
    assert len(calls) == 3
    third = ncd_module().deliver_pending(watch_path, "bark-test-secret", NOW, sender=sender)
    assert third.accepted == third.failed == 0
    assert len(calls) == 3
    assert "bark-test-secret" not in watch_path.read_text(encoding="utf-8")


def test_missing_bark_token_preserves_outbox(tmp_path, diamond_records, app_config):
    state_path, watch_path, old = initialized(tmp_path, diamond_records)
    store(state_path, [old, paper(diamond_records)])
    ncd_module().run_screening(app_config, state_path, watch_path, client(app_config.ai, ModelTransport()), NOW)
    before = watch_path.read_bytes()
    with pytest.raises(ValueError, match="BARK_TOKEN"):
        ncd_module().deliver_pending(watch_path, "", NOW)
    assert watch_path.read_bytes() == before


def test_corrupt_watch_is_not_reset(tmp_path):
    path = tmp_path / "watch.json"
    path.write_text('{"version":1,"outbox":{}}', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid NCD watch"):
        ncd_module().load_watch(path)
    assert path.read_text(encoding="utf-8") == '{"version":1,"outbox":{}}'


def test_smoke_test_is_single_paper_marked_and_does_not_replay_history(tmp_path, diamond_records, app_config):
    state_path, watch_path, old = initialized(tmp_path, diamond_records)
    stats = ncd_module().run_screening(app_config, state_path, watch_path, client(app_config.ai, ModelTransport()), NOW, smoke_paper_key=record_key(old))
    watch = ncd_module().load_watch(watch_path)
    assert stats.processed == stats.selected == 1
    assert watch["reviews"] == {}
    assert len(watch["outbox"]) == 1
    assert "测试" in next(iter(watch["outbox"].values()))["title"]


def test_collection_prefilter_keeps_chinese_and_ncd_acronyms(diamond_records, query_rules):
    from diamond_feed.filtering import matches_rules
    for title in ("纳米晶金刚石薄膜生长", "NCD growth on GaN", "UNCD electrodes"):
        assert matches_rules(paper(diamond_records, title=title, abstract=""), query_rules)
    assert not matches_rules(paper(diamond_records, title="UNCDisease", abstract=""), query_rules)


def test_rejected_doi_upgrade_updates_review_without_repeated_model_requests(tmp_path, diamond_records, app_config):
    state_path, watch_path, old = initialized(tmp_path, diamond_records)
    record = paper(diamond_records, doi=None, abstract="Nanodiamond particles in suspension.")
    store(state_path, [old, record])
    ncd_module().run_screening(app_config, state_path, watch_path, client(app_config.ai, ModelTransport(rejected=[record_key(record)])), NOW)
    upgraded = replace(record, doi="10.1000/upgraded", abstract="A different experiment with isolated nanodiamond particles.")
    store(state_path, [old, upgraded])
    model = client(app_config.ai, ModelTransport(rejected=[record_key(upgraded)]))
    ncd_module().run_screening(app_config, state_path, watch_path, model, NOW)
    again = ncd_module().run_screening(app_config, state_path, watch_path, model, NOW)
    assert again.requests == 0
    assert len(ncd_module().load_watch(watch_path)["reviews"]) == 1


def test_cli_constructs_clients_from_environment_without_exposing_secrets(tmp_path, diamond_records, app_config, monkeypatch, capsys):
    module = ncd_module()
    state_path, watch_path, old = initialized(tmp_path, diamond_records)
    store(state_path, [old, paper(diamond_records)])
    seen = {}

    def factory(key, config):
        seen["deepseek"] = key
        return client(config, ModelTransport())

    def enricher(key, **kwargs):
        seen["semantic"] = key
        return None

    monkeypatch.setattr(module, "DeepSeekClient", factory)
    monkeypatch.setattr(module, "AbstractEnricher", enricher)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-secret")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "semantic-test-secret")
    assert module.main(["--screen", "--state", str(state_path), "--watch", str(watch_path)]) == 0
    assert seen == {"deepseek": "deepseek-test-secret", "semantic": "semantic-test-secret"}
    captured = capsys.readouterr()
    assert "test-secret" not in captured.out + captured.err


def test_cli_requires_watch_initialization_without_implicitly_baselining_new_work(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    watch = tmp_path / "missing-watch.json"
    assert ncd_module().main(["--screen", "--watch", str(watch)]) == 1
    assert not watch.exists()


def test_same_doi_is_not_pushed_again_after_metadata_update(tmp_path, diamond_records, app_config):
    state_path, watch_path, old = initialized(tmp_path, diamond_records)
    record = paper(diamond_records)
    store(state_path, [old, record])
    model = client(app_config.ai, ModelTransport())
    ncd_module().run_screening(app_config, state_path, watch_path, model, NOW)
    record.title += " revised title"
    record.abstract += " Additional experimental evidence."
    store(state_path, [old, record])
    assert ncd_module().run_screening(app_config, state_path, watch_path, model, NOW).requests == 0


def test_invalid_screening_batch_is_retried_on_next_run_and_usage_survives(tmp_path, diamond_records, app_config):
    state_path, watch_path, old = initialized(tmp_path, diamond_records)
    store(state_path, [old, paper(diamond_records)])
    first = client(app_config.ai, ModelTransport(outcomes=[{"decisions": []}] * 3))
    stats = ncd_module().run_screening(app_config, state_path, watch_path, first, NOW)
    watch = ncd_module().load_watch(watch_path)
    assert stats.failed is True
    assert watch["reviews"] == watch["outbox"] == {}
    assert watch["usage"][0]["requests"] == 3
    second = client(app_config.ai, ModelTransport())
    assert ncd_module().run_screening(app_config, state_path, watch_path, second, NOW).selected == 1
    assert ncd_module().load_watch(watch_path)["usage"][0]["requests"] == 4
