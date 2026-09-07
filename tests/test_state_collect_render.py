from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
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


def test_save_state_returns_publication_result(tmp_path):
    from diamond_feed.state import FeedState, save_state

    result = save_state(tmp_path / "state.json", FeedState.empty())

    assert result.committed is True
    assert result.status == "committed"


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


@pytest.mark.parametrize(
    "field",
    ["title", "abstract", "authors", "journal", "doi", "url", "sources", "source_ids", "categories", "summary_zh"],
)
def test_load_state_rejects_unpaired_surrogate_in_every_persisted_paper_string(tmp_path, field):
    from diamond_feed.state import load_state

    record = _paper("Diamond surrogate", "abstract", "10.1000/surrogate", 1)
    key = record_key(record)
    paper = record.to_dict()
    if field in {"authors", "sources", "source_ids", "categories"}:
        paper[field] = ["bad\ud800value"]
    else:
        paper[field] = "bad\ud800value"
    payload = {"version": 1, "papers": {key: paper}, "pending_ai": [], "source_watermarks": {}}
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError) as raised:
        load_state(path)
    assert raised.value.__cause__ is not None
    assert "surrogate" in str(raised.value.__cause__)


def test_save_state_rejects_unpaired_surrogate_before_writing(tmp_path):
    from diamond_feed.state import FeedState, save_state

    record = _paper("Diamond surrogate", "abstract", "10.1000/save-surrogate", 1)
    key = record_key(record)
    record.summary_zh = "bad\udfffvalue"

    with pytest.raises(ValueError, match="surrogate"):
        save_state(tmp_path / "state.json", FeedState(papers={key: record}, pending_ai=[], source_watermarks={}))

    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("location", ["paper-key", "pending-key", "watermark-key", "watermark-value"])
def test_load_state_rejects_unpaired_surrogate_in_state_keys_and_watermarks(tmp_path, location):
    from diamond_feed.state import load_state

    record = _paper("Diamond state surrogate", "abstract", "10.1000/state-surrogate", 1)
    key = record_key(record)
    payload = {"version": 1, "papers": {key: record.to_dict()}, "pending_ai": [], "source_watermarks": {}}
    if location == "paper-key":
        payload["papers"] = {key + "\ud800": record.to_dict()}
    elif location == "pending-key":
        payload["pending_ai"] = [key + "\ud800"]
    elif location == "watermark-key":
        payload["source_watermarks"] = {"open\ud800alex": "2026-09-01T00:00:00+00:00"}
    else:
        payload["source_watermarks"] = {"openalex": "2026-09-01\ud800T00:00:00+00:00"}
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError) as raised:
        load_state(path)
    assert raised.value.__cause__ is not None
    assert "surrogate" in str(raised.value.__cause__)


def test_load_state_wraps_oversized_integer_confidence_as_value_error(tmp_path):
    from diamond_feed.state import load_state

    record = _paper("Diamond confidence", "abstract", "10.1000/confidence", 1)
    key = record_key(record)
    paper = record.to_dict()
    paper["ai_confidence"] = 10**1000
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 1, "papers": {key: paper}, "pending_ai": [], "source_watermarks": {}}), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid state.*confidence"):
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


def test_collection_staging_aggregates_stage_and_previous_temp_cleanup_failures(tmp_path, monkeypatch):
    from diamond_feed import collect
    from diamond_feed.atomic import PublicationOperationError
    from diamond_feed.state import FeedState, save_state

    paths = _successful_collection(tmp_path, monkeypatch)
    config, sources, queries, state_path, feed_path, failures = paths
    save_state(state_path, FeedState.empty())
    state_temporary = state_path.with_name("state.json.tmp")
    original_unlink = Path.unlink

    def fail_feed_stage(path, _contents):
        assert path == feed_path
        raise PublicationOperationError(
            "feed stage write failed",
            (f"remove feed temporary {feed_path.with_name('feed.xml.tmp').resolve()}: locked",),
        )

    def fail_previous_stage_cleanup(path, *args, **kwargs):
        if path == state_temporary:
            raise PermissionError("state temporary cleanup failed")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(collect, "stage_text", fail_feed_stage)
    monkeypatch.setattr(Path, "unlink", fail_previous_stage_cleanup)

    with pytest.raises(PublicationOperationError) as raised:
        collect.main(
            _collection_args(paths),
            now=lambda: datetime(2026, 9, 5, tzinfo=timezone.utc),
        )

    assert "feed stage write failed" in raised.value.primary_failure
    assert any("remove feed temporary" in failure for failure in raised.value.cleanup_failures)
    assert any("state temporary cleanup failed" in failure for failure in raised.value.cleanup_failures)
    assert state_temporary.exists()


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


def test_successful_commit_reports_cleanup_pending_without_rolling_back(tmp_path, monkeypatch):
    from diamond_feed.atomic import commit_staged, stage_text

    state_path = tmp_path / "state.json"
    feed_path = tmp_path / "feed.xml"
    state_path.write_text("old state", encoding="utf-8")
    feed_path.write_text("old feed", encoding="utf-8")
    original_unlink = Path.unlink

    def fail_backup_cleanup(path, *args, **kwargs):
        if path.name.endswith(".bak"):
            raise PermissionError("backup locked")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_backup_cleanup)
    staged = [stage_text(state_path, "new state"), stage_text(feed_path, "new feed")]

    result = commit_staged(staged)

    assert result.committed is True
    assert result.status == "committed-with-cleanup-pending"
    assert result.cleanup_pending is True
    assert result.cleanup_errors
    assert state_path.read_text(encoding="utf-8") == "new state"
    assert feed_path.read_text(encoding="utf-8") == "new feed"
    assert list(tmp_path.glob("*.bak"))
    assert not list(tmp_path.glob("*.tmp"))


