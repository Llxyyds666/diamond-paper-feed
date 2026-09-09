from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

from diamond_feed import focus
from diamond_feed.models import AiDecision, PaperRecord
from diamond_feed.normalize import record_key
from diamond_feed.state import FeedState, load_state, save_state
from diamond_feed.summarize import _apply_decision, run_summary


NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


def _paper(index: int, **changes) -> PaperRecord:
    values = {
        "title": f"Diamond device paper {index}",
        "abstract": f"Diamond device abstract {index}",
        "authors": ["A. Author"],
        "journal": "Diamond Devices",
        "published_at": NOW,
        "doi": f"10.1000/focus.{index}",
        "url": f"https://doi.org/10.1000/focus.{index}",
        "sources": ["crossref"],
        "source_ids": [f"crossref:{index}"],
        "categories": ["electronics-optoelectronics"],
        "summary_zh": f"金刚石器件摘要 {index}",
        "ai_relevant": True,
        "ai_confidence": 0.9,
    }
    values.update(changes)
    return PaperRecord(**values)


class FocusClient:
    def __init__(self, matched_topics: list[str]):
        self.matched_topics = matched_topics
        self.payloads: list[dict[str, object]] = []

    def complete_json(self, messages, max_tokens, budget):
        budget.consume()
        payload = json.loads(messages[-1]["content"])
        self.payloads.append(payload)
        if "papers" not in payload:
            return {"html": "<section><h2>新增概览</h2></section>"}
        return [
            {
                "key": paper["key"],
                "relevant": True,
                "confidence": 0.95,
                "category": "electronics-optoelectronics",
                "matched_topics": list(self.matched_topics),
                "summary_zh": f"{paper['title']} 的中文摘要。",
                "reason": "金刚石器件研究。",
            }
            for paper in payload["papers"]
        ]


def _feed_titles(path):
    return [
        item.findtext("title") or ""
        for item in ElementTree.parse(path).findall("./channel/item")
    ]


def test_focus_override_loader_treats_absence_as_empty(tmp_path):
    assert focus.load_focus_overrides(tmp_path / "missing.json") == {}


