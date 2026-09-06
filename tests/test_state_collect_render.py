from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from xml.etree import ElementTree

import pytest

from diamond_feed.models import PaperRecord, SourceFailure
from diamond_feed.normalize import record_key


def test_atomic_state_round_trip_and_queue_dedup(tmp_path, diamond_records, query_rules):
    from diamond_feed.collect import merge_into_state
    from diamond_feed.state import FeedState, load_state, save_state

    state = FeedState.empty()
    stats = merge_into_state(state, diamond_records + diamond_records, query_rules)
    assert stats.added == len(diamond_records)
    assert len(state.pending_ai) == len(diamond_records)
    path = tmp_path / "state.json"
    save_state(path, state)
    loaded = load_state(path)
    assert list(loaded.papers) == list(state.papers)
    assert not list(tmp_path.glob("*.tmp"))


def test_save_state_flushes_fsyncs_and_replaces_sibling_temp(tmp_path, monkeypatch):
    from diamond_feed.state import FeedState, save_state

    path = tmp_path / "state.json"
    calls = []
    real_fsync = os.fsync
    real_replace = os.replace

    monkeypatch.setattr("diamond_feed.state.os.fsync", lambda fd: calls.append(("fsync", fd)) or real_fsync(fd))
    monkeypatch.setattr(
        "diamond_feed.state.os.replace",
        lambda source, target: calls.append(("replace", Path(source), Path(target))) or real_replace(source, target),
    )

    save_state(path, FeedState.empty())

    assert calls[0][0] == "fsync"
    assert calls[1] == ("replace", path.with_suffix(".json.tmp"), path)
    assert not path.with_suffix(".json.tmp").exists()


@pytest.mark.parametrize("payload", [{"version": 2, "papers": {}, "pending_ai": [], "source_watermarks": {}}, {"version": "1"}])
def test_load_state_rejects_unsupported_or_malformed_version(tmp_path, payload):
    from diamond_feed.state import load_state

    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        load_state(path)


def test_merge_deduplicates_and_keeps_pending_queue_oldest_first(query_rules):
    from diamond_feed.collect import merge_into_state
    from diamond_feed.state import FeedState

    older = _paper("Older diamond study", "abstract", "10.1000/older", 1)
    newer = _paper("Newer diamond study", "abstract", "10.1000/newer", 2)
    duplicate = _paper("Older diamond study", "a longer abstract", "10.1000/older", 1, source="crossref")
    state = FeedState.empty()

    stats = merge_into_state(state, [newer, older, duplicate], query_rules)

    assert stats.added == 2
    assert stats.merged == 1
    assert state.pending_ai == [record_key(older), record_key(newer)]
    assert state.papers[record_key(older)].sources == ["rss", "crossref"]


def test_processed_record_only_requeues_when_abstract_becomes_nonempty(query_rules):
    from diamond_feed.collect import merge_into_state
    from diamond_feed.state import FeedState

    empty = _paper("Diamond detector", "", "10.1000/detector", 1)
    key = record_key(empty)
    empty.ai_relevant = False
    state = FeedState(papers={key: empty}, pending_ai=[], source_watermarks={})

    merge_into_state(state, [_paper("Diamond detector", "", "10.1000/detector", 1, source="openalex")], query_rules)
    assert state.pending_ai == []
    merge_into_state(state, [_paper("Diamond detector", "New experimental abstract", "10.1000/detector", 1, source="crossref")], query_rules)
    assert state.pending_ai == [key]


def test_raw_rss_is_valid_sorted_escaped_and_bounded(diamond_records):
    from diamond_feed.render import render_rss

    special = _paper("Diamond < & >", "A < B & C", "10.1000/special", 3)
    xml = render_rss(diamond_records * 1001 + [special], "Diamond & Feed", "https://example.test?a=1&b=2", 2000)
    root = ElementTree.fromstring(xml)
    items = root.findall("./channel/item")
    dc = "{http://purl.org/dc/elements/1.1/}"

    assert len(items) == 2000
    assert items[0].findtext("title") == "Diamond < & >"
    assert "&lt;" in xml and "&amp;" in xml
    assert items[0].findtext(dc + "creator") == "A. Author"
    assert items[0].findtext(dc + "source") == "Diamond Journal"
    assert items[0].findtext(dc + "identifier") == "10.1000/special"
    assert any(item.findtext("guid") == "doi:10.1000/diamond.1" for item in items)