def test_warning_filters_cannot_turn_completed_commit_into_exception(tmp_path, monkeypatch):
    import warnings

    from diamond_feed.atomic import commit_staged, stage_text

    path = tmp_path / "state.json"
    path.write_text("old state", encoding="utf-8")
    original_unlink = Path.unlink

    def fail_backup_cleanup(target, *args, **kwargs):
        if target.name.endswith(".bak"):
            raise PermissionError("backup locked")
        return original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_backup_cleanup)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = commit_staged([stage_text(path, "new state")])

    assert result.committed is True
    assert result.status == "committed-with-cleanup-pending"
    assert path.read_text(encoding="utf-8") == "new state"


def test_next_commit_cleans_stale_committed_backup_and_continues(tmp_path, monkeypatch):
    from diamond_feed.atomic import commit_staged, stage_text

    path = tmp_path / "state.json"
    path.write_text("version one", encoding="utf-8")
    original_unlink = Path.unlink
    fail_cleanup = True

    def fail_once_enabled(target, *args, **kwargs):
        if fail_cleanup and target.name.endswith(".bak"):
            raise PermissionError("backup locked")
        return original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_once_enabled)
    first_result = commit_staged([stage_text(path, "version two")])
    assert first_result.status == "committed-with-cleanup-pending"
    assert list(tmp_path.glob("*.bak"))

    fail_cleanup = False
    result = commit_staged([stage_text(path, "version three")])

    assert result.committed is True
    assert result.status == "committed"
    assert result.cleanup_pending is False
    assert path.read_text(encoding="utf-8") == "version three"
    assert not list(tmp_path.glob("*.bak"))
    assert not list(tmp_path.glob("*.committed"))


def test_completion_manifest_is_single_and_published_after_all_destinations(tmp_path, monkeypatch):
    from diamond_feed import atomic

    state_path = tmp_path / "state.json"
    feed_path = tmp_path / "feed.xml"
    state_path.write_text("old state", encoding="utf-8")
    feed_path.write_text("old feed", encoding="utf-8")
    original_replace = os.replace
    replacements = []

    def record_replacements(source, destination):
        replacements.append((Path(source), Path(destination)))
        return original_replace(source, destination)

    monkeypatch.setattr(atomic.os, "replace", record_replacements)
    result = atomic.commit_staged(
        [atomic.stage_text(state_path, "new state"), atomic.stage_text(feed_path, "new feed")]
    )

    manifest_replacements = [
        pair for pair in replacements if pair[1].name.startswith(".diamond-feed-publication.")
    ]
    assert result.status == "committed"
    assert len(manifest_replacements) == 1
    manifest_index = replacements.index(manifest_replacements[0])
    assert replacements.index((state_path.with_name("state.json.tmp"), state_path)) < manifest_index
    assert replacements.index((feed_path.with_name("feed.xml.tmp"), feed_path)) < manifest_index
    assert not any(path.name.endswith(".bak.committed") for pair in replacements for path in pair)


def test_duplicate_destination_validation_aggregates_all_staged_cleanup_failures(tmp_path, monkeypatch):
    from diamond_feed import atomic

    destination = tmp_path / "state.json"
    first_temporary = tmp_path / "first.tmp"
    second_temporary = tmp_path / "second.tmp"
    first_temporary.write_text("first", encoding="utf-8")
    second_temporary.write_text("second", encoding="utf-8")
    original_unlink = Path.unlink

    def fail_second_cleanup(path, *args, **kwargs):
        if path == second_temporary:
            raise PermissionError("second staged cleanup failed")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_second_cleanup)
    staged = [
        atomic.StagedFile(destination, first_temporary),
        atomic.StagedFile(destination, second_temporary),
    ]

    with pytest.raises(atomic.PublicationOperationError) as raised:
        atomic.commit_staged(staged)

    assert "destinations must be unique" in raised.value.primary_failure
    assert any("second staged cleanup failed" in failure for failure in raised.value.cleanup_failures)
    assert not first_temporary.exists()
    assert second_temporary.exists()


def test_manifest_layout_validation_aggregates_all_staged_cleanup_failures(tmp_path, monkeypatch):
    from diamond_feed import atomic

    first = atomic.stage_text(tmp_path / "state.json", "state")
    second = atomic.stage_text(tmp_path / "feed.xml", "feed")
    original_unlink = Path.unlink

    monkeypatch.setattr(
        atomic,
        "_manifest_parent",
        lambda _destinations: (_ for _ in ()).throw(ValueError("manifest roots differ")),
    )

    def fail_second_cleanup(path, *args, **kwargs):
        if path == second.temporary:
            raise PermissionError("layout staged cleanup failed")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_second_cleanup)

    with pytest.raises(atomic.PublicationOperationError) as raised:
        atomic.commit_staged([first, second])

    assert "manifest roots differ" in raised.value.primary_failure
    assert any("layout staged cleanup failed" in failure for failure in raised.value.cleanup_failures)
    assert not first.temporary.exists()
    assert second.temporary.exists()


def test_legacy_partial_markers_never_authorize_recovery_backup_cleanup(tmp_path):
    from diamond_feed.atomic import commit_staged, stage_text

    state_path = tmp_path / "state.json"
    feed_path = tmp_path / "feed.xml"
    state_path.write_text("partially published state", encoding="utf-8")
    feed_path.write_text("old feed", encoding="utf-8")
    state_backup = tmp_path / "state.json.interrupted.bak"
    feed_backup = tmp_path / "feed.xml.interrupted.bak"
    state_backup.write_text("recoverable state", encoding="utf-8")
    feed_backup.write_text("recoverable feed", encoding="utf-8")
    (tmp_path / "state.json.interrupted.bak.committed").write_text("committed\n", encoding="utf-8")
    staged = [stage_text(state_path, "next state"), stage_text(feed_path, "next feed")]

    with pytest.raises(RuntimeError, match="recovery"):
        commit_staged(staged)

    assert state_backup.read_text(encoding="utf-8") == "recoverable state"
    assert feed_backup.read_text(encoding="utf-8") == "recoverable feed"
    assert not list(tmp_path.glob("*.tmp"))