def test_focus_override_loader_accepts_exact_multi_label_schema(tmp_path):
    path = tmp_path / "focus.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "papers": {
                    "doi:10.1000/focus.1": [
                        "diamond-power-rf-detectors",
                        "device-grade-single-crystal",
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    assert focus.load_focus_overrides(path) == {
        "doi:10.1000/focus.1": frozenset(
            {"diamond-power-rf-detectors", "device-grade-single-crystal"}
        )
    }


@pytest.mark.parametrize(
    "contents",
    [
        "[]",
        '{"version":2,"papers":{}}',
        '{"version":1,"papers":{},"extra":true}',
        '{"version":1,"papers":{"bad-key":["diamond-power-rf-detectors"]}}',
        '{"version":1,"papers":{"doi:10.1000/x":["unknown"]}}',
        '{"version":1,"papers":{"doi:10.1000/x":[]}}',
        '{"version":1,"version":1,"papers":{}}',
        '{"version":1,"papers":{"doi:10.1000/x":'
        '["diamond-power-rf-detectors"],"doi:10.1000/x":'
        '["diamond-thermal-management"]}}',
    ],
)
def test_focus_override_loader_rejects_malformed_or_duplicate_json(tmp_path, contents):
    path = tmp_path / "focus.json"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(ValueError, match="focus override"):
        focus.load_focus_overrides(path)


def test_focused_records_include_stored_and_overridden_labels_once():
    stored = _paper(
        1,
        categories=[
            "electronics-optoelectronics",
            "diamond-power-rf-detectors",
            "device-grade-single-crystal",
        ],
    )
    overridden = _paper(2)
    general_only = _paper(3)
    state = FeedState(
        papers={record_key(paper): paper for paper in [stored, overridden, general_only]}
    )

    records = focus.focused_records(
        state,
        withheld_aliases=set(),
        overrides={
            record_key(overridden): frozenset({"diamond-thermal-management"})
        },
    )

    assert {record_key(record) for record in records} == {
        record_key(stored),
        record_key(overridden),
    }
    assert len(records) == 2


def test_focused_records_respect_rejection_and_withholding_without_mutation():
    rejected = _paper(
        1,
        ai_relevant=False,
        categories=["electronics-optoelectronics", "diamond-power-rf-detectors"],
    )
    withheld = _paper(
        2,
        categories=["electronics-optoelectronics", "diamond-power-rf-detectors"],
    )
    state = FeedState(
        papers={record_key(paper): paper for paper in [rejected, withheld]}
    )
    before = {key: paper.to_dict() for key, paper in state.papers.items()}

    assert focus.focused_records(
        state,
        withheld_aliases={record_key(withheld)},
        overrides={record_key(rejected): frozenset({"diamond-power-rf-detectors"})},
    ) == []
    assert {key: paper.to_dict() for key, paper in state.papers.items()} == before


def test_focused_records_deduplicate_arxiv_aliases():
    doi_record = _paper(
        1,
        doi="10.48550/arxiv.2608.08152",
        url="https://arxiv.org/abs/2608.08152",
        categories=["other-diamond", "diamond-power-rf-detectors"],
    )
    url_alias = replace(
        doi_record,
        doi=None,
        url="https://arxiv.org/abs/2608.08152v2",
        title="Diamond device preprint version two",
    )
    state = FeedState(
        papers={record_key(paper): paper for paper in [doi_record, url_alias]}
    )

    records = focus.focused_records(state, set(), {})

    assert len(records) == 1
    assert record_key(records[0]) == record_key(doi_record)


def test_apply_decision_persists_controlled_focus_labels_after_primary_category():
    paper = _paper(1, categories=[])
    key = record_key(paper)
    state = FeedState(papers={key: paper})

    _apply_decision(
        state,
        AiDecision(
            key=key,
            relevant=True,
            confidence=0.95,
            category="electronics-optoelectronics",
            matched_topics=[
                "diamond-power-rf-detectors",
                "device-grade-single-crystal",
            ],
            summary_zh="摘要",
            reason="器件论文",
        ),
    )

    assert state.papers[key].categories == [
        "electronics-optoelectronics",
        "diamond-power-rf-detectors",
        "device-grade-single-crystal",
    ]


def test_two_days_accumulate_focus_without_an_extra_request(tmp_path, app_config):
    state_path = tmp_path / "state.json"
    first_record = _paper(1)
    first_key = record_key(first_record)
    save_state(state_path, FeedState(papers={first_key: first_record}, pending_ai=[first_key]))

    first_client = FocusClient(["diamond-power-rf-detectors"])
    first = run_summary(app_config, state_path, first_client, NOW, output_dir=tmp_path)
    state = load_state(state_path)
    second_record = _paper(2)
    second_key = record_key(second_record)
    state.papers[second_key] = second_record
    state.pending_ai.append(second_key)
    save_state(state_path, state)

    second_client = FocusClient(["diamond-thermal-management"])
    second = run_summary(
        app_config,
        state_path,
        second_client,
        NOW + timedelta(days=1),
        output_dir=tmp_path,
    )

    expected = {first_record.title, second_record.title}
    assert set(_feed_titles(tmp_path / "ai_summary_feed.xml")) == expected
    assert set(_feed_titles(tmp_path / "device_focus_feed.xml")) == expected
    assert first.requests == second.requests == 2
    day_two_screening = next(
        payload for payload in second_client.payloads if "papers" in payload
    )
    assert [paper["title"] for paper in day_two_screening["papers"]] == [
        second_record.title
    ]


def test_general_only_paper_stays_in_broad_feed_not_focus_feed(tmp_path, app_config):
    state_path = tmp_path / "state.json"
    paper = _paper(1)
    key = record_key(paper)
    save_state(state_path, FeedState(papers={key: paper}, pending_ai=[key]))

    stats = run_summary(
        app_config,
        state_path,
        FocusClient([]),
        NOW,
        output_dir=tmp_path,
    )

    assert stats.requests == 2
    assert _feed_titles(tmp_path / "ai_summary_feed.xml") == [paper.title]
    assert _feed_titles(tmp_path / "device_focus_feed.xml") == []


def test_focus_output_failure_rolls_back_all_summary_outputs(
    tmp_path, app_config, monkeypatch
):
    from diamond_feed import atomic

    state_path = tmp_path / "state.json"
    paper = _paper(1)
    key = record_key(paper)
    save_state(state_path, FeedState(papers={key: paper}, pending_ai=[key]))
    for name in [
        "ai_summary_feed.xml",
        "ai_summary.html",
        "device_focus_feed.xml",
        "ai_usage.json",
    ]:
        contents = "[]\n" if name == "ai_usage.json" else f"previous {name}\n"
        (tmp_path / name).write_text(contents, encoding="utf-8")
    before = {
        name: (tmp_path / name).read_bytes()
        for name in [
            "state.json",
            "ai_summary_feed.xml",
            "ai_summary.html",
            "device_focus_feed.xml",
            "ai_usage.json",
        ]
    }
    original_replace = atomic.os.replace

    def fail_focus(source, destination):
        if (
            Path(source).name == "device_focus_feed.xml.tmp"
            and Path(destination) == (tmp_path / "device_focus_feed.xml").resolve()
        ):
            raise OSError("simulated focused RSS publication failure")
        return original_replace(source, destination)

    monkeypatch.setattr(atomic.os, "replace", fail_focus)

    with pytest.raises(OSError, match="focused RSS"):
        run_summary(
            app_config,
            state_path,
            FocusClient(["diamond-power-rf-detectors"]),
            NOW,
            output_dir=tmp_path,
        )

    assert all((tmp_path / name).read_bytes() == contents for name, contents in before.items())
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob("*.bak"))


