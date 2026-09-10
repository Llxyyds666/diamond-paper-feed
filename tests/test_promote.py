from dataclasses import replace
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

from diamond_feed import summarize
from diamond_feed.normalize import group_records, identity_aliases, record_key
from diamond_feed.state import FeedState, load_state, save_state


NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


def report_for(records):
    groups = group_records(records)
    canonical = {alias: key for key, (_, aliases) in groups.items() for alias in aliases}
    return {"date": NOW.isoformat(), "run_id": "test-run", "input_records": len(records),
            "unique_records": len(groups), "stats": {"failed": False, "candidates": len(groups),
            "processed": len(groups), "selected": len(groups)},
            "papers": [{"key": record_key(p), "canonical_key": canonical[record_key(p)],
                "title": p.title, "authors": p.authors, "doi": p.doi, "url": p.url,
                "journal": p.journal, "published_at": p.published_at.isoformat(),
                "input_abstract": p.abstract[:1200], "ai_relevant": True,
                "ai_confidence": .9, "categories": ["other-diamond"],
                "summary_zh": "已有的金刚石研究摘要。", "reason": "研究真实金刚石材料。"} for p in records]}


def setup_case(tmp_path, diamond_records):
    old, new = diamond_records
    old.ai_relevant, old.ai_confidence, old.summary_zh = True, .8, "旧摘要，暂不发布。"
    old.categories = ["other-diamond"]
    spare = replace(new, title="Another diamond paper", doi="10.1000/spare")
    records = [old, new, spare]
    state_path = tmp_path / "state.json"
    save_state(state_path, FeedState({record_key(p): p for p in records}, [record_key(new), record_key(spare)]))
    policy_path = tmp_path / "config" / "ai_publication.json"
    policy_path.parent.mkdir()
    policy_path.write_text(json.dumps({"version": 1, "withheld_identity_aliases": sorted(identity_aliases(old))}), encoding="utf-8")
    (tmp_path / "ai_usage.json").write_text('[{"untouched":"original ledger"}]\n', encoding="utf-8")
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report_for([new])), encoding="utf-8")
    return state_path, report_path, old, new, spare


def promote(config, state_path, report_path, output_dir, *, focus_overrides_path=None):
    module = importlib.import_module("diamond_feed.promote")
    return module.promote_evaluation(
        config,
        state_path,
        report_path,
        NOW,
        output_dir=output_dir,
        focus_overrides_path=focus_overrides_path,
    )