def test_manifest_must_exactly_describe_transaction_backups_before_cleanup(tmp_path, monkeypatch):
    from diamond_feed.atomic import commit_staged, stage_text

    state_path = tmp_path / "state.json"
    feed_path = tmp_path / "feed.xml"
    state_path.write_text("old state", encoding="utf-8")
    feed_path.write_text("old feed", encoding="utf-8")
    original_unlink = Path.unlink
    cleanup_locked = True

    def retain_backups(path, *args, **kwargs):
        if cleanup_locked and path.name.endswith(".bak"):
            raise PermissionError("backup locked")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", retain_backups)
    first_result = commit_staged(
        [stage_text(state_path, "new state"), stage_text(feed_path, "new feed")]
    )
    manifests = list(tmp_path.glob(".diamond-feed-publication.*.json"))
    backups = sorted(tmp_path.glob("*.bak"))
    assert first_result.cleanup_pending is True
    assert len(manifests) == 1
    assert len(backups) == 2

    payload = json.loads(manifests[0].read_text(encoding="utf-8"))
    payload["backups"] = payload["backups"][:1]
    manifests[0].write_text(json.dumps(payload), encoding="utf-8")
    cleanup_locked = False

    with pytest.raises(RuntimeError, match="recovery"):
        commit_staged([stage_text(state_path, "later state"), stage_text(feed_path, "later feed")])

    assert all(path.exists() for path in backups)


def test_preflight_retries_orphan_manifest_cleanup_without_touching_invalid_manifest(tmp_path, monkeypatch):
    from diamond_feed import atomic

    path = tmp_path / "state.json"
    path.write_text("version one", encoding="utf-8")
    invalid_manifest = tmp_path / (".diamond-feed-publication." + "f" * 32 + ".json")
    invalid_manifest.write_text("not a manifest", encoding="utf-8")
    unrelated_destination = tmp_path / "unrelated.json"
    unrelated_manifest = tmp_path / (".diamond-feed-publication." + "e" * 32 + ".json")
    unrelated_manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "transaction_id": "e" * 32,
                "destinations": [str(unrelated_destination.resolve())],
                "backups": [],
            }
        ),
        encoding="utf-8",
    )
    original_unlink = Path.unlink
    manifest_cleanup_locked = True

    def fail_manifest_cleanup(target, *args, **kwargs):
        if (
            manifest_cleanup_locked
            and target.name.startswith(".diamond-feed-publication.")
            and target != invalid_manifest
        ):
            raise PermissionError("orphan manifest cleanup failed")
        return original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_manifest_cleanup)
    first_result = atomic.commit_staged([atomic.stage_text(path, "version two")])
    valid_manifests = [
        manifest
        for manifest in tmp_path.glob(".diamond-feed-publication.*.json")
        if manifest not in {invalid_manifest, unrelated_manifest}
    ]
    assert first_result.cleanup_pending is True
    assert len(valid_manifests) == 1
    assert not list(tmp_path.glob("*.bak"))

    manifest_cleanup_locked = False
    original_backup_candidates = atomic._backup_candidates

    def reject_unrelated_directory_scan(destination):
        if destination.resolve() == unrelated_destination.resolve():
            raise AssertionError("unrelated manifest destination was scanned")
        return original_backup_candidates(destination)

    monkeypatch.setattr(atomic, "_backup_candidates", reject_unrelated_directory_scan)
    second_result = atomic.commit_staged([atomic.stage_text(path, "version three")])

    assert second_result.status == "committed"
    assert not valid_manifests[0].exists()
    assert invalid_manifest.read_text(encoding="utf-8") == "not a manifest"
    assert unrelated_manifest.exists()


def test_manifest_creation_primary_and_temp_cleanup_failures_become_commit_debt(tmp_path, monkeypatch):
    from diamond_feed import atomic

    path = tmp_path / "state.json"
    path.write_text("old state", encoding="utf-8")
    original_replace = os.replace
    original_unlink = Path.unlink

    def fail_manifest_publish(source, destination):
        if Path(destination).name.startswith(".diamond-feed-publication."):
            raise OSError("manifest publish failed")
        return original_replace(source, destination)

    def fail_manifest_temp_cleanup(target, *args, **kwargs):
        if target.name.startswith(".diamond-feed-publication.") and target.name.endswith(".tmp"):
            raise PermissionError("manifest temp cleanup failed")
        return original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(atomic.os, "replace", fail_manifest_publish)
    monkeypatch.setattr(Path, "unlink", fail_manifest_temp_cleanup)
    result = atomic.commit_staged([atomic.stage_text(path, "new state")])

    assert result.status == "committed-with-cleanup-pending"
    assert any("manifest publish failed" in error for error in result.cleanup_errors)
    assert any("manifest temp cleanup failed" in error for error in result.cleanup_errors)
    assert path.read_text(encoding="utf-8") == "new state"
    backups = list(tmp_path.glob("*.bak"))
    assert len(backups) == 1

    monkeypatch.setattr(atomic.os, "replace", original_replace)
    monkeypatch.setattr(Path, "unlink", original_unlink)
    with pytest.raises(RuntimeError, match="recovery"):
        atomic.commit_staged([atomic.stage_text(path, "later state")])
    assert backups[0].exists()


def test_stage_text_preserves_primary_and_cleanup_failures(tmp_path, monkeypatch):
    from diamond_feed import atomic

    path = tmp_path / "state.json"
    temporary = tmp_path / "state.json.tmp"
    original_unlink = Path.unlink

    monkeypatch.setattr(atomic.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("stage fsync failed")))

    def fail_stage_cleanup(target, *args, **kwargs):
        if target == temporary:
            raise PermissionError("stage temp cleanup failed")
        return original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_stage_cleanup)

    with pytest.raises(RuntimeError) as raised:
        atomic.stage_text(path, "new state")

    assert type(raised.value).__name__ == "PublicationOperationError"
    assert "stage fsync failed" in raised.value.primary_failure
    assert any("stage temp cleanup failed" in error for error in raised.value.cleanup_failures)
    assert temporary.exists()


