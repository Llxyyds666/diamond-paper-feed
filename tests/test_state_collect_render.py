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

    monkeypatch.setattr("diamond_feed.atomic.os.fsync", lambda fd: calls.append(("fsync", fd)) or real_fsync(fd))
    monkeypatch.setattr(
        "diamond_feed.atomic.os.replace",
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


def test_default_empty_rss_registry_is_readable():
    from diamond_feed.collect import _read_sources

    assert _read_sources(Path("config/rss_sources.tsv")) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(extra=True),
        lambda payload: payload["papers"][next(iter(payload["papers"]))].update(extra=True),
        lambda payload: payload["papers"][next(iter(payload["papers"]))].update(authors="AB"),
        lambda payload: payload["papers"][next(iter(payload["papers"]))].update(ai_relevant="false"),
        lambda payload: payload["papers"][next(iter(payload["papers"]))].update(ai_confidence=True),
        lambda payload: payload["papers"][next(iter(payload["papers"]))].update(published_at="2026-09-01T00:00:00"),
        lambda payload: payload.update(pending_ai=[next(iter(payload["papers"])), next(iter(payload["papers"]))]),
        lambda payload: payload.update(pending_ai=["doi:10.1000/missing"]),
        lambda payload: payload.update(source_watermarks={"openalex": "2026-09-01T00:00:00"}),
    ],
    ids=[
        "unknown-root-field",
        "unknown-paper-field",
        "string-authors",
        "string-bool",
        "bool-as-number",
        "naive-paper-time",
        "duplicate-pending",
        "missing-pending-paper",
        "naive-watermark",
    ],
)
def test_load_state_rejects_non_strict_schema_and_broken_invariants(tmp_path, mutate):
    from diamond_feed.state import load_state

    record = _paper("Diamond strict schema", "abstract", "10.1000/strict", 1)
    key = record_key(record)
    payload = {"version": 1, "papers": {key: record.to_dict()}, "pending_ai": [key], "source_watermarks": {}}
    mutate(payload)
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid state"):
        load_state(path)


def test_load_state_rejects_noncanonical_paper_key_and_normalizes_watermark_to_utc(tmp_path):
    from diamond_feed.state import load_state

    record = _paper("Diamond canonical key", "abstract", "10.1000/canonical", 1)
    payload = {
        "version": 1,
        "papers": {"wrong": record.to_dict()},
        "pending_ai": [],
        "source_watermarks": {"openalex": "2026-09-01T08:00:00+08:00"},
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="key"):
        load_state(path)

    payload["papers"] = {record_key(record): record.to_dict()}
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_state(path).source_watermarks["openalex"] == "2026-09-01T00:00:00+00:00"


def test_save_state_rejects_invalid_in_memory_state_before_replacing(tmp_path):
    from diamond_feed.state import FeedState, save_state

    path = tmp_path / "state.json"
    path.write_text("old state", encoding="utf-8")
    record = _paper("Diamond invalid state", "abstract", "10.1000/invalid-state", 1)
    state = FeedState(papers={"wrong": record}, pending_ai=["wrong"], source_watermarks={})

    with pytest.raises(ValueError, match="key"):
        save_state(path, state)

    assert path.read_text(encoding="utf-8") == "old state"
    assert not list(tmp_path.glob("*.tmp"))


def test_save_state_reports_invalid_runtime_field_as_validation_error(tmp_path):
    from diamond_feed.state import FeedState, save_state

    record = _paper("Diamond invalid DOI", "abstract", "10.1000/invalid-doi", 1)
    key = record_key(record)
    record.doi = 123  # type: ignore[assignment]

    with pytest.raises(ValueError, match="doi"):
        save_state(tmp_path / "state.json", FeedState(papers={key: record}, pending_ai=[key], source_watermarks={}))

    assert not list(tmp_path.glob("*.tmp"))


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


