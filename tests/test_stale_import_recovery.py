from dataclasses import replace
from datetime import date, datetime, timezone
import json

from diamond_feed.normalize import record_key
from diamond_feed.state import FeedState, SourceContinuation, _payload, load_state, save_state
from scripts import repair_stale_index_imports as repair
from scripts.repair_stale_index_imports import recovery_plan


def test_recovery_archives_only_new_old_records_preserving_baseline_and_recent(diamond_records):
    historical = replace(diamond_records[0], published_at=datetime(2015, 1, 1, tzinfo=timezone.utc),
                         ai_relevant=True, summary_zh="原先的精选。")
    accident = replace(historical, doi="10.1000/old-accident")
    recent = replace(diamond_records[1], published_at=datetime(2026, 11, 1, tzinfo=timezone.utc))
    keys = [record_key(record) for record in [historical, accident, recent]]
    baseline = FeedState(papers={keys[0]: historical})
    state = FeedState(papers=dict(zip(keys, [historical, accident, recent])), pending_ai=keys[1:],
                      source_continuations={"crossref": SourceContinuation(date(2026, 10, 16), "old-query"),
                                            "openalex": SourceContinuation(date(2026, 10, 16), "keep-query")})

    cleaned, archived = recovery_plan(state, baseline, date(2026, 10, 16))

    assert set(cleaned.papers) == {keys[0], keys[2]}
    assert cleaned.pending_ai == [keys[2]]
    assert cleaned.papers[keys[0]].summary_zh == "原先的精选。"
    assert set(archived.papers) == {keys[1]}
    assert archived.pending_ai == [keys[1]]
    assert "crossref" not in cleaned.source_continuations
    assert cleaned.source_continuations["openalex"].cursor == "keep-query"
    assert set(state.papers) == set(keys)  # planning never mutates the source
    assert "crossref" in state.source_continuations


def test_recovery_preserves_identity_alias_upgrade_and_cutoff_day(diamond_records):
    old_date = datetime(2016, 1, 1, tzinfo=timezone.utc)
    preprint = replace(diamond_records[0], doi=None, url="https://arxiv.org/abs/1601.00001", published_at=old_date)
    upgraded = replace(preprint, doi="10.48550/arxiv.1601.00001")
    boundary = replace(diamond_records[1], published_at=datetime(2026, 10, 16, tzinfo=timezone.utc))
    baseline = FeedState(papers={record_key(preprint): preprint})
    state = FeedState(papers={record_key(paper): paper for paper in [upgraded, boundary]})

    cleaned, archived = recovery_plan(state, baseline, date(2026, 10, 16))

    assert set(cleaned.papers) == set(state.papers)
    assert archived.papers == {}


def test_apply_archives_exact_input_and_preserves_usage_and_receipts(tmp_path, diamond_records, monkeypatch):
    historical = replace(diamond_records[0], published_at=datetime(2015, 1, 1, tzinfo=timezone.utc),
                         ai_relevant=True, summary_zh="正常历史。")
    accident = replace(historical, doi="10.1000/accidental-import")
    recent = replace(diamond_records[1], published_at=datetime(2026, 11, 1, tzinfo=timezone.utc))
    baseline = FeedState(papers={record_key(historical): historical})
    records = [historical, accident, recent]
    original = FeedState(papers={record_key(p): p for p in records},
                         pending_ai=[record_key(p) for p in records],
                         source_watermarks={"crossref": "2026-11-15T00:00:00+00:00"})
    state_path = tmp_path / "state.json"
    save_state(state_path, original)
    ledger = tmp_path / "ai_usage.json"
    receipts = tmp_path / "ncd_watch.json"
    ledger.write_bytes(b'[{"real_spend":"retained"}]')
    receipts.write_bytes(b'{"accepted_receipt":"retained"}')
    archive = tmp_path / "archives" / "recovery.json"
    baseline_sha, snapshot_sha = "a" * 40, "b" * 40

    def fake_git(args, **kwargs):
        if args[1] == "show":
            return json.dumps(_payload(baseline)).encode("utf-8")
        return (snapshot_sha if args[-1] == "HEAD" else baseline_sha) + "\n"

    monkeypatch.setattr(repair.subprocess, "check_output", fake_git)
    assert repair.main(["--baseline", baseline_sha, "--cutoff", "2026-10-16",
                        "--state", str(state_path), "--archive", str(archive),
                        "--now", "2026-11-17T00:00:00+00:00", "--apply"]) == 0

    evidence = json.loads(archive.read_text(encoding="utf-8"))
    assert evidence["original_state"] == _payload(original)
    assert evidence["archived_keys"] == [record_key(accident)]
    saved = load_state(state_path)
    assert set(saved.papers) == {record_key(historical), record_key(recent)}
    assert saved.pending_ai == [record_key(recent)]
    assert "crossref" not in saved.source_watermarks
    assert ledger.read_bytes() == b'[{"real_spend":"retained"}]'
    assert receipts.read_bytes() == b'{"accepted_receipt":"retained"}'