def test_backup_preserves_copy_and_temp_cleanup_failures(tmp_path, monkeypatch):
    from diamond_feed import atomic

    path = tmp_path / "state.json"
    path.write_text("old state", encoding="utf-8")
    staged = atomic.stage_text(path, "new state")
    original_unlink = Path.unlink

    monkeypatch.setattr(
        atomic.shutil,
        "copyfileobj",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("backup copy failed")),
    )

    def fail_backup_temp_cleanup(target, *args, **kwargs):
        if target.name.endswith(".bak.tmp"):
            raise PermissionError("backup temp cleanup failed")
        return original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_backup_temp_cleanup)

    with pytest.raises(RuntimeError) as raised:
        atomic.commit_staged([staged])

    assert type(raised.value).__name__ == "PublicationOperationError"
    assert "backup copy failed" in raised.value.primary_failure
    assert any("backup temp cleanup failed" in error for error in raised.value.cleanup_failures)
    assert path.read_text(encoding="utf-8") == "old state"


def test_preflight_cleanup_debt_is_preserved_when_publish_fails_and_rollback_succeeds(tmp_path, monkeypatch):
    from diamond_feed import atomic

    path = tmp_path / "state.json"
    path.write_text("version one", encoding="utf-8")
    original_unlink = Path.unlink
    original_replace = os.replace
    retained_backups = set()

    def retain_selected_backups(target, *args, **kwargs):
        if target in retained_backups or (not retained_backups and target.name.endswith(".bak")):
            retained_backups.add(target)
            raise PermissionError("stale backup cleanup failed")
        return original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", retain_selected_backups)
    first_result = atomic.commit_staged([atomic.stage_text(path, "version two")])
    assert first_result.cleanup_pending is True
    assert len(retained_backups) == 1

    def fail_next_publish(source, destination):
        if Path(source) == path.with_name("state.json.tmp") and Path(destination) == path:
            raise OSError("next publish failed")
        return original_replace(source, destination)

    monkeypatch.setattr(atomic.os, "replace", fail_next_publish)

    with pytest.raises(RuntimeError) as raised:
        atomic.commit_staged([atomic.stage_text(path, "version three")])

    assert type(raised.value).__name__ == "PublicationOperationError"
    assert "next publish failed" in raised.value.primary_failure
    assert any("stale backup cleanup failed" in error for error in raised.value.cleanup_failures)
    assert path.read_text(encoding="utf-8") == "version two"


def test_preflight_aggregates_authorized_cleanup_unresolved_backup_and_staged_cleanup(tmp_path, monkeypatch):
    from diamond_feed import atomic

    completed_path = tmp_path / "completed.json"
    unresolved_path = tmp_path / "unresolved.json"
    completed_path.write_text("version one", encoding="utf-8")
    unresolved_path.write_text("current partial", encoding="utf-8")
    original_unlink = Path.unlink
    retained = []

    def retain_first_backup(target, *args, **kwargs):
        if target.name.endswith(".bak") and (not retained or target == retained[0]):
            if not retained:
                retained.append(target)
            raise PermissionError("authorized backup cleanup failed")
        return original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", retain_first_backup)
    first_result = atomic.commit_staged([atomic.stage_text(completed_path, "version two")])
    assert first_result.cleanup_pending is True
    unresolved_backup = tmp_path / "unresolved.json.interrupted.bak"
    unresolved_backup.write_text("recoverable unresolved", encoding="utf-8")
    completed_stage = atomic.stage_text(completed_path, "version three")
    unresolved_stage = atomic.stage_text(unresolved_path, "next partial")

    def fail_authorized_and_staged_cleanup(target, *args, **kwargs):
        if target == retained[0]:
            raise PermissionError("authorized backup cleanup failed")
        if target == unresolved_stage.temporary:
            raise PermissionError("unresolved staged cleanup failed")
        return original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_authorized_and_staged_cleanup)

    with pytest.raises(atomic.PublicationOperationError) as raised:
        atomic.commit_staged([completed_stage, unresolved_stage])

    assert "unresolved publication backup" in raised.value.primary_failure
    assert any("authorized backup cleanup failed" in failure for failure in raised.value.cleanup_failures)
    assert any("unresolved staged cleanup failed" in failure for failure in raised.value.cleanup_failures)
    assert unresolved_backup.exists()
    assert not completed_stage.temporary.exists()
    assert unresolved_stage.temporary.exists()


def test_unresolved_recovery_backup_blocks_publish_but_discards_new_stage(tmp_path):
    from diamond_feed.atomic import commit_staged, stage_text

    path = tmp_path / "state.json"
    path.write_text("possibly partial state", encoding="utf-8")
    backup = tmp_path / "state.json.interrupted.bak"
    backup.write_text("last known good state", encoding="utf-8")
    staged = stage_text(path, "another state")

    with pytest.raises(RuntimeError, match=str(backup.resolve()).replace("\\", "\\\\")):
        commit_staged([staged])

    assert path.read_text(encoding="utf-8") == "possibly partial state"
    assert backup.read_text(encoding="utf-8") == "last known good state"
    assert not list(tmp_path.glob("*.tmp"))