def test_raw_rss_removes_xml_1_0_forbidden_characters_from_all_text():
    from diamond_feed.render import render_rss

    record = _paper("Diamond\x00 title\ud800", "abstract\x08\x0b\x0c\x1f￾\nkept", "10.1000/control", 1)
    record.authors = ["A\x00 Author"]
    record.journal = "Journal\x00"
    record.url = "https://example.test/\x00paper"
    xml = render_rss([record], "Feed\x00\ud800", "https://example.test/\x08", 1)

    root = ElementTree.fromstring(xml)

    assert root.findtext("./channel/title") == "Feed"
    assert root.findtext("./channel/item/title") == "Diamond title"
    assert root.findtext("./channel/item/description") == "abstract\nkept"
    for forbidden in ("\x00", "\x08", "\x0b", "\x0c", "\x1f", "￾", "\ud800"):
        assert forbidden not in xml


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


def test_collection_state_staging_failure_preserves_old_outputs_and_cleans_temps(tmp_path, monkeypatch):
    from diamond_feed import collect
    from diamond_feed.state import FeedState, save_state

    paths = _successful_collection(tmp_path, monkeypatch)
    config, sources, queries, state_path, feed_path, failures = paths
    save_state(state_path, FeedState.empty())
    old_state = state_path.read_text(encoding="utf-8")
    feed_path.write_text("old feed", encoding="utf-8")
    monkeypatch.setattr(collect, "stage_state", lambda *args: (_ for _ in ()).throw(OSError("state stage failed")), raising=False)

    with pytest.raises(OSError, match="state stage failed"):
        collect.main(_collection_args(paths), now=lambda: datetime(2026, 9, 5, tzinfo=timezone.utc))

    assert state_path.read_text(encoding="utf-8") == old_state
    assert feed_path.read_text(encoding="utf-8") == "old feed"
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob("*.bak"))


@pytest.mark.parametrize("failed_destination", ["feed.xml", "state.json"])
def test_collection_commit_failure_rolls_back_both_outputs_and_cleans_artifacts(tmp_path, monkeypatch, failed_destination):
    from diamond_feed import atomic, collect
    from diamond_feed.state import FeedState, save_state

    paths = _successful_collection(tmp_path, monkeypatch)
    config, sources, queries, state_path, feed_path, failures = paths
    save_state(state_path, FeedState.empty())
    old_state = state_path.read_text(encoding="utf-8")
    old_feed = "old feed"
    feed_path.write_text(old_feed, encoding="utf-8")
    real_replace = os.replace

    def fail_selected_commit(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path.name.endswith(".tmp") and destination_path.name == failed_destination:
            raise OSError(f"{failed_destination} commit failed")
        return real_replace(source, destination)

    monkeypatch.setattr(atomic.os, "replace", fail_selected_commit)

    with pytest.raises(OSError, match="commit failed"):
        collect.main(_collection_args(paths), now=lambda: datetime(2026, 9, 5, tzinfo=timezone.utc))

    assert state_path.read_text(encoding="utf-8") == old_state
    assert feed_path.read_text(encoding="utf-8") == old_feed
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob("*.bak"))


def _paper(title, abstract, doi, day, source="rss"):
    return PaperRecord(title=title, abstract=abstract, authors=["A. Author"], journal="Diamond Journal", published_at=datetime(2026, 9, day, tzinfo=timezone.utc), doi=doi, url=f"https://doi.org/{doi}", sources=[source], source_ids=[f"{source}:{doi}"])


def _collection_paths(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(Path("paper_feed_config.json").read_text(encoding="utf-8"), encoding="utf-8")
    sources = tmp_path / "sources.tsv"
    queries = tmp_path / "queries.json"
    queries.write_text(Path("config/queries.json").read_text(encoding="utf-8"), encoding="utf-8")
    return config, sources, queries, tmp_path / "state.json", tmp_path / "feed.xml", tmp_path / "failures.tsv"


def _successful_collection(tmp_path, monkeypatch):
    from diamond_feed import collect

    paths = _collection_paths(tmp_path)
    paths[1].write_text("name\tcategory\turl\n", encoding="utf-8")
    record = _paper("Diamond transaction", "abstract", "10.1000/transaction", 1)
    monkeypatch.setattr(collect, "_collect_scholarly", lambda *args, **kwargs: [(args[0], [record], None)])
    return paths


def _collection_args(paths):
    config, sources, queries, state_path, feed_path, failures = paths
    return ["--config", str(config), "--state", str(state_path), "--sources", str(sources), "--queries", str(queries), "--feed", str(feed_path), "--failures", str(failures)]
