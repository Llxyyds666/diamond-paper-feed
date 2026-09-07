from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from diamond_feed.config import load_config
from diamond_feed.filtering import load_rules
from diamond_feed.models import PaperRecord
from diamond_feed.normalize import record_key
from diamond_feed.state import FeedState, save_state


class FakeDeepSeekClient:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.requests = 0

    def complete_json(self, messages, max_tokens, budget):
        budget.consume()
        self.requests += 1
        if self.fail:
            raise RuntimeError("simulated DeepSeek failure")
        request = json.loads(messages[-1]["content"])
        papers = request.get("papers")
        if papers is None:
            return {"html": "<section><h2>金刚石论文摘要</h2></section>"}
        return [
            {
                "key": item["key"],
                "relevant": True,
                "confidence": 0.95,
                "category": "other-diamond",
                "matched_topics": ["diamond"],
                "summary_zh": f"{item['title']} 的中文摘要。",
                "reason": "论文研究对象是金刚石。",
            }
            for item in papers
        ]


@pytest.fixture
def app_config():
    return load_config(Path("paper_feed_config.json"))


@pytest.fixture
def ai_config(app_config):
    return app_config.ai


@pytest.fixture
def query_rules():
    return load_rules(Path("config/queries.json"))


@pytest.fixture
def diamond_records():
    return [
        PaperRecord(
            title="Piezoelectric effect in a polycrystalline diamond membrane",
            abstract="A flexible diamond membrane produces a stable voltage.",
            authors=["A. Author"],
            journal="Diamond Journal",
            published_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            doi="10.1000/diamond.1",
            url="https://doi.org/10.1000/diamond.1",
            sources=["rss"],
            source_ids=["rss:1"],
        ),
        PaperRecord(
            title="Boron-doped diamond electrode for electrochemistry",
            abstract="Electrochemical response of a boron-doped diamond electrode.",
            authors=["B. Author"],
            journal="Carbon Journal",
            published_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
            doi="10.1000/diamond.2",
            url="https://doi.org/10.1000/diamond.2",
            sources=["crossref"],
            source_ids=["crossref:2"],
        ),
    ]


@pytest.fixture
def configured_state_with_100_pending(tmp_path):
    papers = {}
    pending = []
    for index in range(100):
        record = PaperRecord(
            title=f"Diamond research paper {index}",
            abstract="Diamond material research.",
            authors=["A. Author"],
            journal="Diamond Journal",
            published_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            doi=f"10.1000/bootstrap.{index}",
            url=f"https://doi.org/10.1000/bootstrap.{index}",
            sources=["openalex"],
            source_ids=[f"openalex:{index}"],
        )
        key = record_key(record)
        papers[key] = record
        pending.append(key)
    path = tmp_path / "state.json"
    save_state(path, FeedState(papers=papers, pending_ai=pending, source_watermarks={}))
    return path


@pytest.fixture
def fake_deepseek_client():
    return FakeDeepSeekClient()


@pytest.fixture
def failing_deepseek_client():
    return FakeDeepSeekClient(fail=True)