def test_rollback_diagnostics_collect_restore_and_other_backup_cleanup_failures(tmp_path, monkeypatch):
    from diamond_feed import atomic

    state_path = tmp_path / "state.json"
    feed_path = tmp_path / "feed.xml"
    state_path.write_text("old state", encoding="utf-8")
    feed_path.write_text("old feed", encoding="utf-8")
    staged = [atomic.stage_text(state_path, "new state"), atomic.stage_text(feed_path, "new feed")]
    original_replace = os.replace
    original_unlink = Path.unlink

    def fail_publish_and_restore(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path.name.endswith(".tmp") and destination_path == feed_path:
            raise OSError("feed publish failed")
        if source_path.name.endswith(".bak") and destination_path == state_path:
            raise OSError("state restore failed")
        return original_replace(source, destination)

    def fail_unused_feed_backup_cleanup(path, *args, **kwargs):
        if path.name.startswith("feed.xml") and path.name.endswith(".bak"):
            raise PermissionError("feed backup cleanup failed")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(atomic.os, "replace", fail_publish_and_restore)
    monkeypatch.setattr(Path, "unlink", fail_unused_feed_backup_cleanup)

    with pytest.raises(atomic.PublicationRollbackError) as raised:
        atomic.commit_staged(staged)

    message = str(raised.value)
    backups = sorted(tmp_path.glob("*.bak"))
    assert "feed publish failed" in message
    assert "state restore failed" in message
    assert "feed backup cleanup failed" in message
    assert len(backups) == 2
    assert all(str(path.resolve()) in message for path in backups)
    assert not list(tmp_path.glob("*.tmp"))


def test_rollback_diagnostics_include_preflight_completed_backup_cleanup_failure(tmp_path, monkeypatch):
    from diamond_feed import atomic

    state_path = tmp_path / "state.json"
    feed_path = tmp_path / "feed.xml"
    state_path.write_text("old state", encoding="utf-8")
    feed_path.write_text("old feed", encoding="utf-8")
    original_replace = os.replace
    original_unlink = Path.unlink
    retained_backups = []

    def fail_stale_cleanup(path, *args, **kwargs):
        if path.name.endswith(".bak") and (not retained_backups or path == retained_backups[0]):
            if not retained_backups:
                retained_backups.append(path)
            raise PermissionError("stale completed backup cleanup failed")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_stale_cleanup)
    first_result = atomic.commit_staged([atomic.stage_text(state_path, "committed state")])
    assert first_result.cleanup_pending is True
    stale_backup = retained_backups[0]
    staged = [atomic.stage_text(state_path, "new state"), atomic.stage_text(feed_path, "new feed")]

    def fail_publish_and_restore(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path.name.endswith(".tmp") and destination_path == feed_path:
            raise OSError("feed publish failed")
        if source_path.name.endswith(".bak") and source_path != stale_backup and destination_path == state_path:
            raise OSError("state restore failed")
        return original_replace(source, destination)

    monkeypatch.setattr(atomic.os, "replace", fail_publish_and_restore)

    with pytest.raises(atomic.PublicationRollbackError) as raised:
        atomic.commit_staged(staged)

    assert "stale completed backup cleanup failed" in str(raised.value)
    assert any("stale completed backup cleanup failed" in failure for failure in raised.value.rollback_failures)
    assert stale_backup not in raised.value.recovery_backups


def test_rollback_diagnostics_say_when_failed_removal_has_no_backup(tmp_path, monkeypatch):
    from diamond_feed import atomic

    new_path = tmp_path / "new-state.json"
    feed_path = tmp_path / "feed.xml"
    feed_path.write_text("old feed", encoding="utf-8")
    staged = [atomic.stage_text(new_path, "new state"), atomic.stage_text(feed_path, "new feed")]
    original_replace = os.replace
    original_unlink = Path.unlink

    def fail_feed_publish(source, destination):
        if Path(source).name.endswith(".tmp") and Path(destination) == feed_path:
            raise OSError("feed publish failed")
        return original_replace(source, destination)

    def fail_new_destination_removal(path, *args, **kwargs):
        if path == new_path:
            raise PermissionError("new destination removal failed")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(atomic.os, "replace", fail_feed_publish)
    monkeypatch.setattr(Path, "unlink", fail_new_destination_removal)

    with pytest.raises(atomic.PublicationRollbackError) as raised:
        atomic.commit_staged(staged)

    message = str(raised.value)
    assert "no backup" in message
    assert str(new_path.resolve()) in message
    assert "recoverable backups: none" in message


def test_rollback_diagnostics_say_when_expected_backup_is_no_longer_present(tmp_path, monkeypatch):
    from diamond_feed import atomic

    state_path = tmp_path / "state.json"
    feed_path = tmp_path / "feed.xml"
    state_path.write_text("old state", encoding="utf-8")
    feed_path.write_text("old feed", encoding="utf-8")
    staged = [atomic.stage_text(state_path, "new state"), atomic.stage_text(feed_path, "new feed")]
    original_replace = os.replace

    def lose_backup_during_restore(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path.name.endswith(".tmp") and destination_path == feed_path:
            raise OSError("feed publish failed")
        if source_path.name.endswith(".bak") and destination_path == state_path:
            source_path.unlink()
            raise OSError("state backup disappeared")
        return original_replace(source, destination)

    monkeypatch.setattr(atomic.os, "replace", lose_backup_during_restore)

    with pytest.raises(atomic.PublicationRollbackError) as raised:
        atomic.commit_staged(staged)

    assert raised.value.recovery_backups == ()
    assert raised.value.destinations_without_backup == (state_path.resolve(),)
    assert str(state_path.resolve()) in str(raised.value)
    assert "destinations with no backup" in str(raised.value)


def test_collection_reports_cleanup_debt_from_failure_log_and_publication(tmp_path, monkeypatch, capsys):
    from diamond_feed import collect
    from diamond_feed.atomic import CommitResult

    paths = _successful_collection(tmp_path, monkeypatch)
    real_atomic_write = collect.atomic_write_text
    real_commit = collect.commit_staged
    failure_artifact = tmp_path / "failures.previous.bak"
    publication_artifact = tmp_path / "state.previous.bak"

    def write_with_debt(path, contents):
        real_atomic_write(path, contents)
        return CommitResult(
            status="committed-with-cleanup-pending",
            cleanup_errors=(f"cleanup pending: {failure_artifact.resolve()}",),
        )

    def commit_with_debt(staged):
        real_commit(staged)
        return CommitResult(
            status="committed-with-cleanup-pending",
            cleanup_errors=(f"cleanup pending: {publication_artifact.resolve()}",),
        )

    monkeypatch.setattr(collect, "atomic_write_text", write_with_debt)
    monkeypatch.setattr(collect, "commit_staged", commit_with_debt)

    result = collect.main(
        _collection_args(paths),
        now=lambda: datetime(2026, 9, 5, tzinfo=timezone.utc),
    )
    stderr = capsys.readouterr().err

    assert result == 0
    assert stderr.count("committed-with-cleanup-pending") == 2
    assert str(failure_artifact.resolve()) in stderr
    assert str(publication_artifact.resolve()) in stderr


@pytest.mark.parametrize(
    ("state_name", "feed_name", "failure_name"),
    [
        ("x.tmp", "x", "failures.tsv"),
        ("x", "x.tmp", "failures.tsv"),
        ("out/state.json", "out/sub/../state.json", "failures.tsv"),
        ("state.json", "feed.xml", "state.json"),
        ("state.json", "feed.xml", "nested/../feed.xml"),
        ("state.json", "feed.xml", "state.json.tmp"),
        ("state.json", "feed.xml", "feed.xml.tmp"),
    ],
    ids=[
        "state-is-feed-temp",
        "feed-is-state-temp",
        "relative-destination-alias",
        "failure-is-state",
        "failure-is-feed-alias",
        "failure-is-state-temp",
        "failure-is-feed-temp",
    ],
)
def test_collection_rejects_unsafe_output_layout_before_writes_or_source_calls(
    tmp_path, monkeypatch, state_name, feed_name, failure_name
):
    from diamond_feed import collect

    config, sources, queries, _, _, _ = _collection_paths(tmp_path)
    state_path = tmp_path / state_name
    feed_path = tmp_path / feed_name
    failure_path = tmp_path / failure_name
    snapshots = {}
    for label, path in (("state", state_path), ("feed", feed_path), ("failure", failure_path)):
        normalized = Path(os.path.abspath(os.path.normpath(path)))
        normalized.parent.mkdir(parents=True, exist_ok=True)
        key = os.path.normcase(str(normalized))
        if key not in snapshots:
            normalized.write_text(f"old {label}", encoding="utf-8")
            snapshots[key] = (normalized, normalized.read_bytes())

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("output layout must be rejected before source collection")

    monkeypatch.setattr(collect, "_read_sources", unexpected_call)
    monkeypatch.setattr(collect, "collect_rss", unexpected_call)
    monkeypatch.setattr(collect, "_collect_scholarly", unexpected_call)

    with pytest.raises(ValueError, match="output.*(layout|path)|destination|temporary"):
        collect.main(
            [
                "--config", str(config),
                "--state", str(state_path),
                "--sources", str(sources),
                "--queries", str(queries),
                "--feed", str(feed_path),
                "--failures", str(failure_path),
            ],
            now=lambda: datetime(2026, 9, 5, tzinfo=timezone.utc),
        )

    for path, contents in snapshots.values():
        assert path.read_bytes() == contents


def test_commit_staged_rejects_resolved_destination_alias_and_cleans_all_temporaries(tmp_path):
    from diamond_feed import atomic

    (tmp_path / "sub").mkdir()
    destination = tmp_path / "state.json"
    first_temporary = tmp_path / "first.tmp"
    second_temporary = tmp_path / "second.tmp"
    destination.write_text("old state", encoding="utf-8")
    first_temporary.write_text("first", encoding="utf-8")
    second_temporary.write_text("second", encoding="utf-8")

    with pytest.raises(ValueError, match="destinations must be unique"):
        atomic.commit_staged(
            [
                atomic.StagedFile(destination, first_temporary),
                atomic.StagedFile(tmp_path / "sub" / ".." / "state.json", second_temporary),
            ]
        )

    assert destination.read_text(encoding="utf-8") == "old state"
    assert not first_temporary.exists()
    assert not second_temporary.exists()


@pytest.mark.parametrize("collision", ["duplicate-temporary", "temporary-destination"])
def test_commit_staged_rejects_temporary_topology_and_preserves_destinations(tmp_path, collision):
    from diamond_feed import atomic

    first_destination = tmp_path / "state.json"
    second_destination = tmp_path / "feed.xml"
    first_destination.write_text("old state", encoding="utf-8")
    second_destination.write_text("old feed", encoding="utf-8")
    first_temporary = tmp_path / "first.tmp"
    first_temporary.write_text("first", encoding="utf-8")
    second_temporary = first_temporary if collision == "duplicate-temporary" else first_destination
    if collision == "temporary-destination":
        items = [
            atomic.StagedFile(first_destination, tmp_path / "safe.tmp"),
            atomic.StagedFile(second_destination, second_temporary),
        ]
        items[0].temporary.write_text("safe", encoding="utf-8")
    else:
        items = [
            atomic.StagedFile(first_destination, first_temporary),
            atomic.StagedFile(second_destination, second_temporary),
        ]

    with pytest.raises(Exception, match="temporar"):
        atomic.commit_staged(items)

    assert first_destination.read_text(encoding="utf-8") == "old state"
    assert second_destination.read_text(encoding="utf-8") == "old feed"
    if collision == "duplicate-temporary":
        assert not first_temporary.exists()


def test_symlink_manifest_is_not_followed_or_used_to_delete_external_target(tmp_path, monkeypatch):
    from diamond_feed import atomic

    controlled = tmp_path / "controlled"
    external = tmp_path / "external"
    controlled.mkdir()
    external.mkdir()
    destination = controlled / "state.json"
    destination.write_text("old state", encoding="utf-8")
    transaction_id = "a" * 32
    link = controlled / f"{atomic.MANIFEST_PREFIX}{transaction_id}{atomic.MANIFEST_SUFFIX}"
    sentinel = external / "sentinel.json"
    sentinel.write_text(
        json.dumps(
            {
                "version": 1,
                "transaction_id": transaction_id,
                "destinations": [str(destination.resolve())],
                "backups": [],
            }
        ),
        encoding="utf-8",
    )
    _symlink_or_mock(link, sentinel, monkeypatch)

    atomic.commit_staged([atomic.stage_text(destination, "new state")])

    assert sentinel.exists()
    assert _lexists(link)
    assert destination.read_text(encoding="utf-8") == "new state"


def test_symlink_backup_is_retained_and_never_deletes_external_target(tmp_path, monkeypatch):
    from diamond_feed import atomic

    controlled = tmp_path / "controlled"
    external = tmp_path / "external"
    controlled.mkdir()
    external.mkdir()
    destination = controlled / "state.json"
    destination.write_text("current state", encoding="utf-8")
    transaction_id = "b" * 32
    backup = controlled / f"state.json.{transaction_id}.bak"
    sentinel = external / "sentinel.txt"
    sentinel.write_text("external data", encoding="utf-8")
    _symlink_or_mock(backup, sentinel, monkeypatch)
    manifest = controlled / f"{atomic.MANIFEST_PREFIX}{transaction_id}{atomic.MANIFEST_SUFFIX}"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "transaction_id": transaction_id,
                "destinations": [str(destination.resolve())],
                "backups": [str(backup.absolute())],
            }
        ),
        encoding="utf-8",
    )
    staged = atomic.stage_text(destination, "next state")

    with pytest.raises(RuntimeError, match="unresolved publication backup"):
        atomic.commit_staged([staged])

    assert sentinel.read_text(encoding="utf-8") == "external data"
    assert _lexists(backup)
    assert manifest.exists()
    assert destination.read_text(encoding="utf-8") == "current state"
    assert not staged.temporary.exists()


