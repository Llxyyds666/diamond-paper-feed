from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

from diamond_feed.models import PaperRecord
from diamond_feed.normalize import record_key
from diamond_feed.publication import cumulative_records, load_withheld_aliases
from diamond_feed.render import render_rss
from diamond_feed.state import FeedState, load_state, save_state
from diamond_feed.summarize import render_publication, run_summary


DAY_ONE = datetime(2026, 9, 7, tzinfo=timezone.utc)


def _record(index: int, **changes) -> PaperRecord:
    values = {
        "title": f"Diamond paper {index}",
        "abstract": f"Diamond abstract {index}",
        "authors": ["A. Author"],
        "journal": "Diamond Journal",
        "published_at": DAY_ONE - timedelta(days=index),
        "doi": f"10.1000/cumulative.{index}",
        "url": f"https://doi.org/10.1000/cumulative.{index}",
        "sources": ["crossref"],
        "source_ids": [f"crossref:{index}"],
    }
    values.update(changes)
    return PaperRecord(**values)


class RecordingClient:
    def __init__(self, relevant: bool = True):
        self.relevant = relevant
        self.payloads: list[dict] = []

    def complete_json(self, messages, max_tokens, budget):
        budget.consume()
        payload = json.loads(messages[-1]["content"])
        self.payloads.append(payload)
        if "papers" not in payload:
            return {"html": "<section><h2>本日新增概览</h2></section>"}
        return [
            {
                "key": paper["key"],
                "relevant": self.relevant,
                "confidence": 0.9,
                "category": "other-diamond",
                "matched_topics": ["diamond"] if self.relevant else [],
                "summary_zh": f"{paper['title']} 的中文摘要。",
                "reason": "金刚石材料研究。" if self.relevant else "不相关。",
            }
            for paper in payload["papers"]
        ]


def _feed_titles(path: Path) -> list[str]:
    return [
        item.findtext("title") or ""
        for item in ElementTree.parse(path).findall("./channel/item")
    ]


def test_two_days_publish_cumulative_history_but_digest_receives_only_new_paper(
    tmp_path, app_config
):
    state_path = tmp_path / "state.json"
    first_record = _record(1)
    save_state(
        state_path,
        FeedState(papers={record_key(first_record): first_record}, pending_ai=[record_key(first_record)]),
    )

    first_client = RecordingClient()
    first = run_summary(app_config, state_path, first_client, DAY_ONE, output_dir=tmp_path)
    state = load_state(state_path)
    second_record = _record(2)
    second_key = record_key(second_record)
    state.papers[second_key] = second_record
    state.pending_ai.append(second_key)
    save_state(state_path, state)

    second_client = RecordingClient()
    second = run_summary(
        app_config, state_path, second_client, DAY_ONE + timedelta(days=1), output_dir=tmp_path
    )

    assert first.selected == second.selected == 1
    assert set(_feed_titles(tmp_path / "ai_summary_feed.xml")) == {
        first_record.title,
        second_record.title,
    }
    digest_payload = next(payload for payload in second_client.payloads if "selected" in payload)
    assert [paper["title"] for paper in digest_payload["selected"]] == [second_record.title]
    html = (tmp_path / "ai_summary.html").read_text(encoding="utf-8")
    assert "本次新增 1 篇" in html
    assert "累计收录 2 篇" in html


def test_all_negative_day_retains_previously_published_history(tmp_path, app_config):
    accepted = replace(
        _record(1), ai_relevant=True, ai_confidence=0.9,
        categories=["other-diamond"], summary_zh="历史摘要",
    )
    pending = _record(2)
    state_path = tmp_path / "state.json"
    save_state(
        state_path,
        FeedState(
            papers={record_key(accepted): accepted, record_key(pending): pending},
            pending_ai=[record_key(pending)],
        ),
    )

    stats = run_summary(
        app_config, state_path, RecordingClient(relevant=False), DAY_ONE, output_dir=tmp_path
    )

    assert stats.selected == 0
    assert _feed_titles(tmp_path / "ai_summary_feed.xml") == [accepted.title]
    html = (tmp_path / "ai_summary.html").read_text(encoding="utf-8")
    assert accepted.title in html
    assert "本次新增 0 篇" in html
    assert "累计收录 1 篇" in html