def test_collection_isolates_adapters_persists_success_and_reports_failures(tmp_path, monkeypatch, query_rules):
    from diamond_feed import collect
    from diamond_feed.state import FeedState, load_state, save_state

    config, sources, queries, state_path, feed_path, failures = _collection_paths(tmp_path)
    queries.write_text(json.dumps({"include_any": ["diamond"], "material_context": ["diamond"], "obvious_noise": []}), encoding="utf-8")
    sources.write_text("name\tcategory\turl\nA\tjournal\thttps://one.test/rss\nB\tjournal\thttps://two.test/rss\n", encoding="utf-8")
    seen = []

    def fake_rss(url, fetcher):
        seen.append(url)
        if "one" in url:
            return [], SourceFailure(datetime(2026, 9, 4, tzinfo=timezone.utc), "timeout", url, "slow")
        return [_paper("Diamond success", "abstract", "10.1000/success", 4)], None

    monkeypatch.setattr(collect, "collect_rss", fake_rss)
    monkeypatch.setattr(collect, "_collect_scholarly", lambda *args, **kwargs: [])
    now = datetime(2026, 9, 5, tzinfo=timezone.utc)
    assert collect.main(["--config", str(config), "--state", str(state_path), "--sources", str(sources), "--queries", str(queries), "--feed", str(feed_path), "--failures", str(failures)], now=lambda: now) == 0

    assert seen == ["https://one.test/rss", "https://two.test/rss"]
    assert "10.1000/success" in feed_path.read_text(encoding="utf-8")
    assert "timeout" in failures.read_text(encoding="utf-8")
    loaded = load_state(state_path)
    assert loaded.source_watermarks["rss:https://two.test/rss"] == now.isoformat()
    assert loaded.pending_ai == ["doi:10.1000/success"]


def test_collection_uses_watermark_or_30_day_bootstrap_and_all_failure_preserves_outputs(tmp_path, monkeypatch):
    from diamond_feed import collect
    from diamond_feed.state import FeedState, save_state

    config, sources, queries, state_path, feed_path, failures = _collection_paths(tmp_path)
    sources.write_text("name\tcategory\turl\nA\tjournal\thttps://one.test/rss\n", encoding="utf-8")
    existing = FeedState.empty()
    existing.source_watermarks["openalex"] = "2026-09-01T00:00:00+00:00"
    save_state(state_path, existing)
    feed_path.write_text("old feed", encoding="utf-8")
    requested_dates = []

    monkeypatch.setattr(collect, "collect_rss", lambda url, fetcher: ([], SourceFailure(datetime.now(timezone.utc), "timeout", url, "slow")))
    monkeypatch.setattr(collect, "_collect_scholarly", lambda name, query, from_date, *args: requested_dates.append((name, from_date)) or [(name, [], SourceFailure(datetime.now(timezone.utc), "timeout", name, "slow"))])
    now = datetime(2026, 9, 5, tzinfo=timezone.utc)

    result = collect.main(["--config", str(config), "--state", str(state_path), "--sources", str(sources), "--queries", str(queries), "--feed", str(feed_path), "--failures", str(failures)], now=lambda: now)

    assert result == 2
    assert feed_path.read_text(encoding="utf-8") == "old feed"
    assert json.loads(state_path.read_text(encoding="utf-8"))["source_watermarks"]["openalex"] == "2026-09-01T00:00:00+00:00"
    assert failures.exists()
    assert ("openalex", datetime(2026, 9, 1, tzinfo=timezone.utc).date()) in requested_dates
    assert ("crossref", (now - timedelta(days=30)).date()) in requested_dates


def _paper(title, abstract, doi, day, source="rss"):
    return PaperRecord(title=title, abstract=abstract, authors=["A. Author"], journal="Diamond Journal", published_at=datetime(2026, 9, day, tzinfo=timezone.utc), doi=doi, url=f"https://doi.org/{doi}", sources=[source], source_ids=[f"{source}:{doi}"])


def _collection_paths(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(Path("paper_feed_config.json").read_text(encoding="utf-8"), encoding="utf-8")
    sources = tmp_path / "sources.tsv"
    queries = tmp_path / "queries.json"
    queries.write_text(Path("config/queries.json").read_text(encoding="utf-8"), encoding="utf-8")
    return config, sources, queries, tmp_path / "state.json", tmp_path / "feed.xml", tmp_path / "failures.tsv"