def test_manifest_paths_with_parent_traversal_never_authorize_backup_cleanup(tmp_path):
    from diamond_feed import atomic

    controlled = tmp_path / "controlled"
    (controlled / "sub").mkdir(parents=True)
    destination = controlled / "state.json"
    destination.write_text("current state", encoding="utf-8")
    transaction_id = "c" * 32
    backup = controlled / f"state.json.{transaction_id}.bak"
    backup.write_text("recoverable state", encoding="utf-8")
    manifest = controlled / f"{atomic.MANIFEST_PREFIX}{transaction_id}{atomic.MANIFEST_SUFFIX}"
    escaped_destination = controlled / "sub" / ".." / "state.json"
    escaped_backup = controlled / "sub" / ".." / backup.name
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "transaction_id": transaction_id,
                "destinations": [str(escaped_destination)],
                "backups": [str(escaped_backup)],
            }
        ),
        encoding="utf-8",
    )
    staged = atomic.stage_text(destination, "next state")

    with pytest.raises(RuntimeError, match="unresolved publication backup"):
        atomic.commit_staged([staged])

    assert backup.read_text(encoding="utf-8") == "recoverable state"
    assert manifest.exists()
    assert destination.read_text(encoding="utf-8") == "current state"
    assert not staged.temporary.exists()