def test_malformed_focus_override_fails_before_model_or_publication(tmp_path, app_config):
    state_path = tmp_path / "state.json"
    paper = _paper(1)
    key = record_key(paper)
    save_state(state_path, FeedState(papers={key: paper}, pending_ai=[key]))
    override_path = tmp_path / "focus.json"
    override_path.write_text(
        '{"version":1,"papers":{"doi:10.1000/focus.1":["unknown"]}}',
        encoding="utf-8",
    )
    previous = {
        "ai_summary_feed.xml": "old rss\n",
        "ai_summary.html": "old html\n",
        "device_focus_feed.xml": "old focus\n",
        "ai_usage.json": "[]\n",
    }
    for name, contents in previous.items():
        (tmp_path / name).write_text(contents, encoding="utf-8")
    client = FocusClient(["diamond-power-rf-detectors"])

    with pytest.raises(ValueError, match="focus override"):
        run_summary(
            app_config,
            state_path,
            client,
            NOW,
            output_dir=tmp_path,
            focus_overrides_path=override_path,
        )

    assert client.payloads == []
    assert {
        name: (tmp_path / name).read_text(encoding="utf-8") for name in previous
    } == previous


def test_repository_focus_override_seed_is_exact():
    expected = {
        "doi:10.1016/j.diamond.2026.114067": frozenset(
            {"diamond-power-rf-detectors"}
        ),
        "doi:10.1049/ell2.70574": frozenset({"diamond-power-rf-detectors"}),
        "doi:10.1080/08957959.2026.2651875": frozenset(
            {"diamond-power-rf-detectors"}
        ),
        "doi:10.1080/08957959.2026.2661257": frozenset(
            {"diamond-power-rf-detectors"}
        ),
    }
    assert focus.load_focus_overrides(Path("config/device_focus_overrides.json")) == expected

    root = ElementTree.parse("device_focus_feed.xml").getroot()
    assert root.findtext("./channel/title") == "Diamond Device Focus Feed · 中文摘要"
    guids = [item.findtext("guid") for item in root.findall("./channel/item")]
    assert set(expected).issubset(guids)
    assert None not in guids and len(guids) == len(set(guids))