def test_offline_promotion_renders_focused_rss_without_model_request(
    tmp_path, diamond_records, app_config, monkeypatch
):
    state_path, report_path, _, new, _ = setup_case(tmp_path, diamond_records)
    override_path = tmp_path / "focus.json"
    override_path.write_text(
        json.dumps(
            {
                "version": 1,
                "papers": {
                    record_key(new): ["diamond-power-rf-detectors"],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "diamond_feed.ai.DeepSeekClient.complete_json",
        lambda *a, **k: pytest.fail("unexpected model request"),
    )
    monkeypatch.setattr(
        summarize,
        "AbstractEnricher",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("promotion created enrichment service")
        ),
    )

    result = promote(
        app_config,
        state_path,
        report_path,
        tmp_path,
        focus_overrides_path=override_path,
    )

    items = ElementTree.parse(tmp_path / "device_focus_feed.xml").findall(
        "./channel/item"
    )
    assert [item.findtext("guid") for item in items] == [record_key(new)]
    assert result["focused"] == 1
    assert result["model_requests"] == 0


def test_offline_promotion_persists_focus_labels_from_evaluation_report(
    tmp_path, diamond_records, app_config
):
    state_path, report_path, _, new, _ = setup_case(tmp_path, diamond_records)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["papers"][0]["categories"] = [
        "electronics-optoelectronics",
        "device-grade-single-crystal",
    ]
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = promote(app_config, state_path, report_path, tmp_path)

    assert load_state(state_path).papers[record_key(new)].categories == [
        "electronics-optoelectronics",
        "device-grade-single-crystal",
    ]
    items = ElementTree.parse(tmp_path / "device_focus_feed.xml").findall(
        "./channel/item"
    )
    assert [item.findtext("guid") for item in items] == [record_key(new)]
    assert result["focused"] == 1


def test_offline_promotion_withholds_legacy_preserves_usage_and_is_idempotent(tmp_path, diamond_records, app_config, monkeypatch):
    state_path, report_path, old, new, spare = setup_case(tmp_path, diamond_records)
    original_old = old.to_dict()
    original_usage = (tmp_path / "ai_usage.json").read_bytes()
    original_report = report_path.read_bytes()
    # Promotion must never call the provider, even if a key happens to be present.
    monkeypatch.setattr("diamond_feed.ai.DeepSeekClient.complete_json", lambda *a, **k: pytest.fail("unexpected model request"))
    result = promote(app_config, state_path, report_path, tmp_path)
    assert result["published"] == 1 and result["model_requests"] == 0
    state = load_state(state_path)
    assert state.papers[record_key(old)].to_dict() == original_old
    assert state.papers[record_key(new)].summary_zh == "已有的金刚石研究摘要。"
    assert state.pending_ai == [record_key(spare)]
    items = ElementTree.parse(tmp_path / "ai_summary_feed.xml").findall("./channel/item")
    assert [item.findtext("guid") for item in items] == [record_key(new)]
    assert (tmp_path / "ai_usage.json").read_bytes() == original_usage
    assert report_path.read_bytes() == original_report
    before = {
        name: (tmp_path / name).read_bytes()
        for name in [
            "state.json",
            "ai_summary.html",
            "ai_summary_feed.xml",
            "device_focus_feed.xml",
        ]
    }
    promote(app_config, state_path, report_path, tmp_path)
    assert all((tmp_path / name).read_bytes() == contents for name, contents in before.items())


@pytest.mark.parametrize("mutation", ["failed", "partial", "duplicate", "missing", "boolean", "confidence", "category", "stale_title", "stale_abstract", "summary"])
def test_invalid_report_preserves_every_output(tmp_path, diamond_records, app_config, mutation):
    state_path, report_path, _, _, _ = setup_case(tmp_path, diamond_records)
    for name in ["ai_summary.html", "ai_summary_feed.xml", "device_focus_feed.xml"]:
        (tmp_path / name).write_text("old output", encoding="utf-8")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    item = report["papers"][0]
    if mutation == "failed": report["stats"]["failed"] = True
    if mutation == "partial": report["stats"]["processed"] = 0
    if mutation == "duplicate": report["papers"].append(dict(item))
    if mutation == "missing": item["key"] = "doi:10.1000/missing"
    if mutation == "boolean": item["ai_relevant"] = "false"
    if mutation == "confidence": item["ai_confidence"] = 2
    if mutation == "category": item["categories"] = ["invented"]
    if mutation == "stale_title": item["title"] += " changed"
    if mutation == "stale_abstract": item["input_abstract"] += " changed"
    if mutation == "summary": item["summary_zh"] = ""
    report_path.write_text(json.dumps(report), encoding="utf-8")
    before = {
        name: (tmp_path / name).read_bytes()
        for name in [
            "state.json",
            "ai_usage.json",
            "ai_summary.html",
            "ai_summary_feed.xml",
            "device_focus_feed.xml",
        ]
    }
    with pytest.raises(ValueError): promote(app_config, state_path, report_path, tmp_path)
    assert all((tmp_path / name).read_bytes() == contents for name, contents in before.items())


def test_conflicting_aliases_are_rejected_without_state_write(tmp_path, diamond_records, app_config):
    first = replace(diamond_records[0], doi="10.48550/arxiv.2608.08152", url="https://arxiv.org/abs/2608.08152")
    second = replace(first, doi=None, url=first.url + "v2")
    state_path = tmp_path / "state.json"
    keys = [record_key(p) for p in [first, second]]
    save_state(state_path, FeedState(dict(zip(keys, [first, second])), keys))
    report = report_for([first, second])
    report["papers"][1]["ai_relevant"] = False
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    original = state_path.read_bytes()
    with pytest.raises(ValueError): promote(app_config, state_path, report_path, tmp_path)
    assert state_path.read_bytes() == original


def test_mid_publication_failure_rolls_back_state_and_both_views(tmp_path, diamond_records, app_config, monkeypatch):
    from diamond_feed import atomic
    state_path, report_path, _, _, _ = setup_case(tmp_path, diamond_records)
    for name in ["ai_summary.html", "ai_summary_feed.xml", "device_focus_feed.xml"]:
        (tmp_path / name).write_text("previous", encoding="utf-8")
    before = {
        name: (tmp_path / name).read_bytes()
        for name in [
            "state.json",
            "ai_usage.json",
            "ai_summary.html",
            "ai_summary_feed.xml",
            "device_focus_feed.xml",
        ]
    }
    original_replace = atomic.os.replace
    def fail_focus(source, destination):
        if Path(source).name == "device_focus_feed.xml.tmp" and Path(destination) == (tmp_path / "device_focus_feed.xml").resolve():
            raise OSError("simulated publication failure")
        return original_replace(source, destination)
    monkeypatch.setattr(atomic.os, "replace", fail_focus)
    with pytest.raises(OSError): promote(app_config, state_path, report_path, tmp_path)
    assert all((tmp_path / name).read_bytes() == contents for name, contents in before.items())
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob("*.bak"))


def test_report_cannot_be_a_publication_destination(tmp_path, diamond_records, app_config):
    state_path, report_path, _, _, _ = setup_case(tmp_path, diamond_records)
    collision = tmp_path / "ai_summary.html"
    collision.write_bytes(report_path.read_bytes())
    original = state_path.read_bytes()
    with pytest.raises(ValueError): promote(app_config, state_path, collision, tmp_path)
    assert state_path.read_bytes() == original


def test_focus_override_cannot_be_a_publication_destination(
    tmp_path, diamond_records, app_config
):
    state_path, report_path, _, _, _ = setup_case(tmp_path, diamond_records)
    collision = tmp_path / "device_focus_feed.xml"
    collision.write_text('{"version":1,"papers":{}}', encoding="utf-8")
    original = state_path.read_bytes()

    with pytest.raises(ValueError, match="unique"):
        promote(
            app_config,
            state_path,
            report_path,
            tmp_path,
            focus_overrides_path=collision,
        )

    assert state_path.read_bytes() == original


def test_decision_propagates_to_unreported_alias_and_removes_both_from_queue(tmp_path, diamond_records, app_config):
    first = replace(diamond_records[0], doi="10.48550/arxiv.2608.08152", url="https://arxiv.org/abs/2608.08152")
    alias = replace(first, doi=None, url=first.url + "v2")
    keys = [record_key(first), record_key(alias)]
    state_path = tmp_path / "state.json"
    save_state(state_path, FeedState(dict(zip(keys, [first, alias])), keys))
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report_for([first])), encoding="utf-8")
    result = promote(app_config, state_path, report_path, tmp_path)
    state = load_state(state_path)
    assert state.pending_ai == []
    assert all(state.papers[key].ai_relevant is True for key in keys)
    assert result["published"] == 1 and result["model_requests"] == 0


def test_rejected_batch_keeps_previous_cumulative_publication(tmp_path, diamond_records, app_config):
    previous, rejected = diamond_records
    previous.ai_relevant = True
    previous.ai_confidence = .8
    previous.categories = ["other-diamond"]
    previous.summary_zh = "过去已发布的摘要。"
    state_path = tmp_path / "state.json"
    save_state(
        state_path,
        FeedState(
            {record_key(previous): previous, record_key(rejected): rejected},
            [record_key(rejected)],
        ),
    )
    report = report_for([rejected])
    report["papers"][0]["ai_relevant"] = False
    report["papers"][0]["summary_zh"] = "不是金刚石材料研究。"
    report["stats"]["selected"] = 0
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    result = promote(app_config, state_path, report_path, tmp_path)
    items = ElementTree.parse(tmp_path / "ai_summary_feed.xml").findall("./channel/item")
    assert [item.findtext("guid") for item in items] == [record_key(previous)]
    assert result["published"] == 1 and result["selected"] == 0