def test_collection_rejects_real_linked_output_parent_before_adapter_or_write(
    tmp_path, monkeypatch
):
    from diamond_feed import collect

    config, sources, queries, _, _, _ = _collection_paths(tmp_path)
    controlled = tmp_path / "controlled"
    external = tmp_path / "external"
    controlled.mkdir()
    external.mkdir()
    linked_parent = controlled / "linked-output"
    _require_directory_symlink(linked_parent, external)
    state_path = linked_parent / "state.json"
    feed_path = controlled / "feed.xml"
    failure_path = controlled / "failures.tsv"
    external_state = external / "state.json"
    external_state.write_text(
        json.dumps(
            {"version": 1, "papers": {}, "pending_ai": [], "source_watermarks": {}}
        ),
        encoding="utf-8",
    )
    feed_path.write_text("old feed", encoding="utf-8")
    failure_path.write_text("old failures", encoding="utf-8")
    snapshots = {
        external_state: external_state.read_bytes(),
        feed_path: feed_path.read_bytes(),
        failure_path: failure_path.read_bytes(),
    }

    def unexpected_source_call(*_args, **_kwargs):
        raise AssertionError("linked output parent must be rejected before adapters")

    monkeypatch.setattr(collect, "_read_sources", unexpected_source_call)
    monkeypatch.setattr(collect, "_collect_scholarly", unexpected_source_call)

    with pytest.raises(ValueError, match="ancestor|reparse|link"):
        collect.main(
            [
                "--config", str(config),
                "--state", str(state_path),
                "--sources", str(sources),
                "--queries", str(queries),
                "--feed", str(feed_path),
                "--failures", str(failure_path),
            ]
        )

    for path, contents in snapshots.items():
        assert path.read_bytes() == contents


