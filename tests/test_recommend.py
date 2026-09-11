import json

import pytest

from diamond_feed.ai import RequestBudget, RequestCounter
from diamond_feed.normalize import record_key
from diamond_feed.recommend import focus_names, recommend_one


class RecommendationClient:
    def __init__(self, response):
        self.response = response
        self.messages = None
        self.max_tokens = None

    def complete_json(self, messages, max_tokens, budget):
        budget.consume()
        self.messages = messages
        self.max_tokens = max_tokens
        return self.response


class RetryingRecommendationClient:
    def __init__(self):
        self.attempts = 0
        self.counter = None

    def complete_json(self, messages, max_tokens, budget):
        self.counter = budget
        budget.consume()
        self.attempts += 1
        budget.consume()
        self.attempts += 1
        return {"key": "doi:10.1000/diamond.1", "reason": "值得阅读。"}


def test_recommendation_sends_complete_abstract_and_returns_exact_candidate(
    diamond_records, ai_config
):
    record = diamond_records[0]
    record.abstract = "金刚石完整原始摘要" * 1000
    record.summary_zh = "完整原摘要对应的中文摘要"
    record.categories = [
        "electronics-optoelectronics",
        "diamond-power-rf-detectors",
        "device-grade-single-crystal",
    ]
    client = RecommendationClient(
        {
            "key": "doi:10.1000/diamond.1",
            "reason": "器件结构与实验结果完整，适合作为入门主线。",
        }
    )
    budget = RequestBudget(1)

    result = recommend_one([record], client, ai_config, budget)
    payload = json.loads(client.messages[-1]["content"])

    assert payload["candidates"][0]["abstract"] == record.abstract
    assert payload["candidates"][0]["abstract_missing"] is False
    assert payload["candidates"][0]["summary_zh"] == record.summary_zh
    assert result.key == "doi:10.1000/diamond.1"
    assert budget.used == 1
    assert client.max_tokens == 512
    assert focus_names(record) == (
        "金刚石功率/射频/探测器件",
        "金刚石单晶器件",
    )


def test_missing_abstract_includes_complete_chinese_summary_fallback(
    diamond_records, ai_config
):
    record = diamond_records[0]
    record.abstract = " \t"
    record.summary_zh = "完整中文摘要" * 1000
    record.categories = ["diamond-thermal-management"]
    client = RecommendationClient(
        {"key": record_key(record), "reason": "摘要缺失已标明，中文摘要仍有学习价值。"}
    )

    recommend_one([record], client, ai_config, RequestBudget(1))
    candidate = json.loads(client.messages[-1]["content"])["candidates"][0]

    assert candidate["abstract"] == record.abstract
    assert candidate["abstract_missing"] is True
    assert candidate["summary_zh"] == record.summary_zh


def test_recommendation_allows_one_retry_from_shared_budget(
    diamond_records, ai_config
):
    record = diamond_records[0]
    record.categories = ["diamond-power-rf-detectors"]
    client = RetryingRecommendationClient()
    counter = RequestCounter()

    result = recommend_one([record], client, ai_config, counter)

    assert result.key == "doi:10.1000/diamond.1"
    assert client.attempts == 2
    assert client.counter is counter
    assert counter.used == 2


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"key": "unknown", "reason": "值得阅读。"},
        {"key": "doi:10.1000/diamond.1", "reason": ""},
        {"key": "doi:10.1000/diamond.1", "reason": "english only"},
        {"key": "doi:10.1000/diamond.1", "reason": "值" * 61},
        {"key": "doi:10.1000/diamond.1", "reason": "值得阅读。", "extra": 1},
    ],
)
def test_recommendation_rejects_invalid_response(response, diamond_records, ai_config):
    record = diamond_records[0]
    record.categories = ["other-diamond", "diamond-power-rf-detectors"]
    with pytest.raises(ValueError, match="invalid recommendation"):
        recommend_one(
            [record], RecommendationClient(response), ai_config, RequestBudget(1)
        )


def test_broad_only_record_cannot_enter_recommendation_pool(
    diamond_records, ai_config
):
    record = diamond_records[0]
    record.categories = ["other-diamond"]
    client = RecommendationClient({"key": record_key(record), "reason": "值得阅读。"})
    with pytest.raises(ValueError, match="no device-focus candidates"):
        recommend_one([record], client, ai_config, RequestBudget(1))
    assert client.messages is None


def test_duplicate_candidate_keys_are_rejected_before_request(
    diamond_records, ai_config
):
    first = diamond_records[0]
    first.categories = ["diamond-power-rf-detectors"]
    duplicate = diamond_records[1]
    duplicate.doi = first.doi
    duplicate.categories = ["diamond-thermal-management"]
    client = RecommendationClient({"key": record_key(first), "reason": "值得阅读。"})
    budget = RequestBudget(1)

    with pytest.raises(ValueError, match="invalid recommendation candidates"):
        recommend_one([first, duplicate], client, ai_config, budget)

    assert client.messages is None
    assert budget.used == 0


def test_focus_labels_and_names_have_stable_multi_label_order(
    diamond_records, ai_config
):
    record = diamond_records[0]
    record.categories = [
        "device-grade-single-crystal",
        "diamond-thermal-management",
        "other-diamond",
        "diamond-power-rf-detectors",
    ]
    client = RecommendationClient({"key": record_key(record), "reason": "值得阅读。"})

    recommend_one([record], client, ai_config, RequestBudget(1))
    candidate = json.loads(client.messages[-1]["content"])["candidates"][0]

    assert candidate["focus_labels"] == [
        "diamond-power-rf-detectors",
        "diamond-thermal-management",
        "device-grade-single-crystal",
    ]
    assert focus_names(record) == (
        "金刚石功率/射频/探测器件",
        "金刚石器件散热",
        "金刚石单晶器件",
    )