def test_later_rejected_alias_clears_earlier_accepted_duplicate(tmp_path, app_config):
    accepted = replace(
        _record(
            1,
            doi="10.48550/arxiv.2609.01234",
            url="https://doi.org/10.48550/arxiv.2609.01234",
        ),
        ai_relevant=True,
        ai_confidence=0.9,
        categories=["other-diamond"],
        summary_zh="曾经收录",
    )
    pending_alias = _record(
        2,
        title=accepted.title,
        doi=None,
        url="https://arxiv.org/abs/2609.01234v2",
    )
    state_path = tmp_path / "state.json"
    save_state(
        state_path,
        FeedState(
            papers={record_key(accepted): accepted, record_key(pending_alias): pending_alias},
            pending_ai=[record_key(pending_alias)],
        ),
    )

    run_summary(app_config, state_path, RecordingClient(relevant=False), DAY_ONE, output_dir=tmp_path)
    state = load_state(state_path)

    assert {record.ai_relevant for record in state.papers.values()} == {False}
    assert _feed_titles(tmp_path / "ai_summary_feed.xml") == []


def test_withheld_identity_is_excluded_without_mutating_decisions(tmp_path):
    record = replace(
        _record(1), ai_relevant=True, ai_confidence=0.9,
        categories=["other-diamond"], summary_zh="保留的模型摘要",
    )
    state = FeedState(papers={record_key(record): record})
    before = record.to_dict()

    assert cumulative_records(state, {record_key(record)}) == []
    assert record.to_dict() == before


def test_shared_publication_renderer_uses_cumulative_collection(app_config):
    accepted = replace(
        _record(1), ai_relevant=True, ai_confidence=0.9,
        categories=["other-diamond"], summary_zh="累计摘要",
    )
    rejected = replace(_record(2), ai_relevant=False, summary_zh="不发布")
    state = FeedState(
        papers={record_key(accepted): accepted, record_key(rejected): rejected}
    )

    rss, html = render_publication(
        state,
        app_config,
        "2026-09-07",
        None,
        set(),
        new_count=0,
    )

    assert [
        item.findtext("title")
        for item in ElementTree.fromstring(rss).findall("./channel/item")
    ] == [accepted.title]
    assert accepted.title in html
    assert rejected.title not in html


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"version": 2, "withheld_identity_aliases": []},
        {"version": 1, "withheld_identity_aliases": [], "extra": True},
        {"version": 1, "withheld_identity_aliases": [1]},
        {"version": True, "withheld_identity_aliases": []},
    ],
)
def test_policy_loader_rejects_malformed_exact_schema(tmp_path, payload):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="publication policy"):
        load_withheld_aliases(path)


def test_policy_loader_treats_absence_as_empty(tmp_path):
    assert load_withheld_aliases(tmp_path / "absent.json") == set()


def test_malformed_default_policy_fails_before_client_or_publication(tmp_path, app_config):
    state_path = tmp_path / "state.json"
    pending = _record(1)
    save_state(
        state_path,
        FeedState(papers={record_key(pending): pending}, pending_ai=[record_key(pending)]),
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "ai_publication.json").write_text('{"version": 1}', encoding="utf-8")
    previous = {
        "ai_summary_feed.xml": "old rss",
        "ai_summary.html": "old html",
        "ai_usage.json": "[]\n",
    }
    for name, contents in previous.items():
        (tmp_path / name).write_text(contents, encoding="utf-8")
    client = RecordingClient()

    with pytest.raises(ValueError, match="publication policy"):
        run_summary(app_config, state_path, client, DAY_ONE, output_dir=tmp_path)

    assert client.payloads == []
    assert {
        name: (tmp_path / name).read_text(encoding="utf-8") for name in previous
    } == previous


def test_ai_rss_can_exceed_2000_while_default_raw_renderer_remains_capped(app_config):
    records = [
        replace(
            _record(index), ai_relevant=True, summary_zh=f"摘要 {index}",
            ai_confidence=0.9, categories=["other-diamond"],
        )
        for index in range(2001)
    ]

    state = FeedState(papers={record_key(record): record for record in records})
    ai_xml, _ = render_publication(
        state, app_config, "2026-09-07", None, set(), new_count=0
    )
    raw_xml = render_rss(records, "Raw", "https://example.test", len(records))

    assert len(ElementTree.fromstring(ai_xml).findall("./channel/item")) == 2001
    assert len(ElementTree.fromstring(raw_xml).findall("./channel/item")) == 2000
    with pytest.raises(ValueError, match="negative"):
        render_rss(records, "Raw", "https://example.test", -1, cap_at_2000=False)