def test_commit_rejects_real_linked_manifest_parent_without_external_cleanup(tmp_path):
    from diamond_feed import atomic

    controlled = tmp_path / "controlled"
    external = tmp_path / "external-manifest-root"
    controlled.mkdir()
    external.mkdir()
    linked_parent = controlled / "publication"
    _require_directory_symlink(linked_parent, external)
    state_path = linked_parent / "state.json"
    feed_path = linked_parent / "feed.xml"
    external_state = external / "state.json"
    external_feed = external / "feed.xml"
    external_state.write_text("old state", encoding="utf-8")
    external_feed.write_text("old feed", encoding="utf-8")
    transaction_id = "d" * 32
    external_manifest = external / (
        f"{atomic.MANIFEST_PREFIX}{transaction_id}{atomic.MANIFEST_SUFFIX}"
    )
    external_manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "transaction_id": transaction_id,
                "destinations": [
                    str(Path(os.path.abspath(os.path.normpath(state_path)))),
                    str(Path(os.path.abspath(os.path.normpath(feed_path)))),
                ],
                "backups": [],
            }
        ),
        encoding="utf-8",
    )
    state_temporary = controlled / "state.stage"
    feed_temporary = controlled / "feed.stage"
    state_temporary.write_text("new state", encoding="utf-8")
    feed_temporary.write_text("new feed", encoding="utf-8")

    with pytest.raises(ValueError, match="ancestor|reparse|link"):
        atomic.commit_staged(
            [
                atomic.StagedFile(state_path, state_temporary),
                atomic.StagedFile(feed_path, feed_temporary),
            ]
        )

    assert external_manifest.exists()
    assert external_state.read_text(encoding="utf-8") == "old state"
    assert external_feed.read_text(encoding="utf-8") == "old feed"
    assert not state_temporary.exists()
    assert not feed_temporary.exists()


def test_commit_rejects_real_linked_backup_ancestor_without_external_cleanup(tmp_path):
    from diamond_feed import atomic

    controlled = tmp_path / "controlled"
    external = tmp_path / "external-backup-root"
    controlled.mkdir()
    external.mkdir()
    linked_parent = controlled / "linked-feed"
    _require_directory_symlink(linked_parent, external)
    state_path = controlled / "state.json"
    feed_path = linked_parent / "feed.xml"
    state_path.write_text("old state", encoding="utf-8")
    external_feed = external / "feed.xml"
    external_feed.write_text("old feed", encoding="utf-8")
    transaction_id = "e" * 32
    external_backup = external / f"feed.xml.{transaction_id}.bak"
    external_backup.write_text("external recovery sentinel", encoding="utf-8")
    manifest = controlled / (
        f"{atomic.MANIFEST_PREFIX}{transaction_id}{atomic.MANIFEST_SUFFIX}"
    )
    lexical_state = Path(os.path.abspath(os.path.normpath(state_path)))
    lexical_feed = Path(os.path.abspath(os.path.normpath(feed_path)))
    lexical_backup = Path(os.path.abspath(os.path.normpath(linked_parent / external_backup.name)))
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "transaction_id": transaction_id,
                "destinations": [str(lexical_state), str(lexical_feed)],
                "backups": [str(lexical_backup)],
            }
        ),
        encoding="utf-8",
    )
    state_temporary = controlled / "state.stage"
    feed_temporary = controlled / "feed.stage"
    state_temporary.write_text("new state", encoding="utf-8")
    feed_temporary.write_text("new feed", encoding="utf-8")

    with pytest.raises(ValueError, match="ancestor|reparse|link"):
        atomic.commit_staged(
            [
                atomic.StagedFile(state_path, state_temporary),
                atomic.StagedFile(feed_path, feed_temporary),
            ]
        )

    assert external_backup.read_text(encoding="utf-8") == "external recovery sentinel"
    assert external_feed.read_text(encoding="utf-8") == "old feed"
    assert manifest.exists()
    assert not state_temporary.exists()
    assert not feed_temporary.exists()


def test_output_layout_rejects_mocked_windows_reparse_ancestor(tmp_path, monkeypatch):
    from diamond_feed import atomic

    controlled = tmp_path / "controlled"
    reparse_parent = controlled / "junction"
    reparse_parent.mkdir(parents=True)
    original_lstat = Path.lstat
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    class ReparseMetadata:
        def __init__(self, wrapped):
            self._wrapped = wrapped
            self.st_mode = wrapped.st_mode
            self.st_file_attributes = (
                getattr(wrapped, "st_file_attributes", 0) | reparse_attribute
            )

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

    def report_reparse_directory(path, *args, **kwargs):
        metadata = original_lstat(path, *args, **kwargs)
        if path == reparse_parent:
            return ReparseMetadata(metadata)
        return metadata

    monkeypatch.setattr(Path, "lstat", report_reparse_directory)

    with pytest.raises(ValueError, match="reparse|link"):
        atomic.validate_output_layout(
            [
                reparse_parent / "state.json",
                controlled / "feed.xml",
                controlled / "failures.tsv",
            ]
        )


def test_stage_text_rejects_real_linked_parent_without_external_write(tmp_path):
    from diamond_feed import atomic

    controlled = tmp_path / "controlled"
    external = tmp_path / "external-stage-root"
    controlled.mkdir()
    external.mkdir()
    linked_parent = controlled / "linked-stage"
    _require_directory_symlink(linked_parent, external)
    destination = linked_parent / "feed.xml"
    external_temporary = external / "feed.xml.tmp"
    external_temporary.write_text("external staged sentinel", encoding="utf-8")

    with pytest.raises(ValueError, match="ancestor|reparse|link"):
        atomic.stage_text(destination, "new feed")

    assert external_temporary.read_text(encoding="utf-8") == "external staged sentinel"
    assert not (external / "feed.xml").exists()


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


def _lexists(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _symlink_or_mock(link, target, monkeypatch):
    try:
        link.symlink_to(target)
        return
    except OSError:
        link.write_text("simulated symlink", encoding="utf-8")

    original_lstat = Path.lstat
    original_read_text = Path.read_text

    def simulated_lstat(path, *args, **kwargs):
        result = original_lstat(path, *args, **kwargs)
        if path == link:
            return os.stat_result((stat.S_IFLNK | 0o777, *result[1:]))
        return result

    def simulated_read_text(path, *args, **kwargs):
        if path == link:
            return original_read_text(target, *args, **kwargs)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", simulated_lstat)
    monkeypatch.setattr(Path, "read_text", simulated_read_text)


def _require_directory_symlink(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink unavailable: {error}")
